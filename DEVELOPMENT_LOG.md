# Development log

## v0.1.0

Extract the dedicated VPS SSH service, NAS reverse tunnel and desktop client into a portable distribution. Replace machine-specific profiles with per-device enrollment and a trusted host key. Separate TCP checks from end-to-end file verification; refuse to treat an unrelated listener as a healthy service.

Windows smoke testing found an asynchronous stop/start race in Task Scheduler. The endpoint now waits for both the scheduler state and the profile lock. Directory names with spaces and long Windows paths are covered by the CLI and the file probe. Runtime state stays outside the repository.

The first Linux integration run transferred data but found that PAM logins could survive a systemd stop. The dedicated daemon now uses public-key authentication with PAM disabled and accounts with a nonmatching password field. A second real integration run verified both SSH sessions reconnect, revocation rejects reconnection, and purge removes the installation. macOS registration waits for launchd to unload the previous job. The trusted public enrollment file carries VPS ports and the pinned key to both endpoints.

Validation is reproducible with `python -m unittest discover -s tests -v`, `python tests/service_smoke.py` on an interactive test host, and the disposable Linux SSH integration job in `.github/workflows/test.yml`. The Linux job uses a trusted TLS responder and two real SSH tunnels. Production DSM file syncing with the new package needs a separate two-device probe. The design derives from a working Synology Drive deployment; results from that earlier deployment are not release-binary benchmarks.
