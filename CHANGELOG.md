# Changelog

本文件记录项目面向公开发行的用户可见变更。版本号遵循语义化版本。

## [Unreleased]

暂无未发布变更。

## [0.1.0]

发布日期：2026-08-11

### Added

- 添加 MIT License，为公开使用、修改和分发提供明确许可。
- 添加仓库发行策略测试，防止安全默认值和依赖固定策略回退。
- 添加声明式 `camare.ini` 部署路径，以及 `init`、`validate`、`doctor`、`plan`、`apply`、`verify` 和 `status` 命令。
- 添加实例级备份、dry-run 恢复和安全卸载生命周期能力；录像与 secret 内容不进入应用状态归档。
- 添加公开 Quickstart、兼容性、安全、入口、升级、备份/恢复、回滚、卸载、排错和 legacy 迁移文档。
- 添加可信归档校验、SQLite 一致性快照、POSIX 元数据恢复和失败恢复副本保留测试。
- 添加 amd64/arm64 Portal 与 LAN Caddy GHCR 镜像、SPDX SBOM、构建来源证明、确定性源码归档和 SHA256 校验和发布流程。

### Changed

- Frigate 公开模板默认不录制声音；用户仍可在管理页明确开启新录像音轨。
- Cloudflared 使用固定版本与镜像摘要，不再跟随可变的 `latest` 标签。
- Compose 资源允许按项目、镜像、网络和端口配置，不再声明固定容器名称。
- Portal 与 LAN Caddy 默认使用普通 Docker 构建网络；宿主机网络改为显式选项。
- 新部署生成单一 `camare_<instance-id>` Compose project，运行目录统一为 `/opt/1panel/docker/compose/camare-<instance-id>`；录像继续直接绑定用户指定目录，不复制。
- 客户页面只加载当前摄像头组，最多同时拉取两路画面。
- Frigate 配置明确标记为受控可变文件，使管理页录音开关与 `status`、备份、回滚和后续声明式应用兼容。
- `deploy.py` 明确标记为 legacy，不再作为新安装的推荐入口。

### Security

- 降低误录家庭环境声音和上游镜像静默变化的风险。
- 分离媒体后端与公网入口 Docker 网络，Tunnel/LAN 代理不能直接访问 Frigate 内部 API。
- 输入密码和 Tunnel token 只从权限收紧的独立文件读取；计划、manifest 和普通输出不包含 secret 值。
- LAN 免鉴权入口限定到明确的家庭网 IPv4；公网模式的管理页继续限定为 Tailscale Serve 身份。
- 恢复会保留现有目标文件的 POSIX 所有权和权限；恢复或 purge 无法完整回滚时保留唯一恢复副本，不再清理它。
- 按 Compose 本地 file secret 的 bind-mount 语义固定运行密钥所有权：摄像头密码保持 root-only，Cloudflare token 保持 cloudflared owner-only，LAN 代理 key 只对两个指定消费容器的 GID 可读。

### Verification status

- GitHub Actions 的 Python、Go、Node、配置渲染、依赖审查、完整历史秘密扫描和 HIGH/CRITICAL 镜像漏洞门禁全部通过。
- Linux x86-64 / CPU / 两路合成 H.264 的隔离 Docker E2E 已通过；真实摄像头、Intel VAAPI、NAS 重连、ARM 整栈和跨手机浏览器矩阵仍需分别验收。
