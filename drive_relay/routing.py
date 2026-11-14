"""A fixed Drive listener with authenticated, on-link IPv4 LAN selection."""
import asyncio
import contextlib
from dataclasses import asdict, dataclass, replace
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import ssl
import time

from .config import LOOPBACK, atomic, hostname, port, quiet_kwargs, ssh_args
from .runtime import category, stamp


@dataclass(frozen=True)
class Routes:
    mode: str = "relay"
    lan_ip: str = ""
    nas_name: str = ""
    drive_port: int = 6690
    verify_port: int = 5001
    cert_sha256: str = ""

    def __post_init__(self):
        if self.mode not in ("auto", "relay"):
            raise ValueError("mode must be auto or relay")
        port(self.drive_port)
        port(self.verify_port)
        if self.lan_ip or self.mode == "auto":
            ip = ipaddress.IPv4Address(self.lan_ip)
            nets = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")
            if not any(ip in ipaddress.ip_network(n) for n in nets):
                raise ValueError("use a private NAS LAN IPv4 address")
            object.__setattr__(self, "lan_ip", str(ip))
        if self.nas_name:
            object.__setattr__(self, "nas_name", hostname(self.nas_name))
        if self.cert_sha256:
            digest = self.cert_sha256.replace(":", "").lower()
            if not re.fullmatch("[a-f0-9]{64}", digest):
                raise ValueError("expected a SHA-256 fingerprint of the DSM HTTPS certificate")
            object.__setattr__(self, "cert_sha256", digest)
        if self.mode == "auto" and not (self.nas_name or self.cert_sha256):
            raise ValueError("auto mode requires a trusted NAS certificate name or fingerprint")


def settings(state):
    path = state / "routing.json"
    if not path.exists():
        return Routes()
    return Routes(**json.loads(path.read_text(encoding="utf-8")))


def save(state, profile, routes):
    if profile.role != "client":
        raise ValueError("routing settings belong to desktop clients")
    if routes.lan_ip and ipaddress.ip_address(routes.lan_ip).is_loopback and profile.local_port in (routes.drive_port, routes.verify_port):
        raise ValueError("LAN ports must not point back to the Drive listener")
    atomic(state / "routing.json", (json.dumps(asdict(routes), indent=2) + "\n").encode())


def on_link(address, number, timeout=1.5):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        # A router drops TTL=1. Overlapping private subnets also need the NAS identity check.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 1)
        sock.connect((address, number))
        return sock
    except BaseException:
        sock.close()
        raise


def check_home(routes):
    if routes.mode == "relay":
        return False, "relay_selected"
    try:
        if routes.cert_sha256:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # The trusted invite pins exact DER bytes below.
        else:
            ctx = ssl.create_default_context()
        with on_link(routes.lan_ip, routes.verify_port) as raw:
            with ctx.wrap_socket(raw, server_hostname=routes.nas_name or None) as tls:
                if routes.cert_sha256:
                    digest = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
                    if not hmac.compare_digest(digest, routes.cert_sha256):
                        return False, "nas_identity_mismatch"
        with on_link(routes.lan_ip, routes.drive_port):
            return True, "home_verified"
    except ssl.SSLError:
        return False, "nas_identity_mismatch"
    except OSError:
        return False, "lan_unavailable"


async def pipe(reader, writer):
    while True:
        data = await reader.read(65536)
        if not data:
            if writer.can_write_eof():
                writer.write_eof()
                await writer.drain()
            return
        writer.write(data)
        await writer.drain()


