# ADR-0001: 运行态与源码分离，并生成实例隔离的单一编排

## Status

Accepted

## Context

旧安装器直接修改 Git 跟踪的 Frigate 配置，并在四个源码子目录中生成 `.env`、数据库、绑定状态和秘密。固定容器名和网络名使同一服务器无法安全并行运行第二套实例，也让 `git pull --ff-only`、回滚和服务器隔离测试容易互相影响。用户希望 clone 后尽量自动部署，并能在 1Panel 中直观看到和管理整套服务。

## Decision

源码 checkout 只保存程序和模板。新安装生成 `/opt/1panel/docker/compose/camare-<instance-id>/` 运行目录，包含单一实例 Compose、环境、Frigate 配置、secret、Portal 状态和无秘密 manifest；录像继续绑定用户指定的外部目录。

生成的 Compose 不设置 `container_name`，所有容器、网络和卷由唯一 Compose project name 隔离。现有四份分目录 Compose 暂时保留作为兼容和手工参考，但不再是新安装的推荐运行入口。

## Consequences

### Positive

- Git checkout 始终可更新，运行数据不会污染源码。
- 同机可安全存在生产与 `camaretest_<run-id>` 测试实例。
- 1Panel 只需导入一个运行目录和一份编排。
- 回滚、清理和状态核对可精确限定到一个 project。

### Negative

- 需要生成器维护最终 Compose，不能只让用户复制静态模板。
- 从旧四栈部署迁移到新单栈会产生一次受控停机，不能自动接管生产。
- 当前 `build_mode = local` 仍需要保留源码 checkout；发布 GHCR 镜像本身不会让安装器自动解耦，未来需要单独实现 image build mode。

### Neutral

- Cloudflare Published application 的内部服务地址仍是 `http://camera-portal:8080`。
- 录像目录不进入运行根，因此备份配置默认不包含录像。
- 本地 Compose `file:` secret 保留宿主 UID/GID/mode；manifest 负责把每个运行 secret 收紧到实际消费容器的数值身份，不能只依赖 Compose long syntax。

## Alternatives Considered

**继续在源码目录中运行四个 Compose**：改动最少，但 checkout 污染、跨项目状态和同机冲突仍然存在。

**生成四个隔离运行 Compose**：兼容风险较低，但 1Panel 操作、事务状态和清理仍分散；保留为旧部署兼容路线。

**Kubernetes/Helm**：对一台家庭服务器明显过度，增加运维成本。

## References

- [一键部署发行版设计](../plans/2026-08-11-one-click-release-design.md)
