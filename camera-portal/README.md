# Camera Portal

`camera-portal` 是本项目的轻量观看门户。它不解码、不转码、不复制录像，只在通过鉴权后代理 Frigate/go2rtc 的固定直播与回放接口。

## 功能边界

- 公网设备通过一次性二维码绑定，之后使用一年有效、自动续期的安全 Cookie。
- 管理页面只监听宿主机回环地址，设计为通过 Tailscale Serve 访问。
- 局域网入口由独立 Caddy 注入内部密钥，可免二维码直接观看。
- 默认显示 `ptz_view`、`fixed_view` 两路画面。
- 管理页按物理设备分组展示一次性低清快照，并预留未接入摄像头位置。
- 每组可统一选择新录像是否包含声音；多个改动只在点击“应用并重启”后一次生效。
- 直播和回放互斥；切换模式或页面进入后台时释放原来的播放器。
- 全局最多八条直播，单设备最多两条；每条连接使用十五分钟租约。
- 录像由 Frigate 读取，Portal 不挂载 `/media/frigate`。

## 配置

```sh
cp .env.example .env
```

必须修改：

| 变量 | 说明 |
|---|---|
| `PUBLIC_BASE_URL` | 唯一公网 HTTPS 域名，也是二维码和 Cookie 所属域名 |
| `LOCAL_BASE_URL` | 与 LAN Caddy 完全一致的 HTTP origin |
| `LAN_ACCESS_KEY` | 两个编排共用的随机内部密钥 |
| `ADMIN_ORIGIN` | Tailscale Serve 管理入口 |
| `ADMIN_TAILSCALE_LOGIN` | 唯一允许管理设备的 tailnet 登录名 |
| `CAMERAS` | `Frigate摄像头ID=显示名称`，多个用逗号分隔 |
| `CAMERA_GROUPS` | 管理页分组，格式为 `组ID=组名:槽1|槽2;...`；空槽保留为“待接入” |

默认分组示例：

```dotenv
CAMERA_GROUPS=yard=院子:ptz_view|fixed_view;front=门前:|
```

每组固定两个位置。组内非空摄像头 ID 必须已经出现在 `CAMERAS` 中，不能用分组配置添加任意 Frigate 源。

启动前需要存在两个外部 Docker 网络：

```sh
docker network create camera_portal_tunnel
```

`frigate_default` 会由 Frigate 编排创建。然后：

```sh
mkdir -p data
chown 65532:65532 data
chmod 750 data
chmod 600 .env
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

公网监听器仅发布到 `127.0.0.1:9080`，管理监听器仅发布到 `127.0.0.1:9081`。不要把管理监听器交给 Cloudflare Tunnel 或 Tailscale Funnel。

管理页的“录制声音”滑块只保存待应用值。点击“应用并重启”会统一修改 Frigate 的固定录像预设，并使所有直播和录像短暂中断，通常约 15–60 秒；它只影响随后产生的新录像，历史文件不会重写。公开观看页的双画面回放始终默认静音，只有用户进入单画面全屏并手动点声音按钮后，当前录像才可发声。

## 持久化

唯一的 Portal 状态文件是：

```text
data/state.json
```

它保存绑定设备资料和令牌哈希。公开仓库不包含该文件；需要完整恢复原有绑定设备时，应单独加密备份整个 `data/` 目录。

## 验证

```sh
docker inspect -f '{{.State.Health.Status}}' camera-portal
curl -fsS http://127.0.0.1:9080/healthz
docker compose logs --tail=100 camera-portal
```

完整部署顺序、安全边界和公网入口配置见仓库根目录的 `README.md`。
