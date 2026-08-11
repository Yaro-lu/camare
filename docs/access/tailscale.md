# Tailscale 客户与管理入口

Tailscale 在 Camare 中有两个不同用途：

- **Serve**：只在 tailnet 内提供 Portal 管理页；
- **Funnel**：可选的公网客户入口，客户设备无需安装 Tailscale。

不要把二者混淆。管理页不能使用 Funnel。

> Camare 不执行 `tailscale up`，不登录账号，也不修改已有 Serve/Funnel。不同 Tailscale 版本的 CLI 语法和可用端口可能变化，执行前先查看 `tailscale serve --help`、`tailscale funnel --help` 和当前 `tailscale serve status`。

## 管理页只用 Serve

以实例中配置的管理回环端口为源站。例如：

```sh
sudo tailscale serve --bg --https=9443 http://127.0.0.1:9081
tailscale serve status
```

然后在 INI 中填写 Serve 返回的精确 HTTPS origin 和允许登录名：

```ini
[admin]
origin = https://camera-host.your-tailnet.ts.net:9443
loopback_port = 9081
tailscale_login = owner@example.com
```

Portal 管理端信任 Tailscale Serve 注入的身份头，并再次匹配 `tailscale_login`。因此：

- 不要把管理回环端口映射到 LAN 全地址或公网；
- 不要让 Cloudflare/Nginx/Caddy 手工伪造 Tailscale 身份头；
- Tailscale ACL 和账号保护仍然重要；
- 撤销 tailnet 设备可以阻断该设备到 Serve，但 Portal 内的客户绑定设备还应在管理页单独撤销。

`provider = lan` 会关闭 Portal 管理页面功能；需要管理页时选择 `cloudflare` 或 `tailscale`。

## Funnel 作为客户入口

没有 Cloudflare 域名时，可以把公开回环端口发布为 Funnel，例如：

```sh
sudo tailscale funnel --bg --https=8443 http://127.0.0.1:9080
tailscale funnel status
```

配置：

```ini
[camare]
provider = tailscale

[public]
origin = https://camera-host.your-tailnet.ts.net:8443
loopback_port = 9080
cloudflare_token_file =

[admin]
origin = https://camera-host.your-tailnet.ts.net:9443
loopback_port = 9081
tailscale_login = owner@example.com
```

客户打开 Funnel URL，不需要安装 Tailscale；仍需首次二维码绑定。管理页则只能从 tailnet 打开 Serve URL。

## 应用顺序

1. 先登录 Tailscale，并确认 MagicDNS/HTTPS 可用；
2. 配置 Serve 和可选 Funnel；
3. 把命令实际返回的 origin 写进 INI；
4. 运行 `validate`、`doctor`、`plan`；
5. `sudo apply` 后运行 `verify`；
6. 分别从管理员 tailnet 设备和客户蜂窝网络实测。

`verify` 不替代 Tailscale 控制平面的外部检查；Serve/Funnel 状态和真实远端访问必须单独确认。

Tailscale 模式不会在 Compose 中生成 `cloudflared`。如果客户入口不可用但 LAN 正常，检查 Funnel，而不是重启 Frigate。

## 端口和现有服务

同一台 Tailscale 节点可能已有其他 Serve 规则。先保存 `tailscale serve status` 输出作为基线，避免覆盖无关服务。若端口冲突，选择 Tailscale 支持的另一个 HTTPS 端口，并同步修改 `origin`；不要改变 Camare 的回环源站端口来“碰运气”。
