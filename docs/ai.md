# 用 AI 接入和维护 Drive SSH Relay

在解压目录运行 `python3 relay.py`，Windows 可用 `python relay.py`。安装 wheel 后，`drive-ssh-relay` 提供相同的命令。带空格的文件路径要加引号。指定配置目录时，全局参数 `--state "/absolute/private/directory"` 放在子命令前。

## 给助手的任务示例

> 帮我检查这台电脑能否接入 Drive SSH Relay。用我提供的可信接入文件，在独立配置和空闲端口试运行。告诉我缺少什么、要交给管理员哪一个公钥，以及如何验证在家与异地的线路。保留我当前的同步任务。

## 非交互命令

使用设备管理台时，先运行 `capabilities` 和 `preflight`。将管理员提供的一次性配对码通过标准输入送给 `fleet join --url https://drive.example.net:8443 --code-stdin`，再运行 `fleet install` 安装独立的管理上报任务。管理员在网页核准 SSH 公钥后才从新设备执行 `service install`。用 `fleet sync` 获取策略版本和实际线路。已配对设备的 `status.transport` 显示实际监听者与线路，`status.runtime` 显示新版路由服务。凭证保存在设备私有状态目录。页面入口、端口选择与迁移步骤见 [自建管理台](fleet.md)。

| 目标 | 命令 | 读取结果与下一步 |
| --- | --- | --- |
| 了解本机版本和能力 | `capabilities` | JSON 的 `schema`、`version`、支持的命令、模式及退出码 |
| 检查新电脑 | `preflight --local-port 17690` | 读取 `ready`、`checks[].id/ok/action`。缺少必要组件时执行对应的安装操作后重试。自定义 Drive 安装位置可传 `--drive-path` |
| 导入管理员给的公开参数 | `setup --invite relay-invite.json --local-port 17690` | 成功后将 `device_public_key` 传给 VPS 管理员授权。重复导入相同文件保持设备身份及已保存的选路策略 |
| 核对身份并启动 | VPS 上 `sudo python3 deploy/vps/server.py authorize client laptop laptop.pub`；桌面上 `service install` | 桌面以后登录自启。NAS 已有可用回连时复用 |
| 查看当前线路 | `status`、`doctor` | `status.runtime.route` 为 `lan` 或 `vps`。查看 `route_reason`、`detail`、`connections`、`switched_at`。NAS 或未运行的客户端可能没有 `route` |
| 调整策略 | `config show`、`config set --mode relay` | 保存到私有配置，正在运行的新版后台下一轮检查会应用。要恢复自动，用 `config set --mode auto` |
| 配置旧接入文件的家庭信息 | `config set --mode auto --lan-ip 192.168.1.20 --nas-name nas.example.net` | 填管理员提供的 NAS 地址、可信 HTTPS 名称或 SHA-256 指纹，可传 `--verify-port` 和 `--drive-port` |
| 证明真实同步 | `probe send` 和另一端 `probe ack` | 先按 `--help` 了解参数，在各自同步目录验证文件字节和 SHA-256，再反向重复 |

- `status`、`setup` 和 `preflight` 返回有版本标识的 JSON。诊断、配置和文件探针也输出 JSON。正常成功为 0，参数或执行错误通常为 1，环境缺件或未通过验证为 2。命令行语法错误由 argparse 返回 2。
- `nas_identity_mismatch` 时自动回退 VPS。由管理员核对 DSM 当前证书，设备公钥授权及 SSH 主机指纹另行检查。控制台 URL 只用于本机交互，自动化直接调用 CLI。
- 保留 Drive 自身的 SSL 验证。只处理本项目创建的状态、服务和随机样本。设备身份私钥留在设备，AI 接入只需邀请文件、公开指纹和自己的公钥。