class Router:
    def __init__(self, state, profile, stop, report):
        self.state, self.profile = state, profile
        self.stop, self.report = stop, report
        self.routes = Routes()
        self.route, self.reason, self.error = "pending", "checking", ""
        self.switched = None
        self.ready = asyncio.Event()
        self.sessions = set()
        self.backend = None
        self.ssh_pid = None
        self.relay_detail = "connecting"

    async def relay_worker(self):
        """One multiplexed SSH connection for all Drive streams on the VPS path."""
        delay = 1
        while True:
            child = None
            latest = ["connecting"]
            try:
                # OpenSSH owns this auxiliary listener. Only publish its port
                # after this child confirms binding it, including after retries.
                with socket.socket() as candidate:
                    candidate.bind((LOOPBACK, 0))
                    number = candidate.getsockname()[1]
                args = ssh_args(self.state, replace(self.profile, local_port=number))
                args[1:1] = ["-v"]
                spawning = asyncio.create_task(asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                      stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, **quiet_kwargs()))
                try:
                    child = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    child = await spawning
                    raise
                started = time.monotonic()
                async for line in child.stderr:
                    message = line.decode(errors="replace")
                    if f"Local forwarding listening on {LOOPBACK} port {number}." in message:
                        # OpenSSH can log the target before bind(). The channel
                        # creation line below confirms that the listener exists.
                        latest[0] = "binding"
                    elif latest[0] == "binding" and ("new port-listener" in message or "[port listener]" in message):
                        self.backend, self.relay_detail = number, ""
                        self.ssh_pid = child.pid
                        self.error = ""
                        latest[0] = ""
                    else:
                        label = category(message)
                        if label != "ssh_exit":
                            self.relay_detail = latest[0] = label
                await child.wait()
                if time.monotonic()-started > 30:
                    delay = 1
            except OSError:
                latest[0] = "ssh_start_failed"
            finally:
                self.backend = None
                self.ssh_pid = None
                self.relay_detail = latest[0] or "connection_lost"
                if child and child.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        child.terminate()
                    try:
                        await asyncio.wait_for(child.wait(), 3)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            child.kill()
                        await child.wait()
            await asyncio.sleep(delay)
            delay = min(15, delay * 2)

    async def serve(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((LOOPBACK, self.profile.local_port))
            listener.setblocking(False)
            server = await asyncio.start_server(self.accept, sock=listener, backlog=64)
        except BaseException:
            listener.close()
            raise
        request = self.state / "stop.request"
        votes = 0
        relay = None
        async with server:
            try:
                while not self.stop.is_set() and not request.exists():
                    try:
                        config = settings(self.state)
                        at_home, reason = await asyncio.to_thread(check_home, config)
                    except (OSError, ValueError, TypeError):
                        config, at_home, reason = Routes(), False, "invalid_routing_settings"
                    route = "lan" if at_home else "vps"
                    votes = votes + 1 if route != self.route else 0
                    changed = config != self.routes
                    # Fail closed immediately on identity/availability loss. Require
                    # two positive probes to move active connections back to LAN.
                    if changed or self.route == "pending" or (votes and (not at_home or votes >= 2)):
                        if self.route != route or (changed and route == "lan"):
                            previous = self.route
                            self.route, self.switched, self.error = route, stamp(), ""
                            if previous != "pending":
                                for task in tuple(self.sessions):
                                    task.cancel()  # Drive reconnects; never splice a partial stream.
                            if relay:
                                relay.cancel()
                                await asyncio.gather(relay, return_exceptions=True)
                                relay = None
                            if route == "vps":
                                self.relay_detail = "connecting"
                                relay = asyncio.create_task(self.relay_worker())
                        votes = 0
                    self.routes, self.reason = config, reason
                    self.ready.set()
                    self.publish()
                    for _ in range(10):
                        if self.stop.is_set() or request.exists():
                            break
                        await asyncio.sleep(.3)
            finally:
                if relay:
                    relay.cancel()
                    await asyncio.gather(relay, return_exceptions=True)
                for task in tuple(self.sessions):
                    task.cancel()
                if self.sessions:
                    await asyncio.gather(*tuple(self.sessions), return_exceptions=True)
        return 0

    def publish(self):
        detail = (self.relay_detail or self.error) if self.route == "vps" else self.error
        self.report.update("routing", detail, pid=self.ssh_pid, route=self.route, mode=self.routes.mode,
                           route_reason=self.reason, switched_at=self.switched, connections=len(self.sessions))

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        uplink = None
        pumps = []
        try:
            if len(self.sessions) >= 64:
                return
            self.sessions.add(task)
            await self.ready.wait()
            if self.route == "lan":
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 1)
                    sock.setblocking(False)
                    await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock, (self.routes.lan_ip, self.routes.drive_port)), 2)
                    upstream, uplink = await asyncio.open_connection(sock=sock)
                except BaseException:
                    sock.close()
                    raise
            else:
                for _ in range(60):
                    if self.backend:
                        break
                    await asyncio.sleep(.1)
                if not self.backend:
                    return
                upstream, uplink = await asyncio.open_connection(LOOPBACK, self.backend)
            pumps = [asyncio.create_task(pipe(reader, uplink)), asyncio.create_task(pipe(upstream, writer))]
            # Local half-close still permits the final server reply. A remote
            # EOF signals that Drive must reconnect, even if the client is idle.
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()
            if pumps[1] not in done:
                await pumps[1]
            self.error = ""
        except (OSError, asyncio.TimeoutError, ValueError):
            self.error = self.error or "route_connection_failed"
        finally:
            for pump in pumps:
                pump.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            if uplink:
                uplink.close()
            writer.close()
            self.sessions.discard(task)
            if self.error:
                self.publish()
