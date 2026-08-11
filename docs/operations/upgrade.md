# 升级

升级的唯一长期输入是源码版本、`camare.ini` 和它引用的密钥文件。不要直接修改 `/opt/1panel/docker/compose/camare-<instance-id>` 中的生成文件；下一次 `apply` 会按声明配置重新生成。

> Linux x86-64 / CPU / 两路合成 H.264 隔离 E2E 已通过；正式 Release 与真实设备矩阵仍待完成。没有经过隔离验收的 commit 不应直接用于现网升级。

## 升级前

1. 记录当前版本或 commit：

   ```sh
   git rev-parse HEAD
   ```

2. 运行当前版本状态检查：

   ```sh
   sudo ./install.sh status --config "$PWD/camare.ini"
   sudo ./install.sh verify --config "$PWD/camare.ini"
   ```

3. 创建应用状态备份，并单独确认媒体存储和密钥备份策略，见 [备份与恢复](backup-restore.md)。
4. 记录 Tailscale Serve/Funnel 和 Cloudflare 控制台配置；升级不会替你重建这些外部状态。
5. 阅读 [CHANGELOG](../../CHANGELOG.md)，特别检查 Frigate、数据库格式、Portal 状态和配置字段变化。

## 获取版本

正式 Release 发布后，优先使用经过审查的 tag，而不是让生产机长期跟随 `main`：

```sh
git fetch --tags --prune
git status --short
reviewed_release_tag='vX.Y.Z'  # 换成已审查且实际存在的正式 tag
git checkout "$reviewed_release_tag"
```

如果 `git status --short` 显示本地修改，先停止，不要强制覆盖。运行状态本来就不应写在源码 checkout 中。

## 预检与应用

```sh
./install.sh validate --config "$PWD/camare.ini"
./install.sh doctor --config "$PWD/camare.ini"
./install.sh plan --config "$PWD/camare.ini"
sudo ./install.sh apply --config "$PWD/camare.ini" --yes
sudo ./install.sh verify --config "$PWD/camare.ini"
```

`plan` 必须先成功。`apply` 会：

- 在运行目录外创建并校验暂存文件；
- 按 manifest 逐个原子替换受管控制文件；
- 保留 Portal `state.json`、Frigate 数据库和外部媒体目录；
- 在实例内最多保留三份内部控制文件备份；
- 启动并验证新实例；失败时尝试恢复旧控制文件并重新启动旧 Compose。

这不是全目录、数据库和断电意义上的原子事务。逐文件替换期间断电、磁盘 I/O 错误或 Docker daemon 故障仍需要人工确认状态。

内部 `.camare-backups` 用于异常回滚，可能包含旧运行 secret，必须按敏感数据保护；它不替代 [生命周期备份](backup-restore.md)。

## 升级后

```sh
sudo ./install.sh status --config "$PWD/camare.ini"
sudo ./install.sh verify --config "$PWD/camare.ini"
```

然后分别实测：

- LAN 首页、两路直播、全屏和回放拖动；
- 公网客户域名和已有绑定设备；
- Tailscale 管理页和设备列表；
- 新录像持续产生且旧录像仍可读；
- 有录音需求的组只在新片段中出现预期音轨。

## 禁止操作

- 不运行 `docker compose down -v`；
- 不运行 `docker system prune`、`docker volume prune` 或网络全局清理；
- 不删除实例内 `.camare-backups` 来“腾空间”后再排错；
- 不让旧栈和新栈同时写同一媒体目录或使用同一 Frigate/Portal 数据；
- 不在 1Panel 中省略 `compose.intel.yaml` 后重建 Intel 实例；
- 不通过手改生成的 `.env` 临时修复后忘记同步 `camare.ini`。

升级失败见 [回滚](rollback.md) 和 [故障排查](../troubleshooting.md)。
