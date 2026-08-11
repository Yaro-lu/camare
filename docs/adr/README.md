# Architecture decision records

本目录记录 Camare 公开发行版的重要、长期架构决定。状态为 Accepted 的 ADR 是实现和评审时的约束；若以后改变决定，应新增 ADR 并标明取代关系，不直接改写历史理由。

- [0001：运行态与源码分离，并生成实例隔离的单一编排](0001-isolated-runtime-stack.md)
- [0002：使用 Python 标准库和 INI 作为部署配置](0002-standard-library-ini-config.md)
- [0003：客户页面只加载当前摄像头组](0003-active-camera-group-only.md)
