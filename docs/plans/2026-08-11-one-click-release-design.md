# Camare 一键部署发行版设计

## 目标和边界

首个公开发行版面向 Linux x86-64、H.264 RTSP、Intel VAAPI 或 CPU 回退，最多两组物理设备、每组最多两路画面。它保留现有 Frigate、go2rtc、Camera Portal、LAN Caddy、Cloudflare Tunnel/Tailscale 的结构，但把家庭服务器专用快照改造成可重复安装、可验证、可安全更新的发行版。

首版不自动修改路由器、DHCP、DNS、防火墙、IPv6、磁盘分区、fstab、Cloudflare 账号或 Tailscale 登录身份。安装器只验证用户已经准备好的录像挂载点，不格式化磁盘，也不复制录像。Cloudflare Published application 仍由用户在控制台创建；Tailscale 登录仍由用户授权。

生产服务器不作为开发环境。所有代码改动在独立 Git worktree 完成。只有命名空间隔离、随机回环端口和模拟 RTSP 测试能力全部就绪后，才允许在服务器创建 `camaretest_<run-id>` 测试栈；测试不得连接生产摄像头、录像目录、Tunnel token 或 Tailscale Serve/Funnel。

## 发行架构

源码 checkout 只保存模板和程序，运行文件生成到 `/opt/1panel/docker/compose/camare-<instance-id>/`。`runtime_dir` 表示这个完整实例目录，而不是它的父目录。该目录包含单一 Compose 编排、`.env`、Frigate 配置、Docker secrets、Portal 状态和安装清单；录像仍直接绑定到用户指定目录，只有一份。这样 `git pull --ff-only` 不再与运行配置冲突，1Panel 也只需导入一个生成后的编排目录。

CLI 使用 Python 标准库，入口为 `python3 -m camarectl`，并保留 `install.sh` 与旧 `deploy.py` 的兼容提示。声明式配置采用 INI：部署段描述实例、端口、硬件、存储和入口；每个 `[group:<id>]` 描述一台物理设备及其一到两路 RTSP。密码和 Tunnel token 只从权限为 `0600` 的独立文件读取，不允许写入命令行、配置摘要或 Git。

Compose 不使用固定 `container_name`，所有资源由 `camare_<instance-id>` 项目命名空间管理。Frigate、Portal、LAN Caddy 和可选 cloudflared 位于同一项目网络；Cloudflare 源站服务名仍为 `camera-portal:8080`。Intel 设备通过显式 override 加入，CPU 回退不映射 `/dev/dri`。镜像使用固定版本；正式 Release 由 GitHub Actions 构建 GHCR Portal/Caddy 镜像，本地构建只作为回退。

## 安装与状态流

`doctor` 是只读检查：架构、内存、Docker/Compose、监听端口、服务器 IP、录像挂载与可写性、磁盘余量、VAAPI 设备、摄像头 TCP/RTSP/H.264（存在 ffprobe 时）和可选 Tailscale 状态。检查输出区分 `PASS`、`WARN`、`FAIL`；危险前置条件失败时 `apply` 不继续。

`plan` 解析配置并在临时目录渲染完整编排，运行 `docker compose config`，只显示服务、端口、目录和将发生的变化，不显示秘密。`apply` 获取实例锁，先把新文件写入暂存目录并完成校验，再原子替换运行文件；保存上一个 manifest 和配置快照。启动顺序为 Frigate、Portal/LAN、可选公网连接器，失败只回滚本实例刚修改的文件和容器，不执行全局 prune 或宽泛 `down -v`。

`verify` 不只检查 `/healthz`：还检查 Compose 健康、Frigate `/api/stats` 每路采集进程、go2rtc producer、最新录像片段持续推进、Portal 公网源站和 LAN 入口。`status` 汇总这些结果。升级默认只更新 Portal/LAN；Frigate 版本变化必须显式确认并连同数据库配置备份。卸载默认保留录像、配置、Portal 状态和 Frigate 数据库。

## 两组画面与资源约束

Frigate/go2rtc 仍只有一个实例。每路摄像头只建立一个 go2rtc 上游，录像从 restream 写入同一媒体目录。每组的 `record_audio` 决定生成 `preset-record-generic` 或 `preset-record-generic-audio-aac`；公开模板默认不录音，启用时提示隐私与当地合规责任。

Portal 的 `/api/config` 返回摄像头组。客户端顶部显示已接入组标签，仅创建当前组最多两路播放器；切换组时立即停止上一组直播、回放、HLS 请求、JSMPEG/WebRTC 连接和录像索引请求，再加载新组。单组部署不显示多余标签，空的预留组只出现在管理页，不出现在客户观看页。这样四路配置不会让一个绑定设备占用四个直播槽。

## 测试和发布

单元测试覆盖 INI 校验、路径安全、密钥引用、Frigate/Compose 渲染、幂等写入、端口冲突、失败回滚和 manifest。Go/Node 测试覆盖公开组配置、切组资源释放、仅当前组请求直播/回放以及原有时间轴和回放声音行为。

CI 运行 Python、Go、Node、Shell 语法、Compose 渲染和实际 Docker build；发布前运行 secret scan、依赖/镜像漏洞扫描并生成 SBOM。合成 H.264 RTSP E2E 覆盖首次安装、重复 apply、两组四路、CPU 与 Intel 配置渲染、媒体验证和精确清理。正式版本从 `v0.1.0` 起提供 GitHub Release、变更记录、校验和和固定镜像引用。

服务器隔离验收前后记录生产容器 ID、镜像、restart count、端口、网络、文件哈希、Tailscale Serve JSON、Frigate stats 和最新录像时间。验收后这些值必须一致且不存在任何 `camaretest_*` 资源；否则停止并报告，不擅自修复生产。
