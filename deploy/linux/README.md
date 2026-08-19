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
   UDP `8443` 和 mDNS UDP `5353`。RTSP `8554` 只监听容器本机。虚拟机
   还需出站访问萤石 HTTPS 控制接口。启用 PIR 提前预热时建议允许出站
   TCP `1882` 连接萤石告警推送服务；推送注册不可用时会自动改走 HTTPS
   告警轮询。
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
不会保存；只有权限为 `0600` 的会话令牌会写入 `data/`。`verify` 会主动
取流并可能唤醒电池摄像头；容器自身的周期健康检查只检查本地端口。启用
PIR 预热后，匹配本机序列号的移动/人体告警也会触发一次限时取流。

升级已有部署时，启动入口会检查持久化配置版本。旧版受管配置会自动换成
当前模板，同时保留完整 `homekit` 段、配对身份和配对列表；原文件会以权限
`0600` 备份为 `data/go2rtc.yaml.pre-v3.bak`。该备份同样包含 HomeKit 状态，
不得提交到 Git 或通过不安全渠道传输。若固定备份与当前待迁移配置不同，
入口会保留两者，并为当前配置另建带内容哈希后缀的 `0600` 备份。

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

## 自适应预热策略

默认 `EZVIZ_POWER_MODE=auto`。控制器每 5 分钟读取一次设备的实时供电状态；
只有工作模式和电源状态都明确报告为持续供电时，才进入常电策略。任何字段
缺失、互相矛盾或连续读取失败都会按电池策略处理，避免误判后把电池耗空。

- 持续供电：默认保持一个只连接容器本机 RTSP 的 H.265/AAC 消费者，摄像头
  局域网连接常驻，但 HomeKit 的 H.264/Opus 转码器只在打开“家庭”时启动。
  选择 `continuous` 后才会把 HomeKit 转码器也常驻。
- 纯电池：最后一个观看者退出后，H.264/Opus 转码器立即停止，只保留原始
  摄像头连接 10 分钟；在窗口内再次打开通常只需等待本地编码器启动。窗口
  到期后允许设备正常休眠。
- PIR 提前预热：收到该摄像头的移动/人体告警后，提前建立同一条局域网
  媒体链路并保温 10 分钟；重复事件会顺延窗口。优先使用推送；若推送注册
  被服务端拒绝或 TCP `1882` 不可达，自动改为默认每 15 秒读取一次最新告警。
  首次轮询只建立基线，不会把历史告警误当成新事件重放。
- 保活请求只会在局域网媒体源进程确实存在时续期。休眠状态没有定时空转
  唤醒，也没有云媒体备用源。

`.env` 中可调整以下配置：

```dotenv
EZVIZ_POWER_MODE=auto          # auto、mains 或 battery
EZVIZ_HOMEKIT_TRANSCODE=on_demand # on_demand 或 continuous
EZVIZ_WARM_SECONDS=600         # 60–86400 秒；默认 10 分钟
EZVIZ_PIR_PREHEAT=on           # 不需要任何事件提前预热时设为 off
EZVIZ_PIR_POLL_SECONDS=15      # 5–300 秒；推送不可用时的备用轮询周期
EZVIZ_POWER_REFRESH_SECONDS=300 # 不小于 30 秒
```

仅当摄像头确实持续连接 5V 电源但固件误报时才强制设为 `mains`；纯电池设备
不要使用该覆盖值。若实际插拔电源后自动模式没有在一个刷新周期内切换，可在
日志中核对策略并临时设为 `battery`。

`on_demand` 是默认值，适合降低常电主机的持续转码负载；摄像头原始局域网
连接仍保持热状态，打开“家庭”时才启动 H.264/Opus。若更看重最短打开延迟
且能接受持续编码负载，将 `EZVIZ_HOMEKIT_TRANSCODE` 设为 `continuous`。

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
所分配的宿主 CPU；建议先分配 2–4 个 vCPU，再根据运行时负载调整。默认
`on_demand` 只在观看时占用这部分算力；`continuous` 会持续占用编码资源。

## 限制和安全边界

- 冷启动仍需要互联网访问萤石官方 API/CAS。局域网媒体连接建立后，
  Python 源进程会先确认自己只剩摄像头媒体 socket，再加载 seccomp 规则。
- seccomp 同步应用到该进程的全部线程，并由后续 FFmpeg 子进程继承；规则
  拒绝新的 `socket()` 和 `connect()`，已有摄像头 TCP 流仍可继续读取。
- `data/go2rtc.yaml` 含 HomeKit 配对信息，`data/ezviz_token.json` 含萤石
  会话。整个 `data/` 必须按密钥材料备份和传输，不要提交到 Git。
- 常电策略启动并完成首次取流后，后续查看通常无需再次唤醒。纯电池模式在
  10 分钟保温或 PIR 预热窗口内也可直接复用现有流；窗口到期后的下一次查看
  仍是冷启动，通常需要十几秒。保温和事件预热都会增加耗电。
