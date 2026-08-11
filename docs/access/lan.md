# 局域网入口

LAN 入口始终由实例内的 `lan-caddy` 提供，地址为：

```text
http://<lan.bind_ip>:<lan.port>/
```

它直接走家庭局域网，不经过 Cloudflare Tunnel 或 Tailscale。公网是否可用不会影响这个入口。

## 配置

```ini
[lan]
bind_ip = 192.168.1.250
port = 8088
```

`bind_ip` 必须是服务器真实拥有的 RFC1918 IPv4。建议在主路由器上按服务器网卡 MAC 做 DHCP 地址保留；Camare 不会修改 DHCP/DNS，也不会扫描或改动摄像头网络。

应用配置：

```sh
./install.sh validate --config "$PWD/camare.ini"
./install.sh doctor --config "$PWD/camare.ini"
./install.sh plan --config "$PWD/camare.ini"
sudo ./install.sh apply --config "$PWD/camare.ini" --yes
sudo ./install.sh verify --config "$PWD/camare.ini"
```

## 身份边界

LAN 页面免鉴权。内部随机 key 只用于证明请求经过固定的 LAN Caddy，不是终端用户的登录机制：

- 家庭网设备不需要二维码、Cookie 或账号；
- LAN 和公网访问路径不同，但不会删除公网设备绑定状态；
- 任何能够连接这个 IPv4/端口的设备都能观看；
- 访客 Wi-Fi、被入侵的 IoT 设备和错误的 VLAN 路由都可能扩大访问面。

因此必须：

- 只在可信家庭网使用；
- 精确绑定服务器 LAN IPv4，不能改成 `0.0.0.0`、`[::]` 或只写端口；
- 不做公网端口转发；
- 不把这个端口交给 Cloudflare Tunnel、Tailscale Funnel 或反向代理公网域名；
- 需要访客隔离时在路由器/VLAN 层实施，而不是在 Camare 中猜测网络拓扑。

## `provider = lan`

最小部署可以使用：

```ini
[camare]
provider = lan

[public]
origin =
cloudflare_token_file =

[admin]
origin =
tailscale_login =
```

此模式会关闭 Portal 管理页面功能，仅保留 LAN 观看和 Frigate 自身管理入口。如果需要二维码设备管理，选择 `cloudflare` 或 `tailscale`，并让 Portal 管理页走 Tailscale Serve。

## 验证

先在服务器运行：

```sh
sudo ./install.sh verify --config "$PWD/camare.ini"
```

再从同一 Wi-Fi 的手机打开 LAN 地址，测试直播、回放和全屏。服务器端成功不能代替家庭 Wi-Fi 客户端实测。
