# EZVIZ CB2 → HomeKit：Linux 部署

这套部署保留当前已经验证的链路：萤石 API/CAS 只负责鉴权、唤醒和邀请；
摄像头随后从自己的局域网地址主动连接 Linux 虚拟机。连接建立后，源进程
通过 Linux seccomp 禁止创建或连接任何新 socket，媒体不会自动回退到
VTM/VTDU 云节点，也不读取 Mac 或 iPhone 上显示的画面。

## 网络前提

1. 虚拟机网卡必须使用桥接模式。摄像头应能直接访问虚拟机的局域网地址。
2. 推荐虚拟机、摄像头和 iPhone 位于同一 VLAN；跨 VLAN 时还需要路由、
   防火墙规则以及 mDNS 反射。
3. 为摄像头和虚拟机设置 DHCP 地址保留。
4. 从摄像头地址放行 TCP `39000`；从可信家庭网络放行 TCP `1984`、
   UDP `8443` 和 mDNS UDP `5353`。RTSP `8554` 只监听容器本机。
5. Docker 使用 host network，因为摄像头反向连接、HomeKit mDNS 和 SRTP
   都不适合再经过 Docker 端口映射。

host network 会让容器共享宿主机网络栈，因此建议放在专用、可信的家庭
Linux 虚拟机上。本配置同时使用非 root UID、只读根文件系统、删除全部
Linux capabilities 和 `no-new-privileges`，但这些措施不能把 host network
重新变成隔离网络。

## 首次部署

需要 Docker Engine 和 Docker Compose 插件。进入本目录后运行：

项目在每次推送 `main` 或 `v*` 标签时通过 GitHub Actions 构建
`linux/amd64` 与 `linux/arm64` 镜像。GHCR 地址固定为：

```text
ghcr.io/nick3/ezviz-cb2-homekit:latest
```

若仓库已配置 Docker Hub 发布凭据，同一份多架构镜像也会同步到：

```text
<Docker Hub 用户名>/ezviz-cb2-homekit:latest
```

默认 Compose 配置继续使用本机源码构建，便于审查和修改。若要在 Linux
直接使用预编译镜像，将 `compose.yaml` 中 `image` 改成上述可访问地址并
删除 `build` 段，然后照常执行后续命令。私有 GHCR 镜像需要先登录 GHCR。

Docker Hub 首次接入不应把密码或令牌发到聊天或写入文件。在仓库根目录
运行 `./scripts/configure-dockerhub-publish.sh`，按提示输入 Docker Hub
用户名和带 Read & Write 权限的访问令牌；脚本会直接写入 GitHub Actions
变量/密钥并重新触发发布。

可以先在当前 Mac 工作区生成一个不含任何设备状态或凭据的迁移包：

```bash
./deploy/linux/manage.sh bundle /目标路径/ezviz-homekit-linux-src.tar.gz
```

把它复制到 Linux，解压到空目录，再进入解压后的 `deploy/linux/`。令牌、
HomeKit 配对和当前设备配置都不会进入这个压缩包；需要保留配对时按后文
单独安全传输两个状态文件。脚本还会生成同名 `.sha256` 文件，复制后可用
`sha256sum -c` 检查传输完整性。

随后运行：

```bash
./manage.sh init
```

编辑 `.env`，填写完整摄像头序列号、摄像头固定 IP 和虚拟机 LAN IP。
默认使用 CPU 软件编码，在没有 GPU 的虚拟机中也可运行。

首次构建会从固定提交下载 pyEzvizApiCN，并使用本目录锁定的 Python 依赖
版本。原生架构构建还会在镜像内以非 root 用户实际执行一次 seccomp 自检；
已有摄像头 socket 必须继续可用，而创建新 socket 必须被内核拒绝，否则
镜像构建直接失败。

```bash
./manage.sh login
./manage.sh up
./manage.sh verify
./manage.sh pin
```

`login` 会交互询问账号、密码和可能出现的短信验证码。账号、密码与验证码
不会保存；只有权限为 `0600` 的会话令牌会写入 `data/`。`verify` 是唯一会
主动唤醒电池摄像头的健康检查；容器自身的周期健康检查只检查本地端口。

在 Apple“家庭”中选择“添加配件 → 更多选项 → EZVIZ CB2”，输入 `pin`
命令显示的配对码。常用维护命令：

```bash
./manage.sh status
./manage.sh logs
./manage.sh restart
./manage.sh down
```

如果日志提示设备元数据没有 CAS 地址，先重新运行 `./manage.sh login`，再
执行 `./manage.sh restart`。这通常表示萤石会话已过期或该次官方接口没有
返回完整设备元数据，不会触发云媒体回退。

## 从当前 Mac 迁移

在新 Linux 主机首次执行 `login` 或 `up` 之前，把 Mac 上的 `go2rtc.yaml`
和 `.tmp/ezviz_token.json` 安全复制到临时目录，然后运行：

```bash
chmod 600 /安全路径/ezviz_token.json
./manage.sh import-state /安全路径/go2rtc.yaml /安全路径/ezviz_token.json
```

导入器只保留旧配置的 `homekit` 段，并将它放入新的 Linux 配置，因此会
保留配对码、配件身份和 Apple Home 配对列表，但不会带入 Mac 路径、旧 IP
或 VideoToolbox 设置。目标 `data/` 已有状态时它会拒绝覆盖。

## 编码器

- `EZVIZ_ENCODER=software`：默认使用 libx264，适合普通虚拟机。
- `EZVIZ_ENCODER=vaapi`：Intel/AMD 核显，需要把 `/dev/dri` 传入容器并
  给运行用户 render 组权限。
- `EZVIZ_ENCODER=cuda`：NVIDIA，需要宿主机的 NVIDIA 容器运行时。
- `auto`、`v4l2m2m`、`rkmpp` 仅用于已经配置对应硬件透传的主机。

不确定时保持 `software`。1080p/15fps 软件转码的实际 CPU 占用取决于虚拟机
所分配的宿主 CPU；建议先分配 2–4 个 vCPU，再根据运行时负载调整。

## 限制和安全边界

- 冷启动仍需要互联网访问萤石官方 API/CAS。局域网媒体连接建立后，
  Python 源进程会先确认自己只剩摄像头媒体 socket，再加载 seccomp 规则。
- seccomp 同步应用到该进程的全部线程，并由后续 FFmpeg 子进程继承；规则
  拒绝新的 `socket()` 和 `connect()`，已有摄像头 TCP 流仍可继续读取。
- `data/go2rtc.yaml` 含 HomeKit 配对信息，`data/ezviz_token.json` 含萤石
  会话。整个 `data/` 必须按密钥材料备份和传输，不要提交到 Git。
- CB2 是电池摄像头。每次在“家庭”中打开实时画面都可能需要十几秒唤醒，
  频繁查看会明显增加耗电。
