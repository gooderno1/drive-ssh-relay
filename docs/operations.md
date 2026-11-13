# 部署与运维

## 安装前

- VPS 目标为带 systemd 的 Debian 12+/Ubuntu 22.04+，使用发行版的 `openssh-server`、`iproute2`、`passwd` 和 Python。独立 sshd 会监听所有 IPv4 接口的一个端口，桥接端口仅允许回环。默认公网端口是 443；运行 HTTPS 的机器可改用 7443。可用 `ss -lnt` 查看冲突，在云安全组和系统防火墙中分别核对实际端口。中继使用 SSH 协议，即便端口为 443 也需要网络允许 SSH。
- NAS 可通过套件中心安装 Python 3。用实际运行服务的用户检查 `python3 --version`、`ssh -V`、`ssh-keygen -?`。macOS/Windows 须安装可运行的 Python 3.9+；Windows 同一安装目录需要 `pythonw.exe`，OpenSSH Client 可在系统“可选功能”中安装。
- 桌面客户端和 NAS 都只需要**出站**到中继公网端口。VPS 管理员、VPS 上的本地进程和 NAS 是信任边界。使用仅承载自家服务的主机或独立实例；多租户的本机用户可能连接回环桥接端口。Drive 层保留用户登录和证书验证。
- NAS 的 Drive 服务监听默认为 TCP 6690。若更改了端口，在 NAS 的 `init nas` 加 `--drive-port`。公网 SSH 端口与 VPS 桥接端口不能相同。电脑和 VPS 位于不同主机，二者各用 16690 不冲突。
- 安装前记录原 Drive 服务器地址、端口和 SSL 状态。自定义主机名解析只应覆盖给中继使用的名称，以免其他程序的同名连接被指向回环。Drive Server 使用的证书需包含该名称，证书链需被客户端信任。

## 配置和数据位置

推荐用 `server.py invite --host relay.example.net` 导出 JSON，两端分别运行 `init nas --invite relay-invite.json` 和 `init client --invite relay-invite.json`。自建脚本也可以使用手动入口 `init client --host relay.example.net --host-key-file vps-host.pub --ssh-port 7443`，其中公钥由 VPS 的 `server.py host-key` 获取。接入文件包含的端口与主机公钥不能同时在命令行覆盖。公开接入文件不授予任何转发权限；实际接入仍需 VPS 管理员授权对应设备公钥。

| 平台 | 默认私有状态目录 | 生命周期 |
| --- | --- | --- |
| Windows | `%LOCALAPPDATA%\DriveSSHRelay` | 当前用户登录，GUI 子系统的 Python 和无控制台 SSH 子进程。无需管理员安装客户端任务 |
| macOS | `~/Library/Application Support/DriveSSHRelay` | 当前用户的 LaunchAgent，需在真实登录会话安装 |
| DSM / Linux | `~/.local/share/drive-ssh-relay` | 私有目录、单实例文件锁、安装后输出开机脚本 |
| VPS | `/etc/drive-ssh-relay` | systemd 的 `drive-ssh-relay.service`，日志在 journal |

设备状态目录保存私钥 `identity`、公钥 `identity.pub`、主机公钥清单、脱敏的状态和事件日志。POSIX 目录权限为 0700。Windows 使用当前用户私有的本机目录，避免共享目录或放宽文件 ACL。运行时打包到该目录，移动源码后服务仍可启动。默认服务不记录文件名、账号凭据或同步日志。后台状态保留最近两份小日志，详细 VPS 日志的保留期由 journald 决定。

## 四步定位故障

