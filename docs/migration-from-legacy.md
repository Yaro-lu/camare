# 从 legacy 多栈部署迁移

旧 `deploy.py`、`frigate-1panel/`、`camera-portal/`、`camera-portal-lan/` 和 `examples/cloudflare/` 的分散 Compose 属于 legacy。新路径生成一个 `camare_<instance-id>` project 和唯一运行目录：

```text
/opt/1panel/docker/compose/camare-<instance-id>
```

> 本轮不会自动迁移现有现网。迁移必须另行计划、先做只读基线，并包含一次受控停机。Linux 隔离 E2E 完成前，不应直接对生产执行迁移。

## 为什么不能“直接再启动一套”

新旧栈若同时共享任一项目，会产生数据损坏或不可预测行为：

- 同一个 Frigate SQLite 数据库；
- 同一个 Portal `state.json`；
- 同一个媒体目录并同时写录像；
- 同一宿主机端口；
- 同一个 Cloudflare Tunnel token/route；
- 同一 Tailscale Serve/Funnel 端口。

Docker project 名不同不能解决共享 bind mount 和外部入口冲突。

## 只读基线

对每个 legacy Compose 目录分别记录，不要输出完整环境或 secret：

```sh
legacy_compose_dir=/path/to/legacy-compose
cd "$legacy_compose_dir"
docker compose config --services
docker compose ps
docker compose ps -q
```

按 Compose service 查询容器 ID，不要依赖固定容器名。例如：

```sh
frigate_id="$(docker compose ps -q frigate)"
docker inspect --format '{{.Id}} {{.Image}} {{.RestartCount}} {{json .HostConfig.PortBindings}} {{json .Mounts}} {{json .NetworkSettings.Networks}}' "$frigate_id"
docker compose exec -T frigate wget -qO- http://127.0.0.1:5000/api/stats
```

对 Portal、LAN Caddy 和 cloudflared 使用各自 Compose 目录，并按实际 service 名执行，例如 `docker compose ps -q camera-portal`。不要保存完整 `docker inspect` JSON，它可能包含密钥环境变量。

另外记录：

- 源码 commit 和每个 Compose 文件的 SHA-256；
- 录像目录、Portal state、Frigate DB/auth DB 的真实路径和所有权；
- 每个端口的绑定 IP；
- Frigate stats、go2rtc producer 和最新录像片段时间；
- `tailscale serve status` / `tailscale funnel status`；
- Cloudflare Tunnel 名、公开主机名和源站 service；
- 1Panel 中各编排的 project 名与自动启动策略。

这些命令只读，但输出仍含家庭网络信息，应存入受控位置。

## 隔离演练

生产迁移前，新路径只能使用：

- 独立 `instance_id` 和 Compose project；
- 独立运行目录；
- 随机高位回环/LAN 测试端口；
- 合成或测试 RTSP，不连接生产摄像头；
- 独立临时 media 和 Portal/Frigate state；
- 不使用生产 Cloudflare token 或 Tailscale Serve/Funnel。

演练结束只能精确停止测试 project；不能运行全局 prune，也不能删除生产网络/卷。

## 生产切换原则

正式迁移计划至少应包括：

1. 维护窗口与回退判断点；
2. 应用状态、原始密钥和外部入口配置备份；
3. 停止 legacy 写入者；
4. 再次确认最新录像已落盘、SQLite 一致；
5. 转换/复制**状态**到受控的新运行目录，但不复制媒体；
6. 保证任一时刻只有一个 Frigate 写同一媒体目录；
7. 启动新实例并验证 LAN、直播、回放、录像推进和管理页；
8. 最后才切换 Cloudflare/Tailscale 外部入口；
9. 保留可回退的 legacy 控制和状态，直到观察期结束。

当前仓库没有自动执行上述生产迁移的命令。不要用新 `apply` 指向旧运行目录，除非迁移设计已经明确验证 manifest、所有权和状态格式。

## 继续维护 legacy

需要查看旧安装器帮助时必须显式声明：

```sh
sudo ./install.sh legacy --help
```

legacy 工具不会获得新 CLI 的全部事务、隔离和生命周期保证。维护时仍应在对应 Compose 目录通过 service 操作：

```sh
docker compose ps
docker compose exec -T frigate wget -qO- http://127.0.0.1:5000/api/stats
```

不要使用旧文档中的固定容器名，也不要把旧四栈操作方式混入新单栈运行目录。
