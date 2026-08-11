# 卸载

卸载按单个 `camare_<instance-id>` 限定范围。默认只做 dry-run；普通执行只移除该 project 的容器和网络，并完整保留可重新安装的运行目录；只有显式 purge 才删除受管控制、运行 secret 和 Portal/Frigate 持久数据。外部媒体目录和录像永远不在删除范围。

> 先做 [应用状态备份](backup-restore.md) 和独立密钥/录像备份。生命周期核心已通过自动化测试及 Linux 合成流隔离 E2E；这不替代对目标实例的 dry-run 和备份验证。

卸载与 `apply`、backup、restore 共用实例锁。不要在升级或恢复进行中并发卸载，也不要通过删除 lock 文件绕过保护。

## 1. 查看计划

```sh
sudo ./install.sh uninstall --config "$PWD/camare.ini"
```

不带 `--apply --yes` 时不会停止容器或删除文件。检查输出中的：

- `instance_id` 和 Compose project；
- 当前实例的 control/secret 和持久数据清单；
- 普通模式会保留这些文件，只有 `--purge-data` 计划才会删除；
- 外部 `storage.media` 路径不在删除列表。

如果 manifest、哈希、路径或 symlink 检查失败，先停止，不要手工递归删除来绕过保护。

## 2. 移除容器、完整保留运行目录

```sh
sudo ./install.sh uninstall \
    --config "$PWD/camare.ini" \
    --apply --yes
```

该操作会：

- 仅对当前 project 执行实例级 Compose `down --remove-orphans`；
- 不传 `-v`/`--volumes`；
- 保留 manifest、Compose、`.env`、Frigate/Caddy 配置和运行 secret；
- 保留内部 `.camare-backups`、Portal `state.json` 和 Frigate 数据库/认证状态；
- 保留 `storage.media` 中全部录像；
- Docker `down` 失败时不修改任何实例文件。

保留的运行目录不会自动运行。以后可用匹配源码和 `camare.ini` 再次 `apply`；此模式适合暂时停用或移除容器但保留可恢复现场。

## 3. 显式清除 Portal/Frigate 持久数据

只有已经验证备份、明确接受设备绑定和 Frigate 登录/索引丢失时才执行：

```sh
sudo ./install.sh uninstall \
    --config "$PWD/camare.ini" \
    --apply --yes \
    --purge-data \
    --confirm-instance home-camera
```

`--confirm-instance` 必须与 INI 中 `instance_id` 完全一致。`--purge-data` 会删除该运行目录内经过 manifest/白名单识别的 control、运行 secret、内部 `.camare-backups`、Portal state 和 Frigate 数据库；操作中断时会尝试从隔离区放回文件并用旧 Compose 重启。它仍不会触碰：

- `storage.media`；
- 录像文件；
- 源码 checkout；
- 其他 Compose project、网络、卷或镜像；
- Cloudflare/Tailscale 控制台配置；
- `camare.ini` 和它引用的输入密钥文件。

如果放回文件本身失败，命令会报出保留的 quarantine 目录；其中可能是唯一恢复副本。先记录路径并人工核对，不要重跑 purge 或清理该目录。

## 4. 外部入口与保留录像

容器卸载后还需人工处理：

- Cloudflare Published application/Tunnel connector；
- Tailscale Serve/Funnel 规则；
- 1Panel 中旧编排条目；
- 主路由器上为服务器/摄像头设置的 DHCP 保留（如不再需要）；
- `storage.media` 的归档或保留策略。

先确认这些规则只属于当前 Camare 实例，避免删除同机其他项目使用的 Tunnel 或 Serve 端口。

录像目录需要删除时，应当作为**独立存储任务**另行核对绝对路径、挂载点和备份。Camare 卸载命令故意不提供删除录像的参数。

## 禁止操作

不要用以下命令替代卸载器：

```text
docker compose down -v
docker system prune
docker volume prune
```

它们的范围可能超过当前实例，并破坏可恢复数据。
