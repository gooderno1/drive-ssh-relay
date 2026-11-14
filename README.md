# Drive SSH Relay

给 Synology Drive 桌面客户端自建 TCP 中继。NAS 和电脑各自向一台 Linux VPS 建立出站 SSH 连接，家里有公网地址或入站端口映射都不是前提。原来的同步任务继续由 Synology Drive 管理，VPS 只转发连接。项目采用 MIT 许可证，与 Synology 无隶属关系。

**Windows 和 macOS 自动选路。** Drive 长期使用同一个本机入口。桌面助手确认处于 NAS 所在链路、DSM 身份可信且 Drive 端口可达时直连。外出或身份验证失败时走 VPS。切换会让已有连接重新建立，文件恢复由 Drive 处理。[选路原理](docs/operations.md#routes)

**手动指定线路。** 在设备管理台的线路策略或本机配置页选择「手动 · VPS 中继」或「手动 · NAS 直连」。锁定直连时，访问不到可信 NAS 会暂停，网络恢复后继续。选择「自动」即可恢复按网络选路，三种模式都沿用原 Drive 地址。

### 由管理台接入新电脑

1. 管理员登录自建管理台，在「新设备接入」填写电脑名称，下载 Windows / macOS 客户端并生成 15 分钟有效的配对码。
2. 新电脑安装 Synology Drive Client 和 Python 3.9+。完整解压客户端，Windows 双击 `Install Relay.vbs`，macOS 从访达打开 `Install Relay.command`。在本机页填写管理台 HTTPS 地址和配对码。
3. 管理员展开这台设备的「SSH 公钥与设备权限」并授权。新电脑在本机页点击「安装并启动」，首次配置 Drive 的固定本机入口。管理台此后能显示实际线路和配置确认版本，并按设备设置家庭直连优先或始终中转。[自建管理台及迁移](docs/fleet.md)

### 使用接入文件的设备

1. 下载并完整解压源码包。Windows 双击 `Open Relay.vbs`，macOS 打开同目录的 `Drive Relay.app`。两者打开本机控制台。也可在解压目录执行 `python3 relay.py panel --open`（Windows 可用 `python`）。启动器会检查 Python，页面能检查 Drive Client、OpenSSH、登录启动组件和端口，缺项会给出下一步。[下载版本](https://github.com/gooderno1/drive-ssh-relay/releases)
2. 导入管理员提供的接入文件，把页面显示的**设备公钥**交给管理员授权，然后点击「安装并启动」。含家庭网络信息的 v2 文件会启用自动选路，已有 v1 文件默认始终中转，可在配置页补充家庭 NAS 信息。
3. 首次将已有 Drive 连接指向固定本机入口，例如解析到回环地址、与 NAS Drive 证书匹配的 `drive.example.net:16690`。已有相同入口的设备可沿用。以后换网络无需编辑地址。控制台可随时重新打开，查看当前线路、原因及连接数，或保存新的线路设置。[AI 使用手册](docs/ai.md)

```text
Drive Client → 固定本机入口 ┬→ 已核验的家庭 NAS 局域网 Drive 端口
                          └→ SSH → VPS 回环桥接口 → NAS 出站回连 → Drive
```

| 位置 | 需要 | 启动方式 |
| --- | --- | --- |
| VPS | Debian/Ubuntu、systemd、发行版 OpenSSH Server、Python 3.9+、空闲公网 TCP 端口 | 独立的 systemd 单元 |
| NAS | DSM、Synology Drive Server、Python 3.9+、可执行的 `ssh` 和 `ssh-keygen` | 普通用户运行。DSM 任务计划程序选择“开机” |
| 电脑 | Windows 10/11、macOS 或 Linux，Python 3.9+、系统 OpenSSH、Drive Client | Windows 无窗口登录任务，macOS LaunchAgent，Linux 提供启动脚本 |

## 首次使用

从 [Releases](https://github.com/gooderno1/drive-ssh-relay/releases) 下载源码包并解压，在相应设备的项目目录执行下列命令。直接运行源码只需 Python 标准库。Windows 可将 `python3` 换成 `python`。如果 VPS 的 443 已给 HTTPS 使用，请在 VPS 安装时选一个空闲 TCP 端口，如 7443，接入文件会自动带上实际端口。

1. 在 VPS 安装独立中继。已有可用的管理入口是前提。只在 VPS 防火墙和云安全组允许选定的 SSH 端口。安装程序会先检查端口和角色账号冲突。

   ```sh
   sudo python3 deploy/vps/server.py install --ssh-port 443
   sudo python3 deploy/vps/server.py invite --host relay.example.net --lan-ip 192.168.1.20 --nas-name nas.example.net > relay-invite.json
   ```

   换成自己的 VPS 地址、NAS 局域网 IPv4 地址和 DSM HTTPS 证书名称。管理员配置一次后可以给多台电脑分发。使用自签名证书时可提供从可信 DSM 管理渠道取得的 `--cert-sha256`。接入文件只发给自己的设备，里面有网络信息和公钥。若 NAS 仍用 v0.1.0 程序，单独生成旧格式文件 `invite --host relay.example.net` 供其导入；原有有效回连可继续复用。

2. 首次配置 NAS 时导入接入文件并生成独立密钥。桌面可用上面的页面或命令行。

   ```sh
   # NAS
   python3 relay.py init nas --invite relay-invite.json

   # 每一台电脑
   python3 relay.py setup --invite relay-invite.json
   ```

   每次 `init` 会显示设备的 Ed25519 **公钥**，并在私有状态目录生成 `identity.pub`。通过已有管理连接将 NAS 和电脑的这两个公钥文件放到 VPS，分别命名为 `nas.pub`、`laptop.pub`。VPS 的主机公钥用于两端识别 VPS，设备公钥用于 VPS 放行设备，两者用途不同。

3. 在 VPS 授权，在两端启动。新增电脑只需要为它生成并授权一把独立密钥。

   ```sh
   sudo python3 deploy/vps/server.py authorize nas home-nas nas.pub
   sudo python3 deploy/vps/server.py authorize client laptop laptop.pub
   # 在 NAS 和电脑分别执行
   python3 relay.py service install
   python3 relay.py doctor
   ```

   Windows 安装后即运行，并在用户登录时无窗口启动。macOS 在登录会话中执行安装命令。DSM 安装命令会打印开机命令，复制到“控制面板 → 任务计划程序 → 新增 → 触发的任务 → 用户定义的脚本”，选择**刚运行安装命令的普通用户**和“开机”，保存后可点击“运行”验证。每个 NAS 只需登记一次。

4. 在电脑给中继设置一个与 NAS 的 **Drive 服务证书**匹配的域名。例如 NAS 证书含有 `drive.example.net`，可在这台电脑的 hosts 中将这个专用名字指到 `127.0.0.1`。在 Drive Client 原连接的“编辑连接”中填 `drive.example.net:16690`，开启 SSL 数据传输加密，按原 NAS 账号登录。先记录原地址以便回退。若客户端报告证书不匹配或不受信任，请先修正证书、名称或信任链。部分 DSM 登录、Web 分享和浏览器文件操作另用 HTTPS，需保留这些功能原有的访问路径。[群晖 SSL 说明](https://kb.synology.com/DSM/tutorial/common_SSL_issues_for_drive_client) · [Drive 客户端帮助](https://kb.synology.com/DSM/help/SynologyDriveClient/synologydriveclient)

5. 用同步文件验收。在一端的已同步普通目录执行 `send`，复制输出里的文件名，在另一端同一同步目录执行 `ack`。反过来再做一次即可分别验收上传和下载。

   ```sh
   python3 relay.py probe send --folder "/path/to/synced-folder" --mib 1
   python3 relay.py probe ack --folder "/other/synced-folder" --name "完整的测试文件名" --timeout 180
   python3 relay.py probe clean --folder "/path/to/synced-folder" --name "完整的测试文件名"
   ```

   `ack` 只有字节数与 SHA-256 都匹配才成功。`clean` 只接受项目的样本文件名并再次校验，通常只在一端执行，让 Drive 同步删除。

## 使用入口

| 要做的事 | 命令或说明 |
| --- | --- |
| 查看本机链路层次 | `python3 relay.py doctor` 输出 JSON。各层通过后再用文件验收 |
| 管理桌面、手动更改选路 | `python3 relay.py panel --open`。本机回环上的按需设置页，关闭控制台按钮会结束管理进程 |
| 用 AI 或脚本自助接入 | 从 `capabilities`、`preflight`、`setup`、`status` 开始。输出、退出码和升级步骤见 [AI 使用手册](docs/ai.md) |
| 试运行、观察故障 | `python3 relay.py run`，按 Ctrl+C 结束。已有同一配置的后台实例时会提示 |
| 单独测试、并行迁移 | 在子命令之前加 `--state /absolute/private/path`。客户端可选 `--local-port`；NAS、VPS 和所有客户端的 `--bridge-port` 应一致 |
| 开停自启动服务 | `python3 relay.py service start`、`service stop`、`service uninstall` |
| 新增电脑、轮换和撤销设备 | [部署与运维](docs/operations.md) |
| VPS 将到期、换 IP 或供应商 | [并行迁移和原路回退](docs/operations.md#replace-vps) |
| 选中继线路、判断下载慢、区别可用性和吞吐 | [设计审查与取舍](docs/design-review.md) |
| 测试范围与版本演进 | [验证记录](DEVELOPMENT_LOG.md) · [发布说明](RELEASE_NOTES.md) |

异地速度受两段线路、VPS 带宽及 Drive 队列影响。评估替代节点时，在相同异地网络上用新的随机文件比较旧、新中继的双向表现。本地直连只在能访问 NAS 的局域网测量。
