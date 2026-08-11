# Admin Camera Audio Controls Implementation Plan

> Historical plan for the legacy multi-Compose deployment. It is not an installation guide. New instances use only `/opt/1panel/docker/compose/camare-<instance-id>`; see the [root README](../../README.md) and [Quickstart](../quickstart.md).

**Status:** Completed and deployed on 2026-08-10. The current yard group records without audio; the two front-group slots remain reserved for future cameras.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在仅限 Tailscale 的管理页展示分组摄像头低清快照，以组级滑块统一设置新录像是否包含声音，并在公开回放全屏时提供单路手动声音开关。

**Architecture:** Portal 通过 Frigate Docker 私网 API 读取快照与有效录像预设，管理接口只注册到现有 `AdminHandler` 并沿用身份、同源和管理请求头校验。浏览器维护待应用组状态，一次提交所有变更；Portal 使用互斥锁校验白名单、更新固定预设并只触发一次 Frigate 重启。公开播放器只增加回放声音控制，不暴露任何管理 API 或布局。

**Tech Stack:** Go 1.24、`net/http`、嵌入式 HTML/CSS/JavaScript、Node.js 内置测试、Docker Compose、Frigate 0.17.2 内部 API。

---

### Task 1: 摄像头组配置模型与解析

**Files:**
- Modify: `camera-portal/internal/portal/http.go`
- Modify: `camera-portal/cmd/camera-portal/main.go`
- Modify: `camera-portal/internal/portal/http_test.go`

**Step 1: Write the failing test**

为两个固定槽位、未接入占位、重复 ID、未知摄像头 ID 和默认回退配置添加表驱动测试；有效示例为 `yard=院子:ptz_view|fixed_view;front=门前:|`。

**Step 2: Run test to verify it fails**

Run: `go test ./internal/portal ./cmd/camera-portal`
Expected: FAIL，因为摄像头组类型和解析器尚不存在。

**Step 3: Write minimal implementation**

增加 `CameraGroup`/`CameraSlot`，每组规范化为两个槽位；只允许组引用 `CAMERAS` 中已配置的 ID，空 ID 保留为“待接入”。无 `CAMERA_GROUPS` 时生成包含现有两路的兼容组。

**Step 4: Run test to verify it passes**

Run: `go test ./internal/portal ./cmd/camera-portal`
Expected: PASS。

### Task 2: Frigate 读取、快照和组级录音应用服务

**Files:**
- Create: `camera-portal/internal/portal/camera_control.go`
- Create: `camera-portal/internal/portal/camera_control_test.go`
- Modify: `camera-portal/internal/portal/http.go`

**Step 1: Write the failing test**

使用 `httptest.Server` 覆盖：读取当前两种固定录像预设、组内状态不一致、低清快照代理、未知 ID 拒绝、批量更新只重启一次、并发操作返回冲突、上游失败不伪装成功。

**Step 2: Run test to verify it fails**

Run: `go test ./internal/portal -run 'Camera|RecordingAudio|Snapshot' -count=1`
Expected: FAIL，因为控制服务和路由尚不存在。

**Step 3: Write minimal implementation**

实现带超时的 Frigate 客户端。管理接口只接受 `{groups:{groupID:boolean}}`，服务端映射为 `preset-record-generic` 或 `preset-record-generic-audio-aac`；拒绝任意 FFmpeg 参数。更新所有变化后调用一次 `/api/restart`，随后轮询 `/api/config` 验证有效状态。

**Step 4: Run test to verify it passes**

Run: `go test ./internal/portal -run 'Camera|RecordingAudio|Snapshot' -count=1`
Expected: PASS。

### Task 3: 管理页横向摄像头分组界面

**Files:**
- Modify: `camera-portal/web/admin.html`
- Modify: `camera-portal/web/assets/admin.css`
- Modify: `camera-portal/web/assets/admin.js`
- Modify: `camera-portal/web/embed_test.go`

**Step 1: Write the failing test**

断言管理页包含独立摄像头区域、应用按钮、一次性快照加载函数和待应用状态；断言公开 `client.html` 不包含管理控件。

**Step 2: Run test to verify it fails**

