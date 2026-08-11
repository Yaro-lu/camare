# 安全指南

Camare 是家庭自托管项目，不是托管安全服务。它把公网客户入口、Tailscale 管理入口、LAN 免鉴权入口和 Frigate 内部接口分成不同边界；部署者仍需保护服务器、Docker、家庭网和上游账号。

## 网络边界

生成的实例使用两个 project-local Docker bridge：

- `media_backend`：Frigate 与 Camera Portal；
- `portal_ingress`：Camera Portal、LAN Caddy 和可选 cloudflared。

`cloudflared` 与 LAN Caddy 不加入 `media_backend`，不能直接按 service name 访问 Frigate。宿主机入口必须保持：

| 入口 | 允许绑定 | 禁止做法 |
|---|---|---|
| Portal 公网源站 | `127.0.0.1:<public.loopback_port>` | 直接监听 LAN/公网地址 |
| Portal 管理源站 | `127.0.0.1:<admin.loopback_port>` | Cloudflare/Funnel/端口转发 |
| LAN 页面 | 精确 RFC1918 IPv4 | `0.0.0.0`、`[::]`、公网转发 |
| Frigate 管理页 | 精确 LAN 或 Tailscale IPv4 | 公网发布 |
| Frigate `5000` | Docker 内部 | 任何宿主机/公网发布 |
| go2rtc `1984`/RTSP `8554` | Docker 内部 | 任何宿主机/公网发布 |

LAN 入口的内部 key 只认证固定反向代理，不认证最终用户。任何能访问 LAN 地址的设备都可以看画面。使用可信 Wi-Fi、访客网络隔离和准确的路由/VLAN 规则。

## 管理身份

公网模式下，Portal 管理页只信任 Tailscale Serve 注入的登录身份，并匹配 `admin.tailscale_login`。不要：

- 把管理端交给 Tailscale Funnel；
- 通过 Cloudflare Tunnel 发布管理端；
- 在普通反向代理中手工添加 `Tailscale-User-Login`；
- 把管理回环端口改成全地址监听。

Tailscale ACL、账号 MFA、节点撤销和设备安全仍由 tailnet 管理员负责。Portal 绑定设备的撤销是另一层操作，二者不能互相替代。

## 密钥

以下内容不能提交到 Git、Issue、聊天、截图或公开日志：

- 摄像头用户名/密码和完整 RTSP URL；
- Cloudflare Tunnel token；
- 生成运行目录中的 `secrets/`；
- 运行目录中的 `.camare-backups/`（升级回滚快照可能包含旧运行 secret）；
- `camare.ini` 的真实网络拓扑和密钥文件路径（公开求助前应脱敏）；
- Portal `state.json`、绑定二维码、Cookie；
- Frigate 管理员密码、认证数据库；
- 包含家庭画面、地址或 IPv6 的录像/日志。

输入密钥文件必须是普通文件且权限为 `0600`；目录建议 `0700`。不要把密码放进命令行，因为它可能进入 shell history 或进程列表。Docker group 等同于主机 root 级权限，不要为了免 `sudo` 把不可信用户加入该组。

Compose 的本地 `file:` secret 实际通过 bind mount 提供，容器看到的是宿主文件的 UID、GID 和 mode；不能依赖 Compose 的 long syntax 把它改成另一套权限。因此 `apply` 会按 manifest 在**宿主机**固定运行 secret 的数值所有权：

- 摄像头密码：`root:root 0600`，只由 Frigate 的 root 进程读取；
- Cloudflare token：`65532:65532 0400`，只由官方 cloudflared 镜像的非 root UID 65532 读取；
- 内部 `lan-access-key`：`root:65532 0440`，只让 Portal 的主组和 LAN Caddy 的同号补充组读取；
- `secrets/` 父目录仍为 `0700`，不要把 GID 65532 分配给普通宿主机用户，也不要手工放宽或递归改权限。

