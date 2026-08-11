# 故障排查

先运行高层检查，再看单个 service。不要把“重建所有容器”当成第一步。

```sh
./install.sh validate --config "$PWD/camare.ini"
sudo ./install.sh doctor --config "$PWD/camare.ini"
sudo ./install.sh status --config "$PWD/camare.ini"
sudo ./install.sh verify --config "$PWD/camare.ini"
```

`doctor` 检查部署前条件；`verify` 检查实际容器、采集进程、go2rtc producer、录像推进和入口。保存完整错误 code，但公开求助前先脱敏 IP、域名、用户名和路径。

## 原始 Compose 命令

进入唯一运行目录，并按实际 instance ID 修改 project 名：

```sh
cd /opt/1panel/docker/compose/camare-home-camera

camare_compose() {
    if [ -f compose.intel.yaml ]; then
        docker compose --project-name camare_home-camera --env-file .env \
            -f compose.yaml -f compose.intel.yaml "$@"
    else
        docker compose --project-name camare_home-camera --env-file .env \
            -f compose.yaml "$@"
    fi
}

camare_compose ps
```

不要猜容器名。需要容器 ID 时按 service 获取：

```sh
camare_compose ps -q frigate
camare_compose ps -q camera-portal
```

在容器内运行只读命令使用 `exec -T <service>`：

```sh
camare_compose exec -T frigate wget -qO- http://127.0.0.1:5000/api/stats
camare_compose exec -T frigate wget -qO- http://127.0.0.1:1984/api/streams
```

## `doctor` 失败

| code 前缀 | 检查方向 |
|---|---|
| `platform.*` / `python.*` | 必须是 Linux x86-64、Python 3.10+ |
| `docker.*` | Docker daemon、Compose v2 与 `docker compose up --wait`；普通用户可能无 socket 权限 |
| `runtime.path` | `/opt/1panel/docker/compose` 或现有实例目录不可写 |
| `media.*` | 媒体目录不存在、不可写、未挂载或空间过低 |
| `hardware.vaapi` | render device 不是可读写字符设备，或配置驱动不匹配 |
| `port.*` | 地址冲突、bind IP 不属于服务器、同一配置端口重复 |
| `camera.tcp.*` | 摄像头 IP/端口、路由、供电或局域网连接 |
| `camera.codec.*` | 不是 H.264，或受保护流无法在预部署阶段无凭证探测 |

已有实例升级时，对该实例端口的占用可能被 `apply` 降为警告，最终仍由部署后验证确认归属。不要通过改成 `0.0.0.0` 绕过绑定错误。

## 两路都没有直播

1. 确认摄像头能从服务器访问，而不是只在手机 App 中在线；
2. 查看 Frigate 和 Portal 最近日志：

   ```sh
   camare_compose logs --tail=150 frigate
   camare_compose logs --tail=150 camera-portal
   ```

3. 检查 `/api/stats` 中每路采集进程与 fps；
4. 检查 go2rtc `/api/streams` 中每路 producer；
5. 确认摄像头输出 H.264，且 RTSP 路径区分大小写；
6. 确认每个组的 `password_file` 是正确 `0600` 普通文件，只有一行内容（允许一个结尾换行）。

不要在日志命令中拼出完整带密码 RTSP URL。

## 只有一路黑屏或加载很晚

- 检查另一条 stream ID、路径和 go2rtc producer；
- 确认同组最多两路、stream ID 全局唯一；
- 客户端切换组后应只加载当前组，旧组连接应释放；
- 关闭浏览器省电/低数据模式做对比，但不要把它当服务器修复；
- 分别用 LAN 和公网测试。LAN 正常而公网慢，优先检查 Tunnel/Funnel 和家庭上行。

## 回放能看，直播不能看

回放和直播使用的浏览器媒体路径不同。检查：

- Frigate/go2rtc producer 是否持续存在；
- Portal 日志是否出现直播 lease、上游或浏览器中断；
- iPhone Safari 是否允许页面内自动播放静音视频；
- 页面进入后台后是否被系统暂停，回前台能否恢复；
- LAN 与公网是否表现一致。

