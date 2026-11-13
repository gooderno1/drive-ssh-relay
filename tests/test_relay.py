import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
import xml.etree.ElementTree as ET

from drive_relay import cli, config, probe, runtime, services


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / "state with spaces"
        key = self.base / "host"
        subprocess.run([config.executable("ssh-keygen"), "-q","-t","ed25519","-N","","-f",str(key)],
                       check=True, **config.quiet_kwargs())
        self.hostkey = key.with_suffix(".pub").read_text()
        self.profile = config.Profile("client", "relay.example.test", self.hostkey)

    def test_enrollment_is_repeatable_and_pins_same_key(self):
        key = config.initialize(self.state, self.profile)
        private_stat = (self.state/"identity").stat()
        self.assertEqual(key, config.initialize(self.state, self.profile))
        self.assertEqual(private_stat.st_mtime_ns, (self.state/"identity").stat().st_mtime_ns)
        with self.assertRaisesRegex(ValueError, "different settings"):
            config.initialize(self.state, config.Profile("nas", self.profile.host, self.hostkey))
        self.assertEqual("drive-relay-pin " + config.public_key(self.hostkey),
                         (self.state/"known_hosts").read_text().strip())

    def test_ssh_accepts_space_paths_and_exact_forward_direction(self):
        config.initialize(self.state, self.profile)
        for role, flag, ports in (("client","localforward","127.0.0.1:16690"),
                                  ("nas","remoteforward","127.0.0.1:6690")):
            p = config.Profile(role, self.profile.host, self.hostkey)
            args = config.ssh_args(self.state, p)
            out = subprocess.check_output([args[0], "-G", *args[1:]], stderr=subprocess.DEVNULL,
                                          **config.quiet_kwargs()).decode()
            self.assertIn("stricthostkeychecking true", out.lower().replace(" yes", " true"))
            self.assertIn("hostkeyalias drive-relay-pin", out.lower())
            self.assertIn(flag + " ", out.lower())
            self.assertIn(ports, out.replace("[127.0.0.1]", "127.0.0.1"))
            self.assertIn("state with spaces", out)

    def test_bad_input_does_not_generate_keys(self):
        for host in ("-oProxyCommand", "wrong\nhost","a..b","some host","x:22"):
            with self.assertRaises(ValueError):
                config.Profile("client", host, self.hostkey)
        with self.assertRaises(ValueError):
            config.Profile("client","vps.test","ssh-ed25519 AAAA")
        with self.assertRaises(ValueError):
            config.Profile("client","vps.test",self.hostkey,443,443)

    def test_invitation_import_and_conflicting_overrides(self):
        invite=self.base/"relay-invite.json"
        doc={"schema":"drive-ssh-relay/v1","host":"relay.example.test","ssh_port":7443,
             "bridge_port":19999,"host_key":self.hostkey}
        invite.write_text(json.dumps(doc),encoding="utf-8-sig")
        base=["init","client","--invite",str(invite)]
        self.assertEqual(cli.profile_from_args(cli.parser().parse_args(base)).ssh_port,7443)
        with self.assertRaisesRegex(ValueError,"already supplies"):
            cli.profile_from_args(cli.parser().parse_args(base+["--ssh-port","443"]))
        doc["identity"]="unrecognized"
        invite.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError,"unrecognized"):
            cli.profile_from_args(cli.parser().parse_args(base))

    def test_existing_foreign_listener_is_not_health(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1",0))
            other.listen(4)
            endpoint = other.getsockname()[1]
            self.assertFalse(runtime.local_free(endpoint))
            p = config.Profile("client","127.0.0.1",self.hostkey,ssh_port=endpoint,local_port=endpoint)
            config.initialize(self.state,p)
            with contextlib.redirect_stdout(io.StringIO()) as log:
                self.assertEqual(2, runtime.doctor(self.state))
            self.assertFalse(json.loads(log.getvalue())["checks"]["profile_supervisor_active"])

    def test_supervisor_lock_and_probe_errors(self):
        with runtime.exclusive(self.base/"lock"):
            with self.assertRaisesRegex(ValueError, "another instance"):
                with runtime.exclusive(self.base/"lock"):
                    pass
        with contextlib.redirect_stdout(io.StringIO()) as report:
            probe.send(self.base,1)
        name = json.loads(report.getvalue())["file"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probe.verify(self.base,name,0),0)
            (self.base/name).write_bytes(b"x" * 1048576)
            self.assertEqual(probe.verify(self.base,name,0,True),2)
        self.assertTrue((self.base/name).exists())
        with self.assertRaises(ValueError):
            probe.verify(self.base,"../note.md",0,True)

    def test_cloud_placeholder_is_not_a_completed_probe(self):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            probe.send(self.base,1)
        name=json.loads(buf.getvalue())["file"]
        info=SimpleNamespace(st_size=1048576, st_file_attributes=0x400000)
        with mock.patch.object(Path,"stat",return_value=info), mock.patch.object(Path,"is_file",return_value=True), mock.patch.object(Path,"is_dir",return_value=True), mock.patch.object(Path,"is_symlink",return_value=False), mock.patch.object(Path,"open",side_effect=OSError(22,"cloud access denied")), contextlib.redirect_stdout(io.StringIO()) as result:
            self.assertEqual(probe.verify(self.base,name,0,True),2)
        self.assertEqual(json.loads(result.getvalue())["reason"],"cloud_placeholder_unreadable")
        self.assertTrue((self.base/name).exists())

    def test_packaged_runtime_does_not_reference_checkout(self):
        config.initialize(self.state,self.profile)
        exe = services.package(self.state)
        result = subprocess.check_output([sys.executable,"-I","-B",str(exe),"--version"],
                                         cwd=self.base, **config.quiet_kwargs()).decode().strip()
        self.assertEqual(result,"0.1.0")
        doc = ET.fromstring(services.windows_xml(self.state,exe,"S-1-5-21-123"))
        self.assertEqual(Path(doc.findtext(".//{*}Command")).name,"pythonw.exe")
        self.assertIn(str(exe),doc.findtext(".//{*}Arguments"))
        self.assertIsNone(doc.find(".//{*}TimeTrigger"))
        self.assertEqual(doc.findtext(".//{*}MultipleInstancesPolicy"),"IgnoreNew")


if __name__ == "__main__":
    unittest.main()
