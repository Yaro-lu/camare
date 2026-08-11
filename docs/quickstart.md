# Quickstart

本指南从空目录创建一个新的 Camare 实例。它不会迁移现有 Frigate/Portal，也不会替你修改路由器、DNS、磁盘挂载、Cloudflare 或 Tailscale 账号。

> `v0.1.0` 已正式发布，本地自动化测试及 Linux x86-64 / CPU / 两路合成 H.264 隔离 E2E 已通过。真实摄像头、Intel VAAPI 和目标 NAS 仍需在各自环境验证；第一次部署不能与现有实例共享 Frigate 数据库或同一录像写入目录。

## 1. 准备主机和摄像头

需要：

- Linux x86-64；
- Python 3.10 或更高；
- Docker Engine，以及支持 `docker compose up --wait` 的 Compose v2；
- 服务器能访问的 H.264 RTSP 摄像头；
- 一个已经存在、可写、重启后仍会挂载的录像目录；
- CPU 模式，或 Intel VAAPI 的 `/dev/dri/renderD<number>`。

先检查基础命令：

```sh
python3 --version
docker info
docker compose version
```

如果录像放在 NAS，先由操作系统配置 NFS/SMB 自动挂载并重启验证。Camare 不创建挂载，也不会在挂载失效时主动改写到另一个目录。`doctor` 只能确认路径存在、可写和剩余空间，不能证明它仍是预期的 NAS mount；部署前还应运行：

```sh
findmnt --target /your/media/path
```

如果挂载失效但空 mountpoint 目录仍存在，Linux 可能把新录像写入系统盘。应在系统级使用可靠的自动挂载/依赖和监控，不能只依赖目录存在检查。

## 2. Clone，并以普通用户初始化

```sh
git clone https://github.com/Yaro-lu/camare.git
cd camare
./install.sh init --config "$PWD/camare.ini"
```

`init` 不需要也不应使用 `sudo`。它创建权限为 `0600` 的 `camare.ini` 和权限为 `0700` 的 `secrets/`。如果目标文件已经存在，命令会拒绝覆盖。

## 3. 编辑 INI

```sh
nano camare.ini
```

最小 LAN 示例已经写在 `camare.example.ini` 中。至少修改：

- `instance_id`：本机唯一的小写实例 ID；
- `runtime_dir`：完整实例目录，格式统一为 `/opt/1panel/docker/compose/camare-<instance-id>`；
- `timezone`：录像使用的服务器时区，例如 `Asia/Shanghai`；
- `lan.bind_ip`：服务器真实拥有的家庭网 IPv4；
- `storage.media`：现有且可写的唯一录像目录；
- `[hardware]`：`cpu` 或 `intel-vaapi`；
- 每个组的摄像头 IP、RTSP 用户名、端口和密码文件；
- 每路流的 RTSP 路径。

CPU 模式必须留空设备和驱动：

```ini
[hardware]
mode = cpu
render_device =
va_driver =
```

Intel VAAPI 示例：

```ini
[hardware]
mode = intel-vaapi
render_device = /dev/dri/renderD128
va_driver = i965
```

新 Intel 平台通常使用 `iHD`，旧平台常见 `i965`；以主机实际 VAAPI 驱动为准。

第二组需要把组 ID 加入 `[camare] groups`，再添加一个 `[group:<id>]` 和它的 1–2 个 `[stream:<group-id>:<stream-id>]`。全局 stream ID 不能重复。完整限制见 [兼容性矩阵](compatibility.md)。

## 4. 创建 0600 密钥文件

不要把摄像头密码或 Tunnel token 写进 INI、Compose、命令行参数或 shell 历史。每个摄像头组使用一个独立文件：

```sh
install -m 600 /dev/null secrets/yard-rtsp-password
python3 -c 'from getpass import getpass; from pathlib import Path; Path("secrets/yard-rtsp-password").write_text(getpass("RTSP password: "), encoding="utf-8")'
chmod 600 camare.ini secrets/yard-rtsp-password
```

