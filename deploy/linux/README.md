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
   TCP `8099`、UDP `8443` 和 mDNS UDP `5353`。RTSP `8554` 只监听容器
   本机。虚拟机还需出站访问萤石 HTTPS 控制接口。启用 PIR 提前预热时
   建议允许出站
   TCP `1882` 连接萤石告警推送服务；推送注册不可用时会自动改走 HTTPS
   告警轮询。
5. Docker 使用 host network，因为摄像头反向连接、HomeKit mDNS 和 SRTP
   都不适合再经过 Docker 端口映射。

host network 会让容器共享宿主机网络栈，因此建议放在专用、可信的家庭
Linux 虚拟机上。本配置同时使用非 root UID、只读根文件系统、删除全部
Linux capabilities 和 `no-new-privileges`，但这些措施不能把 host network
重新变成隔离网络。Web 向导会短暂接收萤石账号和密码，因此不要把 `8099`
映射到公网。向导只通过 HTTPS 接收请求；账号、密码和短信验证码只保存在
请求处理内存中，不会写入状态卷。即使监听所有接口，它也会拒绝环回、链路
本地、RFC 1918、CGNAT 和 IPv6 ULA 之外的客户端；多网卡主机还可通过
`.env` 中的 `EZVIZ_SETUP_HOST` 将它绑定到指定的可信 LAN 地址。
IPv6-only 的可信网络可将该值设为 `::`；启动时会把检测到的 ULA 和链路本地
地址加入证书 SAN，并在日志 URL 中保留链路本地地址所需的接口 scope。

## 首次部署

需要 Docker Engine 和 Docker Compose 插件。把 `compose.yaml` 放到 Linux
服务器的空目录中，然后直接运行：

```bash
docker compose up -d
```

Compose 默认拉取以下预构建镜像，不需要下载源码或在服务器上编译：

```text
ghcr.io/nick3/ezviz-cb2-homekit:latest
```

要让全新服务器无需 `docker login` 即可执行这条命令，GHCR 容器包必须设为
`Public`。GitHub 不允许公开后的容器包再改回私有，因此该可见性需要由仓库
所有者在 Package settings 中明确确认；包仍为私有时需先登录 GHCR。

镜像同时支持 `linux/amd64` 与 `linux/arm64`。启动后打开：

```text
https://<Linux 服务器局域网 IP>:8099
```

首次启动会在状态卷中生成本部署独有的自签 TLS 证书和私钥，浏览器因此会
显示证书不受公共 CA 信任。继续访问前先运行 `docker compose logs bridge`，
将日志中的 `TLS 证书 SHA-256` 与浏览器证书详情中的 SHA-256 指纹逐字比对；
匹配后再提交萤石账号。证书会跨容器升级保留，因此正常重启不会更换指纹。
若 Linux 主机 IP 或 `EZVIZ_SETUP_HOST` 发生变化，启动时会检测证书是否覆盖
当前地址，并在缺少所需 SAN 时自动重签；运行期间也会定期检查 SAN 与剩余
有效期，并在需要时热加载新证书，无需重启桥接。每次重签后都必须按日志重新
核对新指纹。私钥不得复制给其他主机。

Web 向导会依次完成：

1. 通过 SADP `inquiry` / `inquiry_v32` 在当前 VLAN 自动搜索萤石/Hikvision
   设备，按完整序列号或末尾字符匹配，并自动填入摄像头 IP；搜索不到时仍
   可手动填写。该过程只发送设备发现报文，不逐个扫描局域网端口。电池
   摄像头休眠、不回应 SADP 时，可使用同页“登录萤石辅助查找”：向导先按
   序列号后缀定位账号中的设备，再读取 `WIFI.address` / `CONNECTION.localIp`；
   元数据没有地址时会发送一次官方唤醒控制并重试局域网发现。
2. 配置电源识别、10 分钟保温、PIR 提前预热以及按需/持续转码策略。
3. 登录萤石并处理可能出现的短信验证码。只有权限为 `0600` 的会话令牌
   会写入 Docker 状态卷；认证同时绑定完整序列号和 API 区域，修改任一项后
   都必须重新登录，避免沿用错误设备或区域的旧会话。
