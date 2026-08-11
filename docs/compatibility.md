# 兼容性矩阵

本页描述当前声明式部署路径的边界，不代表 Frigate 本身的全部能力。

> 验证状态：本地配置、渲染、事务、Portal 和策略自动化测试已通过；Linux 隔离 E2E、真实摄像头矩阵和正式 Release 验收待执行。

## 主机

| 项目 | 状态 | 说明 |
|---|---|---|
| Linux x86-64 | 支持 | `doctor` 会拒绝其他系统/架构 |
| Python 3.10+ | 支持 | CLI 只使用 Python 标准库 |
| Docker Engine + Compose v2 | 支持 | `docker compose` v1 不支持 |
| 1Panel | 可选 | 只作为生成后的单一 Compose 管理界面 |
| ARM / ARM64 | 不支持 | 当前镜像、检测和发行测试未覆盖 |
| Windows / macOS 主机 | 不支持部署 | 可作为观看客户端；不能通过 `doctor` 主机检查 |

## 摄像头和画面规模

| 项目 | 状态 | 说明 |
|---|---|---|
| 标准 RTSP | 支持 | 服务器必须能直接 TCP 访问摄像头 RTSP 端口 |
| H.264 视频 | 支持 | 当前 `doctor` 和浏览器链路的发行目标 |
| H.265 / HEVC | 不支持 | 即使个别浏览器偶然可播，也不属于当前兼容范围 |
| 1–2 个组 | 支持 | 一个组通常对应一台物理设备 |
| 每组 1–2 路 | 支持 | 全局 stream ID 必须唯一 |
| 超过 4 路 | 不支持 | 当前配置模型和客户端资源限制会拒绝 |
| ONVIF 自动发现 | 不支持 | 需要明确填写 IP/主机名、端口和 RTSP 路径 |

客户页面只创建当前组最多两路播放器。切组会释放旧组连接；它不会同时展示四路画面。

## 解码硬件

| 模式 | 状态 | 注意事项 |
|---|---|---|
| CPU | 支持 | 不映射 `/dev/dri`；旧 CPU 或高码率多路可能负载较高 |
| Intel VAAPI | 支持 | 需要可读写的 `/dev/dri/renderD<number>` 和 `i965` 或 `iHD` |
| AMD VAAPI | 不支持 | 当前校验只接受 Intel 发行路径 |
| NVIDIA | 不支持 | 当前没有 NVIDIA runtime/驱动模板 |

硬件解码只降低视频解码负担，不能替代摄像头、磁盘、网络或散热问题的诊断。

## 音频

- 所有组默认 `record_audio = false`，新录像不保留音轨。
- 对某组设置 `record_audio = true` 后，该组新录像使用带 AAC 音频的录制 preset；摄像头 RTSP 本身必须提供可用音频。
- 开关不为历史文件补录或删除音轨。
- 管理页滑块会立即更新当前 Frigate 配置并触发一次重启；`camare.ini` 仍决定下一次 `apply`。希望长期保留时，应把同一值同步到对应组的 `record_audio`。
- 开启前应确认家庭成员告知、隐私和当地法律要求。
- 不同摄像头音频编码组合尚未完成真实设备矩阵 E2E，首次启用后必须检查新片段。

## 存储

| 类型 | 状态 | 说明 |
|---|---|---|
| 本地现有目录 | 支持 | 必须非根目录、存在、可读写且空间充足 |
| 已挂载 NAS/NFS/SMB 目录 | 条件支持 | 挂载、重连、缓存和权限由操作系统负责；`doctor` 不证明 mount 身份；隔离 E2E 待执行 |
| Docker 命名卷 | 当前不支持配置 | `storage.media` 接受主机绝对路径 |
| 自动格式化/挂载/搬迁 | 不支持 | Camare 不操作磁盘分区、fstab 或复制录像 |

Frigate 和 Portal 直接读取同一个媒体目录，录像只有一份。`retention` 表示 Frigate 连续录像保留天数，不是磁盘容量配额。

## 入口提供商

| `provider` | 客户入口 | 管理入口 | 额外要求 |
|---|---|---|---|
| `lan` | LAN IPv4，免鉴权 | 关闭 | 可信家庭网 |
| `cloudflare` | Cloudflare Tunnel | Tailscale Serve | 域名、remotely-managed Tunnel token、Tailscale 登录 |
| `tailscale` | Tailscale Funnel | Tailscale Serve | Funnel 可用、Tailscale 登录 |

Cloudflare 和 Tailscale 模式都会保留 LAN 入口。公网客户入口二选一；不要让两个域名同时成为日常绑定入口。

## 浏览器

Portal 代码面向现代 iPhone/iPad Safari、Android Chromium 以及 Windows/macOS 常见现代浏览器。浏览器后台暂停、自动播放、低电量策略和 H.264 profile 支持由具体系统决定。当前新发行栈的跨设备真实 E2E 尚待执行，因此正式交付前应在目标手机上测试直播、全屏、日期/时间轴回放和后台恢复。
