# ADR-0002: 使用 Python 标准库和 INI 作为部署配置

## Status

Accepted

## Context

当前交互问答难以重复、审查和无人值守运行。项目又希望保持 `git clone` 后无需 pip 安装依赖。配置需要表达最多两组设备、每组一到两路 RTSP、硬件档、存储和入口，同时秘密不能进入配置、命令行、manifest 或日志。

## Decision

部署 CLI 继续只使用 Python 3.10+ 标准库，采用 `configparser` 读取严格 INI。部署、网络、存储和硬件使用固定 section；每台物理设备使用一个 `[group:<id>]` section。摄像头密码和 Cloudflare token 仅以独立文件路径引用，文件权限不得宽于 `0600`。

解析器拒绝未知 section/key、重复或不安全 ID、超过两组、每组超过两路、非 Linux 绝对运行/存储路径及首版范围外的编码声明。模型只保存 secret 引用，不读取 secret 内容，且摘要和 `repr` 必须脱敏。

## Consequences

### Positive

- 无需 PyYAML/TOML writer 或额外虚拟环境。
- 配置可进入版本控制模板并用于 `doctor/plan/apply` 重复执行。
- 拼写错误会显式失败，不会静默使用危险默认值。
- 秘密文件可独立轮换和收紧权限。

### Negative

- INI 对任意深层嵌套和未来大量摄像头不如 YAML 自然。
- Python 3.10 标准库没有 TOML 写入器，因此交互向导需要自行输出 INI。
- 首版模型刻意限制两组，未来扩展需 schema version。

### Neutral

- 旧 `.env`/交互安装保留为迁移输入，但不会成为新配置的第二真相源。

## Alternatives Considered

**YAML**：用户熟悉，但需要额外依赖且安全解析/精确错误处理更复杂。

**JSON**：标准库原生支持，但手工编辑和注释体验较差。

**只使用 `.env`**：无法清晰表达组和流的层次，也难以严格识别未知字段。

## References

- [Python configparser](https://docs.python.org/3/library/configparser.html)