4. 自动启动 go2rtc，并显示 HomeKit 配对码。

保存设置或重新登录后，桥接进程会自动重载，不需要再次运行 Compose 命令。
首次配置完成前容器也会保持运行并呈现健康状态，因为此时 Web 向导就是
预期服务。

标准 Docker 命名卷中的 `/data` 预置为 `1000:1000`、权限 `0700`，因此普通
部署必须保持 `PUID=1000`、`PGID=1000`。只有已经在宿主机上把状态卷完整
预置为另一组数字 UID/GID 时才可改这两个值；属主不匹配时桥接容器会拒绝
启动，并在日志中显示卷属主、权限与当前进程 UID/GID，避免以不安全权限
继续运行。

Compose 还包含一个先于桥接运行的一次性 `migrate` 服务。它没有网络、根文件
系统只读、删除其他 capability，只临时保留读取旧权限目录和为新卷设置属主
所需的 `DAC_OVERRIDE`、`FOWNER`、`CHOWN`；旧 `./data` 只读挂载给该服务，
不会暴露给长期运行的桥接容器。全新部署可能因此看到一个空的宿主机
`data/` 目录，实际新状态仍只写入 Docker 命名卷。

项目在每次推送 `main` 或 `v*` 标签时通过 GitHub Actions 构建镜像。

若仓库已配置 Docker Hub 发布凭据，同一份多架构镜像也会同步到：

```text
<Docker Hub 用户名>/ezviz-cb2-homekit:latest
```

若需要固定版本或改用私有镜像，可在同目录 `.env` 中设置
`EZVIZ_HOMEKIT_IMAGE`。正常部署不需要创建或编辑 `.env`。GHCR 仓库若被
改为私有，需先用具备 `read:packages` 权限的令牌登录 GHCR。

Docker Hub 首次接入不应把密码或令牌发到聊天或写入文件。在仓库根目录
运行 `./scripts/configure-dockerhub-publish.sh`，按提示输入 Docker Hub
用户名和带 Read & Write 权限的访问令牌；脚本会直接写入 GitHub Actions
变量/密钥并重新触发发布。

只有需要审查源码或本地构建镜像时，才需要在当前 Mac 工作区生成无凭据
源码包：

```bash
./deploy/linux/manage.sh bundle /目标路径/ezviz-homekit-linux-src.tar.gz
```

把它复制到 Linux，解压到空目录，再进入解压后的 `deploy/linux/`。令牌、
HomeKit 配对和当前设备配置都不会进入这个压缩包；需要保留配对时按后文
单独安全传输两个状态文件。脚本还会生成同名 `.sha256` 文件，复制后可用
`sha256sum -c` 检查传输完整性。

本地构建会从固定提交下载 pyEzvizApiCN，并使用本目录锁定的 Python 依赖
版本。原生架构构建还会在镜像内实际执行一次 seccomp 自检；
已有摄像头 socket 必须继续可用，而创建新 socket 必须被内核拒绝，否则
镜像构建直接失败。

```bash
docker build -t ezviz-homekit:local -f deploy/linux/Dockerfile .
EZVIZ_HOMEKIT_IMAGE=ezviz-homekit:local EZVIZ_PULL_POLICY=never \
  docker compose -f deploy/linux/compose.yaml up -d
```

`./manage.sh verify` 会主动取流并可能唤醒电池摄像头；容器自身的周期健康
检查只读取监督器状态，不会连接或唤醒摄像头。启用
PIR 预热后，匹配本机序列号的移动/人体告警也会触发一次限时取流。