LAN key 不是摄像头或 Tunnel 凭证，只用于实例内固定代理访问 Portal。单文件受限组方案避免复制同一 key；Caddy 仍以 UID/GID 1000 运行，仅额外获得 GID 65532，不获得 root 或 Docker 权限。cloudflared 也使用 65532，但不加入 Caddy 的容器或网络权限边界，且 Tunnel token 仍是 owner-only `0400`。rootless Docker、user namespace remap 和自定义容器 UID/GID 当前未纳入发行兼容范围，因为它们会改变 bind mount 的数值身份映射。

运行备份刻意不包含 secret 内容，只记录恢复所需的 secret 文件名。灾难恢复必须另外保存：

- `camare.ini`；
- 它引用的原始密钥文件；
- Cloudflare/Tailscale 外部配置记录；
- 必要时加密导出的凭证。

这些敏感备份应加密、离线或放在受控密码库中，并与公开代码备份分开。

生命周期归档中的 SHA-256 用于发现损坏，不是数字签名。归档仍包含设备绑定状态和认证数据库，只能恢复由自己创建、持续受控且来源可信的归档；不要恢复从论坛、网盘或他人服务器取得的目录。

## 设备绑定和 Cookie

公网客户页使用一次性二维码建立浏览器绑定。一个二维码只给一个设备：

- 不在群聊或公开相册分享二维码；
- 微信/QQ 扫码后按提示转到默认浏览器；
- 清除 Cookie、换浏览器或换 origin 后可能需要重新绑定；
- 手机丢失、设备转让或出现未知访问时，在管理页撤销绑定；
- Portal `state.json` 丢失会破坏绑定状态，应纳入应用状态备份。

二维码绑定不代替公网边界防护。仍应保留速率限制、Cloudflare/Tailscale 账号保护和最小暴露面。

## 录音与隐私

默认 `record_audio = false`。只有明确将某组设为 `true` 并重新 `apply`，或在 Tailscale 管理页确认录音变更并重启后，该组的新录像才尝试保留音轨。开启前确认：

- 摄像头确实提供音频；
- 家庭成员、访客和工作人员已按要求告知；
- 使用场景符合当地隐私、劳动和录音法律；
- 公网回放设备不会在不合适环境自动出声。

历史录像不会因开关变化被重新编码或删除音轨。

Tailscale 管理页的录音滑块也能修改当前 Frigate 配置并重启服务。该变化会被备份，但下一次 `apply` 会重新采用 `camare.ini`；需要持续启用时必须同步声明配置，避免无意中重新开启或关闭录音。

## 更新与供应链

渲染器引用固定 Frigate/cloudflared 版本和镜像 digest，本地构建也固定关键工具版本。固定不等于永久安全：

1. 阅读 Release notes 和漏洞公告；
2. 在隔离实例构建和验证；
3. 备份状态；
4. `plan` 后再 `apply`；
5. 不让生产机自动跟随 `latest`/`stable`。

不要使用来源不明的 `curl | sh`。从 GitHub clone 后先检查 remote、branch/tag 和 `git status`。

## 日志与求助

优先运行 `camarectl status/verify` 和限定 service 的 Compose 日志。发布日志前删除 URL 凭证、token、Cookie、家庭 IP/IPv6、Tailscale 登录名和画面信息。不要上传完整 `docker inspect`，其中可能包含环境变量和宿主路径。

漏洞报告渠道和公开 Issue 注意事项见仓库根 [SECURITY.md](../SECURITY.md)。

## 绝对禁止

- `docker compose down -v`；
- `docker system prune` / `docker volume prune`；
- 把 Frigate `5000` 或 go2rtc `1984` 暴露公网；
- 让生产与测试栈共享 media、Frigate DB 或 Portal state；
- 把媒体目录设为 `/` 或未确认挂载状态的空目录；
- 把“目录存在”当作 NAS 仍已挂载的证明；挂载失效后的空 mountpoint 可能落到系统盘；
- 为“修好权限”递归 `chmod 777`；
- 在不了解影响时修改路由器、全屋 DNS、Mihomo、IPv6 或防火墙。
