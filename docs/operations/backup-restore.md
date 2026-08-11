# 备份与恢复

Camare 把数据分为三类，不能用一个 Git 仓库代替完整备份：

| 类别 | 示例 | 默认生命周期备份 |
|---|---|---|
| 运行控制与应用状态 | manifest、Compose、`.env`、Frigate 配置/数据库、Portal `state.json` | 包含 |
| 密钥与声明输入 | `camare.ini`、摄像头密码文件、Cloudflare token | **不包含 secret 内容**；必须独立加密备份 |
| 录像媒体 | `storage.media` 下的片段 | **永不包含**；由存储/NAS 自己备份 |

这能避免每次备份复制数百 GB 录像，也避免把明文 secret 混入普通应用归档。

`apply`、backup、restore 和 uninstall 共用实例锁，不能并发执行。遇到锁占用先确认另一操作是否仍在运行，不要删除 lock 文件强行继续。

> 生命周期核心已通过本地自动化测试；Linux 隔离 E2E 和真实运行数据库恢复演练待执行。第一次对生产恢复前必须在隔离实例演练。

## 创建应用状态备份

目标必须是运行目录和媒体目录之外、尚不存在的新目录：

```sh
sudo ./install.sh backup \
    --config "$PWD/camare.ini" \
    --output /srv/camare-backups/home-camera-2026-08-11
```

成功后目录包含：

```text
backup-manifest.json
backup-manifest.sha256
payload/control/...
payload/portal/state.json       # 存在时
payload/frigate/*.db            # 存在时，使用 SQLite 一致性快照
```

备份以私有权限写入暂存目录，全部完成后才发布最终目录；失败会清理部分输出。manifest 记录恢复所需的 secret 文件名，但不会读取或复制 secret 内容。

归档中的 SHA-256 只能发现意外损坏，不能证明来源可信。归档仍含 Portal 设备绑定状态和 Frigate 登录/索引数据库；只恢复由自己创建并始终保存在受控位置的归档，整个备份目录都应按敏感数据保护。

不要把输出放在：

- `/opt/1panel/docker/compose/camare-<instance-id>` 内；
- `storage.media` 内或它的子目录；
- Git checkout 内；
- 会被公网 Web 服务直接读取的位置。

## 独立备份声明与密钥

至少保存：

- 当前源码 tag/commit；
- `camare.ini`；
- INI 引用的每个摄像头密码文件；
- Cloudflare token（如使用）；
- Cloudflare Published application 和 Tailscale Serve/Funnel 的配置记录；
- 正式 Release 的镜像 digest/校验和。

将这些内容放入加密密码库或离线加密归档。不要把它们复制进生命周期备份目录后上传到普通网盘。

## 录像备份

生命周期命令永远不读取、复制、恢复或删除 `storage.media`。根据存储类型使用：

- NAS snapshot/replication；
- 文件系统快照；
- 独立离线盘；
- 经过校验的增量备份。

录像保留天数不是备份。Frigate 到期删除、磁盘损坏、误删或勒索软件都可能同时影响在线副本。

## 恢复前 dry-run

恢复只接受同一 `instance_id` 的归档。先保持服务不变，验证 manifest、SHA-256、所有路径和当前所需 secret：

```sh
sudo ./install.sh restore \
    --config "$PWD/camare.ini" \
    --archive /srv/camare-backups/home-camera-2026-08-11
```

不带 `--apply --yes` 时只做 dry-run，不停止容器、不写运行目录。

## 执行恢复

确认 dry-run 输出、媒体挂载、源码版本和镜像可用后：

```sh
sudo ./install.sh restore \
    --config "$PWD/camare.ini" \
    --archive /srv/camare-backups/home-camera-2026-08-11 \
    --apply --yes

sudo ./install.sh verify --config "$PWD/camare.ini"
```

恢复过程会：

1. 再次校验归档和目标实例；
2. 确认运行目录中所需 secret 仍存在；
3. 停止 Portal 和 Frigate 写入者；
4. 逐文件恢复控制、Portal state 和 SQLite 一致性快照；
5. 清理被恢复 SQLite 的旧 WAL/SHM sidecar；
6. 使用现有镜像 `up -d --no-build`；
7. 验证失败时尝试恢复操作前状态并重启旧实例。

恢复保留现有目标文件的 POSIX 所有权和模式；目标文件原先不存在时使用固定安全模式和父目录所有权。若恢复旧状态本身失败，工具会保留带路径提示的 rollback 目录，供人工恢复；不要在问题查清前删除它。

secret 和录像不会被归档覆盖；当前运行目录中的 secret 继续生效。

## 灾难恢复限制

`restore` 是对已有匹配实例的受控恢复，不是从一份目录自动重建整台服务器。服务器全损后还需要：

- 恢复正确源码版本；
- 恢复 `camare.ini` 和独立密钥；
- 恢复媒体挂载；
- 保证归档引用的旧镜像/本地构建内容可获得；
- 先创建匹配的实例运行目录和 secret；
- 恢复 Cloudflare/Tailscale 外部配置。

旧镜像不可用或数据库版本不兼容时，先停止，不要用“最新镜像”强行打开旧数据库。

## 定期演练

至少定期验证：

- `backup-manifest.sha256` 可校验；
- 加密密钥备份可解密；
- 录像副本能独立读取；
- 在隔离实例 dry-run/恢复成功；
- 恢复后已有绑定设备、Frigate 登录和录像索引符合预期。

隔离演练不得连接生产摄像头、生产 Tunnel token，也不得让两个栈共享 media/DB。