Run: `go test ./web -count=1`
Expected: FAIL，因为新区域尚不存在。

**Step 3: Write minimal implementation**

管理页首次加载组状态并仅为非空槽位请求一次快照。同组卡片包在不可拆分容器中，组内间距小、组间距大；第二组两个空槽显示“待接入”。滑块只更新本地待应用值，统一按钮二次确认后提交并展示重启进度。

**Step 4: Run test to verify it passes**

Run: `go test ./web -count=1`
Expected: PASS。

### Task 4: 公开回放单路声音控制

**Files:**
- Modify: `camera-portal/web/assets/client.js`
- Modify: `camera-portal/web/assets/common.css`
- Create: `camera-portal/web/tests/replay-audio.test.mjs`

**Step 1: Write the failing test**

覆盖：双画面默认静音、仅全屏当前回放可手动解除静音、切换画面/日期/模式或退出全屏立即重新静音、直播始终不启用声音。

**Step 2: Run test to verify it fails**

Run: `node --test camera-portal/web/tests/*.test.mjs`
Expected: FAIL，因为回放声音状态机尚不存在。

**Step 3: Write minimal implementation**

增加小型喇叭按钮和单一 `audibleReplayCamera` 状态。按钮仅在回放全屏启用；所有状态转换先静音全部播放器，再按用户手势解除当前录像静音。

**Step 4: Run test to verify it passes**

Run: `node --test camera-portal/web/tests/*.test.mjs`
Expected: PASS。

### Task 5: 安全、回归与容器构建验证

**Files:**
- Modify: `camera-portal/internal/portal/http_test.go`
- Modify: `camera-portal/README.md`
- Modify: `camera-portal/.env.example`
- Modify: `camera-portal/compose.yaml`

**Step 1: Write the failing test**

确认 PublicHandler 对所有管理摄像头路由返回 404，AdminHandler 无身份返回 401/403，修改请求缺少同源或管理头被拒绝，未知组/摄像头 ID 被拒绝。

**Step 2: Run test to verify it fails**

Run: `go test ./... -count=1`
Expected: 新增安全断言在实现前失败。

**Step 3: Complete configuration and documentation**

补充 `CAMERA_GROUPS` 示例、录音切换会导致 15–60 秒中断、历史录像不变、第二组占位及回放声音行为。

**Step 4: Run full verification**

Run: `gofmt -w camera-portal/cmd/camera-portal/main.go camera-portal/internal/portal/*.go`

Run: `go test ./... -count=1`

Run: `node --test web/tests/*.test.mjs`（工作目录 `camera-portal`）

Run: `docker compose -f camera-portal/compose.yaml config`

Run: `docker build -t camera-portal:audio-controls-test camera-portal`

Expected: 全部 PASS，Compose 配置有效，镜像构建成功。

### Task 6: 服务器备份、部署与真实录像验证

**Files:**
- Server backup: `/home/lol/camera-portal-backups/<timestamp>/`
- Legacy server Portal working directory（历史参考，不用于新安装）
- Legacy server Frigate config（历史参考；当时只经 Frigate API 写入固定预设）

**Step 1: Verify deployment target**

比较服务器文件哈希、Compose 状态和当前容器网络，发现服务器存在未同步人工修改时先停止覆盖并合并。

**Step 2: Create recoverable backup**

复制 Portal 当前代码、Compose 和环境配置到时间戳目录；记录现有镜像 ID 与 Frigate 当前录像预设。

**Step 3: Deploy Portal only**

构建并重建 `camera-portal`，不重启 Cloudflare、Caddy 或 Frigate；验证公网监控和 Tailscale 管理入口均正常。

**Step 4: Apply recording setting once**

通过新管理 API 将当前“院子”组设为无声，触发一次 Frigate 重启；等待健康恢复。

**Step 5: Verify media and access boundaries**

用 `ffprobe` 验证新录像无音轨、旧录像仍有 AAC；验证直播/回放可用、管理快照一次加载、公开管理 URL 为 404、第二组只显示两个占位槽。

**Step 6: Commit and hand off**

提交本地实现，更新项目状态和回滚说明；报告服务器备份位置、实际中断时长及验证结果。
