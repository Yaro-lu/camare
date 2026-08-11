# Camare One-click Release Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把当前家庭服务器快照改造成 checkout 保持干净、支持两组双路、可诊断、可验证、可隔离测试和可版本发布的一键部署项目。

**Architecture:** 保留现有 Frigate/go2rtc/Portal，新增标准库 Python `camarectl` 生成 `/opt/1panel/docker/compose/camare-<instance-id>` 单栈运行目录；`runtime_dir` 始终是完整实例目录。Compose 资源由实例命名空间隔离；Portal 只加载当前摄像头组。旧的分目录 Compose 和 `deploy.py` 在迁移期保留，但不再作为推荐安装路径。

**Tech Stack:** Python 3.10+ 标准库、Docker Compose v2、Frigate 0.17.2、Go、原生 ES modules、Caddy、GitHub Actions/GHCR。

---

### Task 1: 公开发行安全基线

**Files:**
- Create: `LICENSE`
- Create: `CHANGELOG.md`
- Modify: `.gitignore`
- Modify: `frigate-1panel/config/config.yml`
- Modify: `examples/cloudflare/compose.yaml`
- Modify: all existing Compose files
- Test: `tests/test_repository_policy.py`

**Steps:**
1. 写失败测试，断言存在 MIT License、默认录像 preset 不含音频、cloudflared 不是 `latest`、Compose 不含 `container_name`。
2. 运行 `python -m unittest tests.test_repository_policy -v`，确认失败。
3. 添加 MIT License 和变更记录；将公开模板默认改为静音；固定 cloudflared 版本；删除固定容器名并参数化网络/镜像/端口。
4. 运行策略测试和四个 Compose render，确认通过。
5. 提交 `chore: establish public release safety baseline`。

### Task 2: 声明式配置模型

**Files:**
- Create: `camare.example.ini`
- Create: `camarectl/__init__.py`
- Create: `camarectl/config.py`
- Create: `camarectl/model.py`
- Test: `tests/test_camare_config.py`

**Steps:**
1. 写失败测试，覆盖实例 ID、Linux 绝对路径、1–2 个组、每组 1–2 路、唯一 camera ID、H.264 首版声明、音频布尔值和 secret 文件权限。
2. 运行测试确认缺少模块而失败。
3. 用 `configparser` 实现不可变 dataclass 和严格校验；错误必须指出 section/key，且 `repr` 不包含秘密。
4. 加入带注释的双组示例，第二组默认禁用，所有密码使用相对 secret 文件引用。
5. 运行配置测试，提交 `feat: add declarative deployment configuration`。

### Task 3: 独立运行目录与渲染器

**Files:**
- Create: `camarectl/layout.py`
- Create: `camarectl/render.py`
- Create: `deploy/compose.base.yaml`
- Create: `deploy/compose.intel.yaml`
- Create: `deploy/frigate.config.yml.tmpl`
- Test: `tests/test_render.py`

**Steps:**
1. 写失败测试，渲染到临时 `/opt/camare/test` 等价目录，断言源码文件哈希不变、秘密不进入 `.env/config.yml`、Compose 无固定名称、网络由 project 隔离、CPU 不映射 GPU、Intel override 精确映射 render device。
2. 写四路两组快照测试，断言 go2rtc/cameras、Portal `CAMERAS/CAMERA_GROUPS`、每组录音 preset 和 Docker secrets 正确。
3. 实现原子暂存渲染、`0600` secret/`.env`、`0640` Frigate config、manifest 哈希和三份精确备份。
4. 用 `docker compose --project-name camare_test --env-file ... -f ... config --quiet` 验证渲染结果。
5. 提交 `feat: render isolated single-stack deployments`。

### Task 4: doctor 与部署计划

**Files:**
- Create: `camarectl/checks.py`
- Create: `camarectl/commands.py`
- Create: `camarectl/__main__.py`
- Modify: `install.sh`
- Test: `tests/test_doctor.py`
- Test: `tests/test_cli.py`

**Steps:**
1. 写失败测试，模拟架构、内存、Docker、Compose、端口、IP、挂载、可写性、空间、render device、RTSP TCP 和 ffprobe 结果。
2. 实现 `doctor` 的 PASS/WARN/FAIL 结构化结果和 JSON 输出；只读运行且普通用户可执行。
3. 实现 `plan`，临时渲染并输出脱敏变更计划；任何 secret 值都不能出现在 stdout/stderr。
4. 更新 `install.sh` 调用 `python3 -m camarectl install`，保留 `deploy.py` 兼容入口与弃用提示。
5. 运行 CLI/脱敏测试，提交 `feat: add doctor and deployment planning commands`。

### Task 5: apply、verify 与 status 最小闭环

**Files:**
- Create: `camarectl/runtime.py`
- Create: `camarectl/verify.py`
- Modify: `camarectl/commands.py`
- Test: `tests/test_runtime.py`
- Test: `tests/test_verify.py`

**Steps:**
1. 写失败测试，覆盖实例文件锁、幂等 apply、Compose 校验先于替换、启动失败恢复旧 manifest/文件、绝不调用 `down -v`/prune。
2. 实现 `apply`：doctor gate → 暂存渲染 → Compose config → 原子切换 → 分阶段 up → bounded verify。
3. 实现媒体级 `verify`：容器健康、Frigate stats/pid/fps、go2rtc producer、录像文件推进、Portal/LAN HTTP。
4. 实现 `status --json`，读取 manifest 并输出版本、服务、入口和验证结果。
5. 运行故障注入测试，提交 `feat: add transactional apply and media verification`。

