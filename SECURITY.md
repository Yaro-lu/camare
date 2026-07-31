# 安全说明

## 支持范围

这是家庭自托管项目，不提供托管服务或安全响应 SLA。发现问题时请不要在公开 Issue 中粘贴摄像头密码、Cloudflare token、Tailscale auth key、绑定二维码、Cookie 或完整日志。

## 不应提交到 GitHub 的内容

- 任意真实 `.env`
- `camera-portal/data/state.json`
- `frigate-1panel/secrets/`
- `frigate-1panel/config/frigate.db*`
- Cloudflare Tunnel token
- Tailscale auth key/OAuth secret
- 摄像头账号和密码
- Frigate 首次管理员密码
- 录像、截图和包含家庭地址的画面

即使仓库设为 private，也不建议把长期密钥直接提交到 Git。

## 网络边界

公网连接器只允许访问 `camera-portal:8080` 或宿主机 `127.0.0.1:9080`。

禁止公网暴露：

- Frigate `5000` 内部免鉴权 API
- go2rtc `1984`
- RTSP `8554`
- Portal 管理端 `9081`
- LAN 免鉴权端口 `8088`

Frigate `8971` 启用了认证，但仍建议只绑定局域网或 Tailscale 地址。

## 局域网免鉴权

LAN Caddy 使用随机内部密钥证明请求来自固定代理，但最终用户没有账号体系。任何能够访问该家庭网地址的设备都能看监控。请使用可信 Wi-Fi、访客网络隔离和精确 IPv4 绑定。

## 密钥轮换

以下情况应立即轮换对应密钥：

- token 曾粘贴到聊天、截图、Issue 或公开日志；
- 仓库误提交了 `.env`、secret 或运行状态；
- 服务器、手机或管理员账号丢失；
- Cloudflare/Tailscale 控制台出现未知连接；
- 摄像头被重置或移交他人。

轮换 `LAN_ACCESS_KEY` 时必须同时更新 Camera Portal 和 LAN Caddy。

## 更新

Frigate、Caddy、Go、hls.js、JSMPEG 和 cloudflared 都属于外部依赖。升级前阅读 release notes、备份状态并在局域网验证。不要让生产服务器无审查地自动跟随可变 `latest`/`stable` 标签。