从旧版 `./data:/data` 部署升级时，应在原部署目录中保留 `.env` 和 `data/`，
替换 Compose 文件后直接运行 `docker compose up -d`。一次性迁移服务会先从
只读旧目录把 HomeKit 身份、配对列表和萤石会话自动复制到空命名卷，再把新
状态属主设为桥接所用的 `PUID`/`PGID`；因此旧版曾由任意宿主机 UID/GID
运行也可升级，不依赖旧 `.env` 是否记录了当时的数字 ID。旧 `.env` 可在登录
后被修改，无法安全证明现有令牌仍属于当前序列号和 API 区域；没有完整认证
sidecar 的旧令牌会保留但标记为“未绑定”，升级后需在 Web 向导重新登录一次。
HomeKit 配对关系不受影响，无需重新配对。已有命名卷状态绝不会被旧目录覆盖，
原 `data/` 也不会被修改。确认“家庭”画面和 Web 向导状态正常后，再把两处状态
一起纳入密钥级备份，并安全清理不再需要的旧副本。

迁移会拒绝符号链接、权限不是 `0600` 的令牌以及损坏状态，并在生成任何新
HomeKit 身份前阻止桥接启动；意外中断的复制会在下次启动时安全续传。遇到
错误先查看 `docker compose logs migrate`，备份旧目录后修正提示的问题再
重新执行 `docker compose up -d`；不要直接删除旧状态。

迁移完成后，启动入口还会检查持久化配置版本。旧版受管配置会自动换成当前
模板，同时保留完整 `homekit` 段、配对身份和配对列表；原文件会以权限
`0600` 备份为状态卷中的 `go2rtc.yaml.pre-v3.bak`。该备份同样包含 HomeKit
状态，不得提交到 Git 或通过不安全渠道传输。若固定备份与当前待迁移配置
不同，入口会保留两者，并为当前配置另建带内容哈希后缀的 `0600` 备份。

在 Apple“家庭”中选择“添加配件 → 更多选项 → EZVIZ CB2”，输入 Web 向导
显示的配对码。常用维护命令：

```bash
./manage.sh status
./manage.sh logs
./manage.sh restart
./manage.sh down
```

如果日志提示设备元数据没有 CAS 地址，先在 Web 向导中重新登录萤石。向导
保存新会话后会自动重启桥接。这通常表示萤石会话已过期或该次官方接口没有
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

这些配置可直接在 Web 向导中调整。旧版 `.env` 部署首次启动时会自动导入
以下同名环境变量，之后以状态卷中的 Web 配置为准：

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

在新 Linux 主机首次执行 `docker compose up -d` 之前，把 Mac 上的 `go2rtc.yaml`
和 `.tmp/ezviz_token.json` 安全复制到临时目录，然后运行：

```bash
chmod 600 /安全路径/ezviz_token.json
./manage.sh import-state /安全路径/go2rtc.yaml /安全路径/ezviz_token.json
```

该迁移命令需要源码包中的 `manage.sh`。它通过无网络、只读根文件系统的一次性
root helper 读取宿主机上的 `0600` 文件，因此宿主用户 UID 不需要与 `PUID`
一致；helper 只保留读取源文件和把目标状态交还给 `PUID`/`PGID` 所需的最小
capability，源文件权限不会被放宽。导入器只保留旧配置的 `homekit` 段，并将
它放入新的 Linux 配置，因此会保留配对码、配件身份和 Apple Home 配对列表，
但不会带入 Mac 路径、旧 IP 或 VideoToolbox 设置。目标状态卷已有状态时它会
拒绝覆盖。旧令牌无法可靠证明自己属于哪个序列号，因此导入后会明确标记为
“未绑定”，不会据此启动桥接；打开 Web 向导重新登录一次萤石后才会把新会话
绑定到当前摄像头。HomeKit 配对关系不受这次重新登录影响。

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
- Docker 命名卷 `ezviz-homekit_ezviz-data` 中的 `go2rtc.yaml` 含 HomeKit
  配对信息，`ezviz_token.json` 含萤石会话，`wizard-key.pem` 是 Web 向导
  TLS 私钥，`settings.json` 含非密码运行参数。整个状态卷必须按密钥材料
  备份和传输，不要提交到 Git。
- 常电策略启动并完成首次取流后，后续查看通常无需再次唤醒。纯电池模式在
  10 分钟保温或 PIR 预热窗口内也可直接复用现有流；窗口到期后的下一次查看
  仍是冷启动，通常需要十几秒。保温和事件预热都会增加耗电。