文件只能包含一行密码；允许文本工具常见的一个结尾换行，但不能有空行或嵌入换行。第二组创建另一个文件，并在对应 `password_file` 中引用。

Cloudflare 模式还需单独创建 token 文件：

```sh
install -m 600 /dev/null secrets/cloudflare-token
python3 -c 'from getpass import getpass; from pathlib import Path; Path("secrets/cloudflare-token").write_text(getpass("Cloudflare token: "), encoding="utf-8")'
```

配置加载只验证密钥文件和权限；只有渲染阶段会读取值，输出不会显示秘密。

这里的 `0600` 要求针对你维护的输入文件。`apply` 会另外生成运行 secret，并依据 manifest 固定宿主权限：摄像头密码保持 root-only，Cloudflare token 只允许 cloudflared 的非 root UID 读取；只在 Portal 与 LAN Caddy 间共享的内部 LAN key 使用受限组读取。不要手工 `chmod 777` 运行目录来处理容器读权限。

## 5. 验证、预检和计划

```sh
./install.sh validate --config "$PWD/camare.ini"
./install.sh doctor --config "$PWD/camare.ini"
./install.sh plan --config "$PWD/camare.ini"
```

- `validate` 严格检查 INI 结构、路径、端口、组和密钥引用。
- `doctor` 是只读检查，覆盖平台、Docker、端口、媒体目录、VAAPI 和摄像头连通性。它不会改变网络或文件。
- `plan` 在临时目录渲染完整实例并执行 `docker compose config`，随后删除临时文件。

如果普通用户无 Docker socket 或 `/opt/1panel/docker/compose` 检查权限，可用 `sudo ./install.sh doctor ...` 再做一次只读预检。不要为了通过检查而把 Docker socket 或运行目录改成全员可写。

`doctor` 无法在不泄露密码的前提下验证部分受保护 RTSP 的编码时，会返回 `WARN`；最终以部署后的 `verify` 为准。所有 `FAIL` 必须先处理。

## 6. 应用并验证

```sh
sudo ./install.sh apply --config "$PWD/camare.ini" --yes
sudo ./install.sh verify --config "$PWD/camare.ini"
sudo ./install.sh status --config "$PWD/camare.ini"
```

`apply` 只操作这个实例：

1. 再次执行只读预检；
2. 在目标目录外生成暂存树；
3. 校验 manifest、哈希和 Compose；
4. 首次安装时发布到唯一运行目录，升级时逐文件原子替换控制文件；
5. 启动 `camare_<instance-id>` project；
6. 检查容器、Frigate 采集进程、go2rtc producer、录像推进和入口；
7. 失败时尝试恢复上一份控制文件和旧实例。

逐文件替换和异常恢复不等于断电原子。应用期间断电后先运行 `status`/`verify`，不要执行 `down -v` 或 `prune`。

## 7. 打开 LAN 页面

同一家庭网打开：

```text
http://<lan.bind_ip>:<lan.port>/
```

该入口免鉴权，任何能访问这个 IPv4/端口的设备都能观看。确认 LAN 正常后，再根据需要配置 [Cloudflare](access/cloudflare.md) 或 [Tailscale](access/tailscale.md)。

## 8. 后续修改

不要直接编辑运行目录中的 `.env`、Compose 或 Frigate 配置。修改源码 checkout 外的 `camare.ini` 或其引用的密钥，再重复：

```sh
./install.sh validate --config "$PWD/camare.ini"
./install.sh doctor --config "$PWD/camare.ini"
./install.sh plan --config "$PWD/camare.ini"
sudo ./install.sh apply --config "$PWD/camare.ini" --yes
sudo ./install.sh verify --config "$PWD/camare.ini"
```

录像目录不会被复制。修改 `storage.media` 是切换写入位置，不会搬迁旧录像；应先自行完成挂载、权限和数据迁移计划。
