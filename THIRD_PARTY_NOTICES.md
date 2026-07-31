# Third-party notices

本仓库编排或在构建时下载以下上游项目。它们不归本仓库作者所有，各自适用自己的许可证和商标规则：

- [Frigate NVR](https://github.com/blakeblackshear/frigate) — 官方容器镜像；代码采用 MIT License。`Frigate` 名称和标志受其商标政策约束。
- [Caddy](https://github.com/caddyserver/caddy) — 局域网反向代理容器。
- [hls.js](https://github.com/video-dev/hls.js) — Portal 构建时固定版本下载。
- [jsmpeg-player](https://github.com/cycjimmy/jsmpeg-player) — iOS 直播播放器依赖。
- [go-qrcode](https://github.com/skip2/go-qrcode) — Go 二维码生成依赖。
- [cloudflared](https://github.com/cloudflare/cloudflared) — 可选 Cloudflare Tunnel 连接器。
- [Tailscale](https://tailscale.com/) — 可选公网和管理入口。

仓库不重新分发 Frigate 源码或镜像 tar；Docker 根据 Compose 从官方镜像仓库拉取固定版本。使用、修改或再发布前，请核对对应上游版本的许可证、NOTICE 和商标要求。
