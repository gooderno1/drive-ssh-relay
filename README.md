# Drive SSH Relay

给 Synology Drive 桌面客户端自建 TCP 中继。NAS 和电脑各自向一台 Linux VPS 建立出站 SSH 连接，家里有公网地址或入站端口映射都不是前提。原来的同步任务继续由 Synology Drive 管理，VPS 只转发连接。项目采用 MIT 许可证，与 Synology 无隶属关系。

```text
Drive Client ── 本机 127.0.0.1:16690 ── SSH 本地转发 ─┐
                                                     ├─ VPS 127.0.0.1:16690
Drive Server ── NAS 127.0.0.1:6690 ── SSH 反向转发 ───┘
                                      两端出站到 VPS TCP 443
```

| 位置 | 需要 | 启动方式 |
| --- | --- | --- |
| VPS | Debian/Ubuntu、systemd、发行版 OpenSSH Server、Python 3.9+、空闲公网 TCP 端口 | 独立的 systemd 单元 |
| NAS | DSM、Synology Drive Server、Python 3.9+、可执行的 `ssh` 和 `ssh-keygen` | 普通用户运行。DSM 任务计划程序选择“开机” |
| 电脑 | Windows 10/11、macOS 或 Linux，Python 3.9+、系统 OpenSSH、Drive Client | Windows 无窗口登录任务，macOS LaunchAgent，Linux 提供启动脚本 |

## 首次使用

从 [Releases](https://github.com/gooderno1/drive-ssh-relay/releases) 下载源码包并解压，在相应设备的项目目录执行下列命令。直接运行源码只需 Python 标准库。Windows 可将 `python3` 换成 `python`。如果 VPS 的 443 已给 HTTPS 使用，选择一个空闲 TCP 端口，如 7443，并在两端的 `init` 都加上 `--ssh-port 7443`。

1. 在 VPS 安装独立中继。已有可用的管理入口是前提。只在 VPS 防火墙和云安全组允许选定的 SSH 端口。安装程序会先检查端口和角色账号冲突。

   ```sh
   sudo python3 deploy/vps/server.py install --ssh-port 443
   sudo python3 deploy/vps/server.py host-key
   ```

   通过已验证的 VPS 管理连接或提供商控制台，把 `host-key` 输出的整行公钥保存为两台设备上的 `vps-host.pub`。安装命令还会打印 SHA-256 指纹，便于另一路核对。

2. 分别在 NAS 和电脑注册设备，把示例域名换成 VPS 公网域名。

   ```sh
   # NAS
   python3 relay.py init nas --host relay.example.net --host-key-file vps-host.pub

   # 每一台电脑
   python3 relay.py init client --host relay.example.net --host-key-file vps-host.pub
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
| 试运行、观察故障 | `python3 relay.py run`，按 Ctrl+C 结束。已有同一配置的后台实例时会提示 |
| 单独测试、并行迁移 | 在子命令之前加 `--state /absolute/private/path`。客户端可选 `--local-port`；NAS、VPS 和所有客户端的 `--bridge-port` 应一致 |
| 开停自启动服务 | `python3 relay.py service start`、`service stop`、`service uninstall` |
| 接入第二台电脑、轮换和撤销 | [部署与运维](docs/operations.md) |
| 选中继线路、判断下载慢、区别可用性和吞吐 | [设计审查与取舍](docs/design-review.md) |
| 测试范围与版本演进 | [验证记录](DEVELOPMENT_LOG.md) · [发布说明](RELEASE_NOTES.md) |

客户端实际得到的速度取决于两段线路、VPS 带宽和 Drive 队列。先用相同文件、方向和时间窗口比较直连与中继，再决定是否切换常用连接。
