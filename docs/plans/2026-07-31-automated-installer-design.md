# 自动部署器设计

## 目标

为当前已经验证的 Frigate、Camera Portal、LAN Caddy 和可选 Cloudflare Tunnel 增加一个 Linux 交互式安装器。用户不再手工复制和同步多份 `.env`，只需回答一次摄像头、录像目录、局域网和公网入口问题。首版只自动化当前 Intel VAAPI、两路 RTSP 结构，不猜测 NVIDIA/AMD 驱动，也不修改路由器、Mihomo、防火墙或正在运行的远程服务器。

## 结构

仓库根目录新增 `deploy.py` 和 `install.sh`。Python 只使用标准库，负责验证输入、生成随机 `LAN_ACCESS_KEY`、安全写入四个 Compose 项目的 `.env`、摄像头密码和 Cloudflare token，并原子更新 Frigate 配置中的保留时间。Shell 文件只是检查 `python3` 后转交给 Python。

安装器提供正常部署、`--configure-only` 和 `--dry-run` 三种方式。正常部署要求 Linux、root、Docker Compose 和可用 Intel render device；配置模式只生成文件，便于先在 1Panel 中检查。每次覆盖已有普通配置前创建带时间戳的备份并只保留最近三份，secret 不复制；现有 Portal 状态库、Frigate 数据库和录像永不删除。

## 公网模式

- `cloudflare`：写入 token 文件并启动隔离的 cloudflared Compose；管理端仍使用 Tailscale Serve。
- `tailscale`：配置 Serve 管理入口和 Funnel 客户入口。
- `lan`：使用保留的 `.invalid` 域名，仅启动局域网观看，二维码和管理端暂不可用。

## 验证

单元测试覆盖输入校验、环境文件转义、密钥复用、Frigate 保留天数更新和 secret 隔离。CI 继续执行 Go 测试、竞态测试、Shell/Python 语法检查和四套 Compose 渲染。安装器不把秘密写入命令行或日志，最终摘要只显示非敏感地址。
