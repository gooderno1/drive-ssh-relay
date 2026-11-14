# v0.3.3

- Windows 和 macOS 的同一 Drive 入口支持三种选择：自动、手动 VPS、手动 NAS 直连。手动直连仍验证 NAS；无法确认身份或端口时显示原因，等直连恢复。
- 管理台区分已配置的策略与设备上报的实际线路；旧客户端升级并上报后才可选手动直连。本机页面和命令行也能修改模式。

# v0.3.2

- 设备首页显示实际线路、在线状态、Drive 活动连接与最近上报。策略、公钥和设备接入移到独立窗口。
- 提供 HTTPS 和现有 SSH 共用 443 的部署模板。原地址可跳转至正式域名，已配对设备可以继续使用原有接入地址。[部署方法](docs/fleet.md)

# v0.3.1

- 管理员可以用至少 8 个字符、同时含字母和数字的密码首次设密或重置密码。已有管理员可继续使用原密码登录。已配对的设备无需升级。

# v0.3.0

- 新增 VPS 设备管理台。管理员登录后可以下载客户端、生成短期配对码、授权设备公钥、查看设备在线情况，按设备修改家庭网络与线路策略。
- Windows 与 macOS 在用户登录时运行独立的管理任务。设备确认已保存的配置版本，旧线路也可先接入管理。Windows 本机页能识别已登记的既有 OpenSSH 中继和 Drive 活动连接。
- 新版安装器把程序保存在用户应用目录，本机控制页和设备管理台均可在手机宽度查看。[安装、升级和迁移](docs/fleet.md)

# v0.2.0

- Windows 和 macOS 支持固定本机入口自动选路。家中通过 NAS 身份、同链路地址与端口检查后直连，异地自动走 VPS。切换时 Drive 会重新建立传输连接。
- 新增本机控制台、Windows 与 macOS 启动入口、新设备依赖检查、一次邀请导入，以及 AI 使用手册和 JSON 命令。
- v0.1.0 的配置与邀请继续使用中继模式。升级端侧运行包后，可在控制台填写家庭参数，或在独立配置中导入 v2 邀请。已有服务可先在临时端口验收。
- 源码包含服务端和桌面启动器。wheel 提供端侧 CLI 和配置页。详见 README、`docs/ai.md` 与 `docs/operations.md`。

# v0.1.0

First public source release. Install from the source archive using the README. The Python wheel contains the endpoint CLI. VPS scripts and deployment instructions are in the source release.

- Dedicated VPS service, two restricted roles, a shareable public enrollment file, per-device keys, out-of-band host-key pinning and explicit enrollment/revocation.
- Windows windowless login service, macOS LaunchAgent, DSM boot hook, reconnect backoff and per-profile lifecycle commands.
- Layered diagnostics and random-file checksum probes. A changed or unrelated file cannot be removed by the probe cleanup command.
- Migration uses parallel endpoint state and a spare port. Switch the Drive Client after a verified file transfer. One NAS and multiple clients are supported per installation.

See [operations](docs/operations.md) and the [design review](docs/design-review.md) for certificate setup, throughput measurement, limitations and rollback.