| 现象 | 核对方法 | 可采取的行动 |
| --- | --- | --- |
| VPS TCP 不通 | 两端分别运行 `doctor`，VPS 执行 `sudo python3 deploy/vps/server.py status` | 检查公网域名、VPS 公网端口、云安全组及线路 |
| `key_rejected` 或 `host_key_mismatch` | 对比 VPS 控制台的主机指纹、设备 `identity.pub` 与对应角色授权 | 将设备公钥放入正确角色。主机密钥发生轮换时先从独立可信入口核验，再新建配置迁移 |
| `local_port_conflict` | 查看 `doctor` 的 `phase/detail`，Windows 用 `Get-NetTCPConnection -LocalPort 16690`，macOS 用 `lsof -nP -iTCP:16690` | 查清占用者，或给新客户端配置选另一空闲本机端口。状态不会把其他程序的监听当作本项目服务 |
| `bridge_unavailable` / `forward_destination_failed` | VPS status 应显示 `127.0.0.1:<桥接端口>`；NAS doctor 应显示 Drive 服务端口可达 | 检查 NAS 回连、重复 NAS 实例、桥接端口的一致性和 Drive Server。一次 VPS 只能有一个 NAS 占用某个桥接口 |
| 端口正常、文件不动 | 用普通同步文件夹双向运行随机文件探针，检查 Drive 日志和本机空间 | 核对任务是否暂停、过滤规则、文件权限、按需状态、客户端证书及账号。重复锁定需按文件核对，优先保留原文件 |
| 偶发掉线 | `status.json` 的时间、`events.log` 的错误类别和 `journalctl -u drive-ssh-relay` | 检查每一段的断开时间。客户端的退避上限为 60 秒；Windows、macOS 在用户登录后启动 |

`doctor` 的退出码 0 表示该设备能访问 VPS、能看到本机相关监听、并且当前配置的监督进程持有运行锁且心跳新鲜。SSH TCP 可能正处于认证阶段，或者 VPS 到 NAS 的后半段暂时不可用。完整业务验收使用 `probe ack`，退出码 0 表示测试文件的实际内容匹配。退出码 2 表示至少有一项待处理。

Windows 按需同步可先创建逻辑大小正确的占位文件，Drive 也会登记完成。如果接收端返回 `cloud_placeholder_unreadable`，请在已登录 Drive 的同一用户会话运行 `ack`，或在已固定为离线可用的测试文件夹再测。后台 SSH 登录可能没有权限触发云文件下载。探针保留未验证的样本，不会自动操作同步目录的固定策略。

## 增加、轮换、撤销

每台电脑独立执行 `init client`，在 VPS 用不同标签运行 `authorize client <label> <device.pub>`。带同一个标签和公钥的重复操作是幂等的。更换密钥时先给新状态目录生成密钥，以新标签添加，开另一可用本机端口验证探针，再执行 `revoke client <old-label>`。NAS 换钥时安排切换窗口，因为桥接端口只能由一台 NAS 占用。

撤销会拒绝后续认证。若需要立即断开已经建立的所有中继会话，可在维护窗口重启 **drive-ssh-relay.service**；所有授权设备随后会重新认证。恢复 VPS 时保管好主机私钥备份和设备公钥清单；如主机身份已变更，需从可信控制台重新分发指纹与公钥。

## 升级、回退与卸载

端点升级可从新版源码再次执行 `service install`，服务使用带内容摘要的运行包。macOS、Windows 会更新启动项，DSM 的固定开机脚本路径保持一致。允许卸载后保留密钥以便重装。升级 VPS 代码前比对新版与现有 unit。如管理员自定义了 unit，安装器将指出差异；用 `systemctl edit drive-ssh-relay` 保存本地覆盖。发行版升级后可用 `sudo /usr/sbin/sshd -t -f /etc/drive-ssh-relay/sshd_config` 检查配置。手工改动配置后在维护窗口重启专用服务使之生效。

回退时先在 Drive 的“编辑连接”里恢复原地址并完成一次文件互验，再执行端点 `service uninstall`。DSM 另在任务计划程序中移除刚登记的开机项。VPS 执行 `sudo python3 deploy/vps/server.py uninstall`，默认保留信任资料；`uninstall --purge` 同时移除本工具写入的密钥、授权清单和专用账号。最后移除专用 hosts 项和不再使用的云防火墙规则。原业务目录不参与卸载。

一台 VPS 的 v0.1 安装管理一个 NAS 和多台桌面客户端。macOS 原型里的特定无线网卡绑定和尾网备用路由与现场网络有关，发行版跟随系统路由。
