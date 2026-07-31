# Camera Portal 局域网入口

该编排提供独立的家庭局域网入口。打开后直接进入观看页，不要求二维码或 Cookie。

```text
家庭 Wi-Fi 浏览器
  → http://<LAN_BIND_IP>:<LAN_PORT>
  → LAN Caddy
  → camera_portal_tunnel
  → camera-portal
  → Frigate/go2rtc
```

局域网 Caddy 不加入 Frigate 网络，不能直接访问摄像头、Frigate API 或录像目录。

## 配置

```sh
cp .env.example .env
```

修改：

```dotenv
LAN_BIND_IP=192.168.1.250
LAN_PORT=8088
LAN_ACCESS_KEY=<与 camera-portal/.env 完全相同的随机密钥>
PORTAL_INGRESS_NETWORK=camera_portal_tunnel
```

`LAN_BIND_IP` 必须是服务器真实拥有的家庭网 IPv4。端口映射必须保留精确 IP：

```text
192.168.1.250:8088:8080
```

不能简写成 `8088:8080`，否则可能同时监听 Tailscale、公网 IPv6 或其他网卡。

## 启动与验证

先启动 Frigate 和 Camera Portal，再启动本编排：

```sh
chmod 600 .env
docker compose config --quiet
docker compose up -d --build
./scripts/verify.sh
```

家庭 Wi-Fi 浏览器打开：

```text
http://<LAN_BIND_IP>:<LAN_PORT>/
```

应直接显示两路监控，管理页设备数量不会增加。

## 安全边界

任何能进入该家庭局域网并访问服务器的人都可以观看监控。这是有意设计的免鉴权入口，因此：

- 只能绑定家庭网 IPv4；
- 不要做路由器端口转发；
- 不要用 Cloudflare/Tailscale 把该端口发布到公网；
- 公网用户继续使用 Camera Portal 的二维码绑定入口。
