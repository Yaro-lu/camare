# Cloudflare Tunnel 客户入口

Cloudflare 模式适合让客户设备在不安装 Tailscale 的情况下，通过 HTTPS 域名首次扫码绑定、后续直接访问。Portal 管理页仍然使用 Tailscale Serve，不通过 Cloudflare。

> Cloudflare 账号、域名、Tunnel、Published application 和套餐政策由用户自行管理。Camare 只运行连接器，不调用控制台 API，也不自动修改 DNS。

## 1. 创建 remotely-managed Tunnel

在 Cloudflare Zero Trust 中创建 Tunnel，复制 connector token。把 token 写入本地 `0600` 文件，不要粘到 INI 或 Compose：

```sh
install -m 600 /dev/null secrets/cloudflare-token
python3 -c 'from getpass import getpass; from pathlib import Path; Path("secrets/cloudflare-token").write_text(getpass("Cloudflare token: "), encoding="utf-8")'
```

## 2. 配置 INI

```ini
[camare]
provider = cloudflare

[public]
origin = https://camera.example.com
loopback_port = 9080
cloudflare_token_file = secrets/cloudflare-token

[admin]
origin = https://camera-host.your-tailnet.ts.net:9443
loopback_port = 9081
tailscale_login = owner@example.com
```

`public.origin` 必须与客户实际打开的 HTTPS origin 完全一致，不能包含路径。`admin.origin` 必须是 `*.ts.net`，登录名必须与 Tailscale Serve 注入的账号匹配。

## 3. 设置 Published application

在该 Tunnel 中创建公开主机名：

```text
Hostname: camera.example.com
Service:  http://camera-portal:8080
```

这里使用 Compose service 名，而不是宿主机 IP：`cloudflared` 与 `camera-portal` 位于同一个隔离的 `portal_ingress` 网络。`localhost` 在 cloudflared 容器里指向它自己。

严禁将 Service 指向：

- `frigate:5000`（内部免鉴权 API）；
- `frigate:1984`（go2rtc 内部 API）；
- Frigate `8971`；
- Portal 管理端 `8081`/宿主机 `9081`；
- LAN 免鉴权端口。

## 4. 配置 Tailscale 管理入口

Cloudflare 只负责客户入口。管理页按 [Tailscale 指南](tailscale.md#管理页只用-serve) 使用 Serve，并限制为 `admin.tailscale_login`。

## 5. 应用和检查

```sh
./install.sh validate --config "$PWD/camare.ini"
./install.sh doctor --config "$PWD/camare.ini"
./install.sh plan --config "$PWD/camare.ini"
sudo ./install.sh apply --config "$PWD/camare.ini" --yes
sudo ./install.sh verify --config "$PWD/camare.ini"
```

`verify` 主要验证实例内服务、回环/LAN 入口和媒体推进；它不能证明 Cloudflare 控制台的公网主机名、DNS 和套餐策略都正确。还必须从不在家庭 Wi-Fi 的客户网络手工打开 `public.origin`。

如果域名返回 Cloudflare 1033，先确认本实例的 `cloudflared` 服务在运行，再检查 Tunnel token 和控制台路由。参见 [故障排查](../troubleshooting.md#cloudflare-error-1033)。

## 风险与容量

- Tunnel token 等同于连接器凭证；泄露后立即在 Cloudflare 控制台轮换。
- Cloudflare 可能对视频、大文件、缓存和套餐使用有单独政策。Camare 不代表免费无限视频分发。
- 该结构目标是少量家庭用户偶尔查看，不是大规模 CDN。
- 不要让 Cloudflare 缓存绑定页、API 或录像；Portal 会发送 no-store 相关头，但仍应检查控制台是否添加了强制缓存规则。
- 先在目标手机的蜂窝网络完成绑定、直播和回放实测，再交付给家人。
