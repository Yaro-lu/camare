# Frigate 1Panel 编排

本目录保存已经验证的 Frigate 双画面录像模板：

- 一个摄像头提供两条 RTSP 地址；
- `ptz_view` 对应云台画面，默认路径 `/live/101`；
- `fixed_view` 对应固定/枪机画面，默认路径 `/live/201`；
- 只连续录像，不启用目标检测、移动检测、快照和 MQTT；
- Intel 老核显使用 `i965 + preset-vaapi`；
- 录像只写 `${FRIGATE_MEDIA_PATH}`，不会由 Portal 复制。

Frigate 使用官方固定镜像 `0.17.2`，同时锁定已验证的 manifest digest。升级前请阅读 Frigate release notes 并备份 `/config`。

## 准备配置

```sh
cp .env.example .env
mkdir -p secrets
printf '%s' '摄像头RTSP密码' > secrets/FRIGATE_RTSP_PASSWORD
chmod 600 .env secrets/FRIGATE_RTSP_PASSWORD
```

根目录的 `sudo ./install.sh` 会自动生成 `.env` 和 secret，并只修改 `config/config.yml` 中的录像保留天数。覆盖前会生成备份，不会替换 Frigate 数据库或录像。

创建录像目录。目录必须在启动前真实存在：

```sh
sudo mkdir -p /srv/frigate/media
sudo chmod 755 /srv/frigate/media
```

将 `.env` 中 `FRIGATE_MEDIA_PATH` 指向该目录。Compose 使用 `create_host_path: false`，因此外接硬盘未挂载时会启动失败，而不会悄悄写满系统盘。

## 启动

```sh
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
```

首次管理员密码只会出现在启动日志中：

```sh
docker logs frigate 2>&1 | grep -A1 'User: admin'
```

管理入口使用 HTTPS：

```text
https://<FRIGATE_ADMIN_BIND_IP>:<FRIGATE_ADMIN_PORT>/
```

## 硬件加速

默认模板适用于 Intel generation 1–5：

```dotenv
FRIGATE_RENDER_DEVICE=/dev/dri/renderD128
LIBVA_DRIVER_NAME=i965
```

先检查：

```sh
ls -l /dev/dri
docker exec frigate ls -l /dev/dri
```

其他 Intel、AMD、NVIDIA、树莓派或 ARM 主机不能直接照抄这两个参数，应按 Frigate 官方硬件加速文档选择镜像、驱动和 preset。

## 摄像头地址变化

推荐优先在路由器中按摄像头 MAC 做 DHCP 地址保留。无法固定时，可以填写：

```dotenv
CAMERA_MAC=摄像头MAC
CAMERA_SUBNET=192.168.1
```

然后手动执行：

```sh
./find-camera-ip.sh
```

脚本只在当前 RTSP 地址不可达时扫描指定 `/24` 子网，找到相同 MAC 后备份并更新 `.env`，再仅重建 Frigate 服务。

## 录像容量

模板的 `continuous.days` 为 `0.125`，即约三小时。请按实际码率和磁盘容量修改 `config/config.yml`。

不要在系统计划任务中直接删除录像文件；这样会让 Frigate 数据库留下失效记录。需要硬容量上限时，应使用独立分区、逻辑卷、磁盘配额或限定大小的文件系统。

## 验证

```sh
./verify.sh
docker compose logs --tail=200 frigate
```

`/config` 包含 Frigate 配置、数据库和认证状态，应放在本地可靠磁盘并单独备份；录像目录可以放在 NAS/外接硬盘，但必须确保挂载稳定。