### Task 6: Portal 公开摄像头组与按组加载

**Files:**
- Modify: `camera-portal/internal/portal/http.go`
- Modify: `camera-portal/internal/portal/http_test.go`
- Modify: `camera-portal/web/client.html`
- Modify: `camera-portal/web/assets/client.js`
- Modify: `camera-portal/web/assets/common.css`
- Create: `camera-portal/web/assets/camera-groups.mjs`
- Create: `camera-portal/web/tests/camera-groups.test.mjs`
- Modify: `camera-portal/web/embed_test.go`

**Steps:**
1. 写 Go 失败测试，断言 `/api/config` 返回 public groups 且不暴露管理/录音配置。
2. 写 Node 失败测试，断言单组隐藏标签、空组过滤、切组先释放旧组资源、当前组最多两路。
3. 实现纯函数组模型和顶部标签；将 `state.cameras` 变成当前组视图，并在切组时 abort 索引、销毁直播/HLS/JSMPEG、重建当前模式。
4. 验证直播/回放切换、全屏、时间轴、声音控制既有测试无回归。
5. 提交 `feat: load only the active camera group`。

### Task 7: CI、镜像和发行物

**Files:**
- Modify: `.github/workflows/checks.yml`
- Create: `.github/workflows/release.yml`
- Create: `.github/dependabot.yml`
- Create: `camera-portal-lan/Dockerfile`
- Create: `scripts/release-check.sh`
- Test: workflow lint / Docker build

**Steps:**
1. CI 增加 Node 测试、Python 全套、gofmt/go vet、ShellCheck、实际 Portal/Caddy build、gitleaks 和 Trivy；Actions 固定 commit SHA。
2. Release workflow 在 `v*` tag 构建 amd64/arm64 Portal/Caddy GHCR 镜像，生成 digest、checksums、SBOM 和 provenance。
3. 生成 release compose，默认引用不可变版本镜像；本地 build 通过显式 override 启用。
4. 运行本地可执行检查并审查权限，提交 `ci: publish reproducible release artifacts`。

### Task 8: 备份、升级、回滚和卸载边界

**Files:**
- Modify: `camarectl/commands.py`
- Create: `camarectl/backup.py`
- Test: `tests/test_lifecycle.py`

**Steps:**
1. 写失败测试，确保 backup 使用 SQLite 一致性备份、manifest/checksum，不复制媒体目录。
2. 实现 `backup`、`restore --dry-run`、Portal/LAN 默认升级、显式 `--with-frigate`、配置+镜像回滚。
3. 实现 `uninstall` 精确使用实例 project，默认保留 state/config/media；purge 需要独立参数，媒体永不自动删除。
4. 故障注入验证回滚，提交 `feat: add safe deployment lifecycle commands`。

### Task 9: 文档与兼容矩阵

**Files:**
- Rewrite: `README.md`
- Create: `docs/compatibility.md`
- Create: `docs/install/quickstart.md`
- Create: `docs/install/1panel.md`
- Create: `docs/install/cloudflare.md`
- Create: `docs/install/tailscale.md`
- Create: `docs/operations/backup-restore.md`
- Create: `docs/operations/upgrade-rollback.md`
- Create: `docs/troubleshooting.md`
- Modify: `SECURITY.md`

**Steps:**
1. 将根 README 压缩为五分钟 Quickstart、已验证范围、架构、安全警告和文档导航。
2. 明确已验证/预期兼容/实验/不支持矩阵；默认无声与录音合规提示。
3. 写 1Panel 单编排导入、Cloudflare 手工 Published application、Tailscale 身份边界、备份/恢复演练和排错。
4. 运行链接/示例配置检查，提交 `docs: publish one-click deployment guide`。

### Task 10: 合成 RTSP E2E 与服务器零影响验收

**Files:**
- Create: `tests/e2e/compose.yaml`
- Create: `tests/e2e/run.sh`
- Create: `scripts/server-staging-audit.sh`
- Modify: `.github/workflows/checks.yml`

**Steps:**
1. 用 synthetic H.264/RTSP 创建两组四路低码率测试源和临时媒体目录。
2. CI 验证首次 apply、重复 apply、直播/录像索引/回放、CPU profile 和精确 teardown。
3. 服务器测试前只读记录生产容器/文件/Tailscale/录像基线；运行随机端口 `camaretest_<run-id>`，不使用生产 secret/media/入口。
4. 精确删除测试 project/volume/network/目录；逐项比较生产 ID、restart count、哈希、Tailscale JSON 和录像推进。
5. 保存脱敏验收报告，提交 `test: add isolated end-to-end deployment verification`。

### Task 11: 发布与交付

**Files:**
- Modify: `CHANGELOG.md`
- Modify: version constants and release manifests

**Steps:**
1. 运行 Python、Go race/vet、Node、Compose、Docker、secret scan、SBOM 和 E2E 全套。
2. 使用 `git diff --check`、敏感信息扫描和变更影响审查。
3. 推送 `feat/release-hardening`，通过 CI 后合并或按用户仓库策略直接更新 `main`。
4. 创建 `v0.1.0` annotated tag 和 GitHub Release，核对 GHCR digest。
5. 报告服务器测试前后基线、生产零重启证明、安装命令和已知限制。