不要在未定位前把直播替换成额外录像副本；Camare 的设计是读取 Frigate 已有媒体和上游。

## 回放首次加载慢或拖动后黑屏

- 运行 `verify` 确认每路录像文件时间确实在推进；
- 检查所选时间是否存在录像，摄像头断网会形成空档；
- 检查媒体挂载延迟和家庭公网带宽；
- 全屏只应拉当前一路高清回放，退出后再恢复双画面；
- 不要清空数据库“重建索引”，除非已有一致备份且确认数据库损坏。

## 录像不增长或写到错误磁盘

```sh
findmnt --target /srv/frigate/media
df -h /srv/frigate/media
camare_compose exec -T frigate df -h /media/frigate
```

把 `/srv/frigate/media` 换成 INI 中实际的 `storage.media`。

如果 NAS/外接盘没有挂载，先停止故障处理并恢复系统挂载。不要在相同空目录继续启动，以免录像落到系统盘。修改 `storage.media` 不会搬迁旧录像。

## Intel VAAPI 报错或 CPU 很高

```sh
ls -l /dev/dri
camare_compose exec -T frigate ls -l /dev/dri
camare_compose logs --tail=200 frigate
```

确认 Intel 实例同时使用 `compose.intel.yaml`，INI 中设备节点存在，驱动是实际平台需要的 `i965` 或 `iHD`。VAAPI 只优化解码；CPU 高温还可能来自散热、转码、浏览并发或其他容器。

## Cloudflare Error 1033

该错误通常表示 Cloudflare 找不到活动 Tunnel connector。只在 `provider = cloudflare` 时应存在 cloudflared service：

```sh
camare_compose ps cloudflared
camare_compose logs --tail=150 cloudflared
```

再检查：

- token 是否仍有效；
- Published application 是否指向 `http://camera-portal:8080`；
- 主机名是否与 `public.origin` 完全一致；
- cloudflared 是否仅加入 `portal_ingress`；
- 是否误停了当前实例的 cloudflared。

不要用启动无关项目的 cloudflared 容器来临时恢复；一个入口应由当前实例明确拥有。

## Tailscale 管理页登录失败

```sh
tailscale status
tailscale serve status
```

确认：

- 使用的是 Serve URL，不是 Funnel URL；
- `admin.origin` 与浏览器 origin 完全一致；
- `admin.tailscale_login` 与实际登录账号一致；
- Tailscale Serve 指向 `127.0.0.1:<admin.loopback_port>`；
- `provider = lan` 会按设计关闭 Portal 管理页。

不要在 Portal HTML 中硬编码密码提示或绕过 Tailscale 身份头；Frigate 自身登录和 Portal 管理登录是两个系统。

## Frigate 登录失败

Frigate 管理页与 Portal/Tailscale 管理页是两套认证。首次 Frigate 管理员通常为 `admin`，初始密码只会出现在首次启动日志中；可按 service 查询，而不是猜容器名：

```sh
camare_compose logs frigate | grep -A1 'User: admin'
```

日志轮转后可能已找不到初始密码。不要把摄像头 RTSP 密码当成 Frigate 密码，也不要删除 Frigate 数据库来“重置”；先备份状态，再按当前固定 Frigate 版本的认证文档执行恢复。

## LAN 页面打不开

```sh
ip -4 address
camare_compose ps lan-caddy camera-portal
camare_compose logs --tail=100 lan-caddy
```

确认 `lan.bind_ip` 仍属于服务器、端口没有被占用、手机连接的是同一可信家庭网。若 DHCP 地址改变，应在路由器做地址保留，然后更新 INI/`apply`；不需要给全屋设备手工填写 DNS。

## 最后手段之前

如果准备回滚、恢复或卸载，先阅读 [备份与恢复](operations/backup-restore.md) 和 [回滚](operations/rollback.md)。以下命令不属于排障手段：

```text
docker compose down -v
docker system prune
docker volume prune
```

它们可能删除无关项目或破坏可恢复状态。
