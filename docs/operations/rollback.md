# 回滚

Camare 有两类回滚：

1. `apply` 或恢复操作失败时的**自动异常回滚**；
2. 新版本已成功启动但业务不符合预期时的**人工版本回滚**。

二者都只限定在单个 `camare_<instance-id>`，不会操作录像目录或全局 Docker 资源。

## 自动异常回滚的边界

升级会先校验暂存树，再逐文件原子替换受管控制文件。如果启动或部署后验证失败，会尝试：

- 恢复升级前的 manifest、Compose、`.env`、Frigate 配置和 Caddy 配置；
- 恢复升级前对应的受管 secret；
- 重新执行旧实例的 Compose `up`；
- 保留 Portal 状态、Frigate 数据库和外部媒体目录。

内部控制备份最多保留三份，但它们是实现细节，不是服务器灾难恢复备份。

自动回滚不保证覆盖：

- 写入期间断电或文件系统损坏；
- Docker daemon、磁盘或网络同时不可用；
- 新 Frigate 已完成不可逆数据库迁移；
- 用户在 1Panel 或运行目录中手工改了受管文件；
- Cloudflare/Tailscale 控制台的外部变更。

## 异常后先做什么

不要立即删除容器或目录。先收集：

```sh
sudo ./install.sh status --config "$PWD/camare.ini"
sudo ./install.sh verify --config "$PWD/camare.ini"
git rev-parse HEAD
```

再按 [故障排查](../troubleshooting.md) 查看这个 Compose project 的状态和日志。严禁 `down -v` 和 `prune`。

## 人工回到上一版本

若新版本已应用成功，但需要回到旧代码：

1. 确认已有当前状态备份和升级前备份；
2. 检查旧版本是否能读取当前 Portal/Frigate 数据格式；
3. checkout 已知 commit/tag；
4. 使用**旧版本能理解的 INI**运行 `validate`、`doctor`、`plan`；
5. 再运行 `sudo apply --yes` 和 `verify`。

```sh
known_good_ref='REPLACE_WITH_REVIEWED_TAG_OR_COMMIT'
git checkout "$known_good_ref"
./install.sh validate --config "$PWD/camare.ini"
./install.sh doctor --config "$PWD/camare.ini"
./install.sh plan --config "$PWD/camare.ini"
sudo ./install.sh apply --config "$PWD/camare.ini" --yes
sudo ./install.sh verify --config "$PWD/camare.ini"
```

若涉及不兼容数据库变化，不要只 checkout 代码；应按 [备份与恢复](backup-restore.md) 使用匹配实例的已知良好归档恢复状态。

## 何时停止

出现以下任一情况时停止自动尝试，保留现场并人工检查：

- `status` 无法识别 manifest 或报告哈希不匹配；
- 恢复后旧实例也无法启动；
- 媒体挂载点消失或变成系统根分区目录；
- Frigate 数据库损坏；
- 生产和测试栈可能正在共享 media/DB；
- 不清楚 Cloudflare/Tailscale 当前规则归属。

此时重建容器通常不能解决根因，反而可能破坏可恢复信息。
