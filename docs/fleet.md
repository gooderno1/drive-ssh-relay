# 自建 Drive Relay 设备管理台

管理台集中维护设备、线路和公钥授权。现有 NAS 回连和 Drive 的同步任务可以继续使用。Web 管理、设备上报和 SSH 文件传输有各自的端口与后台进程。

打开设备的线路策略，可选择自动、手动 VPS 或手动 NAS 直连。后两项固定路径；手动直连验证不通过时保持断开并报告原因。只有上报支持手动直连的桌面版本才可远程选择该项。保存后看设备的策略确认版本、实际线路和 Drive 连接数。

## VPS 运行条件

- Debian 或 Ubuntu，已有可用的 NAS 回连，安装发行版 OpenSSH Server、Nginx、Python 3.9+、Certbot。
- 为管理域名设置 DNS 记录并取得 HTTPS 证书。部署器使用 `/etc/letsencrypt/live/<域名>/`。选择证书验证方式时核对已有的端口与 Web 服务。
- 为设备 SSH 选择一个空闲端口。管理台默认使用标准 HTTPS 443，管理 API 只监听 VPS 回环地址。443 已由同步 SSH 占用时，可以按下方「与 SSH 共用 443」部署。

## 安装

把当前发行版解压到 `/opt/drive-relay-fleet/releases/<版本>/` 的 root 所有目录。以下命令在该目录运行，示例地址和密钥路径请按实际部署替换。

```sh
python3 deploy/fleet/package_client.py client.zip
sudo python3 deploy/vps/server.py invite --host relay.example.net --lan-ip 192.168.1.20 --nas-name nas.example.net | sudo tee /root/drive-invite.json >/dev/null
```

家用参数与 NAS 证书名称来自已验证的接入文件。在新管理入口与原中继并行的部署中，将该文件的 `ssh_port` 设置为新设备 SSH 端口；保留主机公钥和现有 NAS 回环桥端口。核对命令所在的 shell 对输出文件有写入权限。

```sh
sudo python3 deploy/fleet/install.py --domain drive.example.net --https-port 443 --invite /root/drive-invite.json --host-key /etc/drive-ssh-relay/host_ed25519
sudo -u drive-fleet python3 -m drive_relay.fleet password --data /var/lib/drive-relay-fleet
```

在 VPS 的交互终端设置管理员密码，随后用浏览器打开 `https://drive.example.net/`。网页设密和命令行重置均接受 8 至 256 个字符，至少包含一个字母和一个数字。部署工具也可以通过 `issue-setup` 登记一次性设密代码，私下向管理员提供含 `#activate=<代码>` 的入口。有效期为 24 小时。重置密码会使原管理员会话失效。

## 与 SSH 共用 443

新部署优先将 HTTPS 放在 443、设备 SSH 放在独立端口。已有设备依赖 SSH 443 且 VPS 只有一个公网 IPv4 地址时，可使用仓库随附的 HAProxy 配置。该入口支持发起 SSH 标识的客户端，也支持等待服务器先打招呼的客户端。原有 SSH 服务仍执行公钥认证和端口转发限制。

1. 核对公网 IP、SSH 监听和已有连接，并保留独立管理通道。给原专用 sshd 增加仅回环的监听，例如 `127.0.0.1:19444`。用 `sshd -t -f <配置>` 检查，向原监听进程发送 HUP 后核对主机公钥。若部署依赖 SSH 客户端源 IP 白名单，先设计保留源 IP 的入口或使用独立公网地址；普通 TCP 代理会使 sshd 看到代理地址。
2. 将 `deploy/fleet/shared-https.conf.in` 的 `@DOMAIN@` 换成真实域名，作为 `/etc/drive-relay-fleet/shared-https.conf` 纳入专用 Nginx 的 http 配置。这个回环 TLS 入口通过 PROXY v2 接收 HTTPS 客户端地址。将 `shared-443.cfg.in` 中的公网 IP 和 SSH 回环地址替换后保存为 `/etc/drive-relay-fleet/shared-443.cfg`。发行版 HAProxy 与专用 systemd 模板 `drive-relay-frontdoor.service.in` 管理公网入口。
3. 先让测试 HAProxy 绑定一个备用回环端口，分别验证 HTTPS 证书和 SSH 主机公钥。再释放原公网监听，启用专用 HAProxy 的公网 443。切换仅作用于新建 TCP 连接。开放所需公网端口，保留回环后端的本地绑定。
4. 原 8443 入口升级时向安装器传入 `--https-port 8443 --public-https-port 443`。安装器保留共享配置、新设备使用标准域名，旧浏览器入口跳转，旧设备的上报 API 继续接受原地址。用浏览器、既有设备上报和固定 SSH 身份分别验收；如失败，先恢复原 SSH 的公网监听及管理台原先的对外地址。

## 日常操作

1. **加设备。** 登录管理台，下载客户端并为新电脑生成配对码。独立配对码限一台设备、15 分钟有效。在新电脑解压文件，运行 Windows 或 macOS 安装器，打开本机工作台并配对。管理员展开设备的「SSH 公钥与设备权限」授权后，在新机的「本机服务」安装并启动路由。Synology Drive 保留证书匹配的固定入口。[首次设置 Drive 地址](../README.md)
2. **看运行状态。** 设备详情显示最近一次上报、实际传输线路和配置版本。设备一般每 45 秒上报一次。管理员保存策略后，页面先显示等待确认，随后显示设备已保存的版本。新版路由服务运行时执行策略；已登记的旧中继继续按其现有配置转发。
3. **撤销授权。** 撤销 SSH 公钥授权会拒绝该密钥在新入口的下一次认证。已有 SSH 会话在断开时结束。停用设备还会撤销它的管理凭证。有独立旧 SSH 入口的部署应在该入口单独管理原来的授权。
4. **升级与迁移。** 同一 VPS 将新发行版放在新的 root 所有目录，生成客户端包，再用相同域名、端口与接入文件运行安装。备份 `/var/lib/drive-relay-fleet/`（SQLite 使用在线备份或停管理 API 后复制）和 `/etc/drive-relay-fleet/`，按敏感运维材料保存。在新 VPS 上准备证书、NAS 回连、同一已核验 SSH 主机身份或重新分发可信接入文件，然后恢复设备数据并校验设备登录、上报、SSH 认证及双向文件。在并行验证通过后切 DNS。[更换 VPS](operations.md#replace-vps)
