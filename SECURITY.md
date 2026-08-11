# 安全策略

Camare 是家庭自托管项目，不是托管服务，也不提供安全响应 SLA。部署者负责保护 Linux 主机、Docker daemon、家庭网络、摄像头、Cloudflare/Tailscale 账号和录像数据。

## 支持版本

[Releases](https://github.com/Yaro-lu/camare/releases) 页面发布的最新 `vX.Y.Z` 是正式版本依据。如果页面尚无正式发行版，唯一维护目标是 `main` 中由 `camarectl` 生成的单项目架构；这表示可以报告安全问题，不表示它已经完成生产级兼容性验收。

| 路径或版本 | 安全维护状态 |
|---|---|
| 当前 `main` 的 `camarectl` 单项目架构 | 接受报告；发行前的维护目标 |
| 最新正式 `vX.Y.Z` Release（发布后） | 支持；修复通常随新版提供 |
| 更早的正式 Release | 不承诺回补安全修复，请先升级到最新版 |
| `deploy.py` 和旧四栈 Compose | 仅为既有部署保留兼容，不推荐新安装，不承诺获得新架构的安全加固 |
| Fork、自行修改的 Compose、未受支持平台或镜像 | 尽力判断；可能要求先在受支持配置中复现 |

即使问题只出现在 legacy 部署，也可以报告。修复方案可能是迁移到当前单项目架构，而不是给 legacy 路径回补功能。迁移边界见 [从 legacy 多栈部署迁移](docs/migration-from-legacy.md)。

## 当前架构和安全边界

推荐入口是 `python3 -m camarectl` 或 `./install.sh`。它根据声明式 INI 生成一个运行目录和一个 Compose project：

```text
/opt/1panel/docker/compose/camare-<instance-id>
camare_<instance-id>
```

源码 checkout、运行控制文件、应用状态和外部录像目录彼此分离。一个实例包含 Frigate、Camera Portal、LAN Caddy，以及按 provider 可选的公网连接器；不要再把源码中的旧四个 Compose 目录分别部署成新项目。

生成的网络分为两个 project-local bridge：

- `media_backend` 只承载 Frigate 与 Camera Portal；
- `portal_ingress` 承载 Camera Portal、LAN Caddy 和可选 cloudflared；
- LAN Caddy 与 cloudflared 不加入 `media_backend`，因此不能按 service name 直接访问 Frigate；
- Portal 公网和管理源站只绑定 `127.0.0.1`；LAN 页面只允许绑定精确的 RFC1918 IPv4；
- Frigate `5000`、go2rtc `1984` 和 RTSP `8554` 不发布到宿主机；Frigate `8971` 使用自身认证，并只应绑定受控 LAN 或 Tailscale IPv4；
- 公网客户入口与管理入口是不同边界。管理页只应通过 Tailscale Serve，并匹配配置的 Tailscale 登录身份；LAN provider 会关闭管理页面功能。

摄像头密码和 Cloudflare token 由 INI 引用独立文件。在 POSIX 主机上，这些输入必须仅 owner 可读且 group/other 不可访问（通常为 `0600`）；渲染后以 Docker secret 文件提供给容器，不写入配置摘要、非秘密 manifest 或命令行。不要因此把 Docker 视为低权限边界：能够控制 Docker 的用户通常等同于主机 root。

以下行为是设计边界，不应误解为额外认证：

- LAN 观看页故意不要求最终用户登录；任何能访问该 LAN 地址的设备都能观看。请使用可信 Wi-Fi、访客网络隔离或 VLAN；
- LAN 内部 key 只证明请求来自生成的 Caddy 代理，不识别最终观看者；
- Portal 的设备绑定不能替代 Cloudflare/Tailscale、主机和家庭网络防护；
- Camare 不能保护已经被接管的服务器、Docker daemon、管理员账号或摄像头；
- 测试和生产实例不得共享 Frigate 数据库、Portal `state.json`、写入中的媒体目录、端口或 Tunnel token。

完整部署边界、密钥轮换和日志脱敏规则见 [安全指南](docs/security.md)，受支持的平台和功能见 [兼容性矩阵](docs/compatibility.md)。

## 私密报告漏洞

不要先公开漏洞细节。优先登录 GitHub 后，从仓库的 **Security → Report a vulnerability** 提交 [GitHub Security Advisories 私密报告](https://github.com/Yaro-lu/camare/security/advisories/new)。该按钮只有在仓库启用 private vulnerability reporting 后才会显示；操作说明见 [GitHub 官方文档](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)。

本项目目前没有公布安全邮箱或其他私密联系渠道。如果私密报告按钮不可用，可以创建一个[公开 Issue](https://github.com/Yaro-lu/camare/issues/new)，但只写“需要私密安全联络渠道”和受影响组件的大类，请维护者启用私密报告；不要写漏洞原理、利用步骤或任何敏感数据。

私密报告建议包含：

- 受影响的 tag、commit，以及使用 `camarectl` 还是 legacy 路径；
- provider、主机/浏览器类别和最小化配置，所有地址与标识均脱敏；
- 可在合成数据或测试摄像头上复现的步骤；
- 实际影响、攻击所需权限和是否已被利用；
- 已尝试的缓解方法，以及可选的修复建议。

维护者会尽力确认和协调修复，但不承诺首次响应或发布修复的时间。修复或 Security Advisory 发布前，请避免公开披露可直接利用的细节。

## 绝不能放进公开报告

不要在公开 Issue、Pull Request、Discussion、截图或日志中粘贴：

- 摄像头用户名、密码或含凭证的完整 RTSP URL；
- Cloudflare Tunnel token、Tailscale auth key/OAuth secret；
- `camare.ini`、真实 `.env`、运行目录 `secrets/` 或任何备份中的秘密；
- Portal `state.json`、绑定二维码、Cookie 或设备绑定 URL；
- Frigate 管理员密码、认证数据库或首次启动密码；
- 完整 `docker inspect`、未脱敏日志、家庭公网 IP/IPv6、Tailscale 登录名；
- 录像、截图、家庭地址或可识别人员的画面与声音。

如果 secret 已经进入公开页面、Git 历史、聊天或截图，应先在对应摄像头、Cloudflare/Tailscale 或应用中撤销并轮换；仅删除原消息不能使 secret 恢复安全。

## legacy 说明

`deploy.py`、`frigate-1panel/`、`camera-portal/`、`camera-portal-lan/` 和 `examples/cloudflare/` 的分散编排属于 legacy。它们不具备当前 `camarectl` 路径完整的单项目隔离、事务更新、验证和生命周期保证。旧部署如需查看兼容帮助，必须显式使用：

```sh
sudo ./install.sh legacy --help
```

不要让 legacy 与新实例同时写同一个媒体目录或共享 Frigate DB、Portal state、端口、Tunnel/Funnel/Serve 配置。现网迁移必须先做只读基线、备份和回退计划，并安排受控停机；`camarectl apply` 不会自动接管旧四栈。
