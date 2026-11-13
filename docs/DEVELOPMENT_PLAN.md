# First release acceptance

1. Isolate a new repository and use generated example credentials. Review the VPS forwarding policy and both endpoint roles.
2. Implement endpoint enrollment, startup, diagnosis, file verification and retirement. Keep all live configuration and keys in the operator's state directory.
3. Run Windows and POSIX checks. In a disposable Linux runner deploy the shipped server, forward bytes through both SSH connections, interrupt a leg, verify recovery, then remove the service.
4. Document licensing, deployment, measurements, failure modes and the difference between a verified file and a listening socket.
