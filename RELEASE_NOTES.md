# v0.1.0

First public source release. Install from the source archive using the README. The Python wheel contains the endpoint CLI. VPS scripts and deployment instructions are in the source release.

- Dedicated VPS service, two restricted roles, a shareable public enrollment file, per-device keys, out-of-band host-key pinning and explicit enrollment/revocation.
- Windows windowless login service, macOS LaunchAgent, DSM boot hook, reconnect backoff and per-profile lifecycle commands.
- Layered diagnostics and random-file checksum probes. A changed or unrelated file cannot be removed by the probe cleanup command.
- Migration uses parallel endpoint state and a spare port. Switch the Drive Client after a verified file transfer. One NAS and multiple clients are supported per installation.

See [operations](docs/operations.md) and the [design review](docs/design-review.md) for certificate setup, throughput measurement, limitations and rollback.
