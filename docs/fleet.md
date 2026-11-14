# 自建 Drive Relay 设备管理台

管理台集中维护设备、线路和公钥授权。现有 NAS 回连和 Drive 的同步任务可以继续使用。Web 管理、设备上报和 SSH 文件传输有各自的端口与后台进程。

## VPS 运行条件

- Debian 或 Ubuntu，已有可用的 NAS 回连，安装发行版 OpenSSH Server、Nginx、Python 3.9+、Certbot。
- 为管理域名设置 DNS 记录并取得 HTTPS 证书。部署器使用 `/etc/letsencrypt/live/<域名>/`。选择证书验证方式时核对已有的端口与 Web 服务。
- 为设备 SSH 选择一个空闲端口。管理台默认使用 HTTPS 8443。只有 443 空闲时才选择 HTTPS 443。云防火墙和系统防火墙需要放行选定的两个入口。管理 API 本身只监听 VPS 的回环地址。

## 安装

把当前发行版解压到 `/opt/drive-relay-fleet/releases/<版本>/` 的 root 所有目录。以下命令在该目录运行，示例地址和密钥路径请按实际部署替换。

```sh
python3 deploy/fleet/package_client.py client.zip
sudo python3 deploy/vps/server.py invite --host relay.example.net --lan-ip 192.168.1.20 --nas-name nas.example.net | sudo tee /root/drive-invite.json >/dev/null
```

家用参数与 NAS 证书名称来自已验证的接入文件。在新管理入口与原中继并行的部署中，将该文件的 `ssh_port` 设置为新设备 SSH 端口；保留主机公钥和现有 NAS 回环桥端口。核对命令所在的 shell 对输出文件有写入权限。

```sh
sudo python3 deploy/fleet/install.py --domain drive.example.net --https-port 8443 --invite /root/drive-invite.json --host-key /etc/drive-ssh-relay/host_ed25519
sudo -u drive-fleet python3 -m drive_relay.fleet password --data /var/lib/drive-relay-fleet
```

在 VPS 的交互终端设置管理员密码，随后用浏览器打开 `https://drive.example.net:8443/`。网页设密和命令行重置均接受 8 至 256 个字符，至少包含一个字母和一个数字。首次设密和后续密码重置也可以由部署工具通过 `issue-setup` 的标准输入登记高熵一次性代码，经私有渠道给管理员提供含 `#activate=<代码>` 的本机入口。有效期为 24 小时。重置密码会使原管理员会话失效。

## 日常操作

1. **加设备。** 登录管理台，下载客户端并为新电脑生成配对码。独立配对码限一台设备、15 分钟有效。在新电脑解压文件，运行 Windows 或 macOS 安装器，打开本机工作台并配对。管理员展开设备的「SSH 公钥与设备权限」授权后，在新机的「本机服务」安装并启动路由。Synology Drive 保留证书匹配的固定入口。[首次设置 Drive 地址](../README.md)
2. **看运行状态。** 设备详情显示最近一次上报、实际传输线路和配置版本。设备一般每 45 秒上报一次。管理员保存策略后，页面先显示等待确认，随后显示设备已保存的版本。新版路由服务运行时执行策略；已登记的旧中继继续按其现有配置转发。
3. **撤销授权。** 撤销 SSH 公钥授权会拒绝该密钥在新入口的下一次认证。已有 SSH 会话在断开时结束。停用设备还会撤销它的管理凭证。有独立旧 SSH 入口的部署应在该入口单独管理原来的授权。
4. **升级与迁移。** 同一 VPS 将新发行版放在新的 root 所有目录，生成客户端包，再用相同域名、端口与接入文件运行安装。备份 `/var/lib/drive-relay-fleet/`（SQLite 使用在线备份或停管理 API 后复制）和 `/etc/drive-relay-fleet/`，按敏感运维材料保存。在新 VPS 上准备证书、NAS 回连、同一已核验 SSH 主机身份或重新分发可信接入文件，然后恢复设备数据并校验设备登录、上报、SSH 认证及双向文件。在并行验证通过后切 DNS。[更换 VPS](operations.md#replace-vps)
