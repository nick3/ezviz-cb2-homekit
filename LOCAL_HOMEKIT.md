# 萤石 CB2 接入 HomeKit

目标设备为 `EZVIZ CB2`。部署时请在本地配置中填写完整设备序列号；
不要把序列号、萤石会话或 HomeKit 配对状态提交到代码仓库。

## 为什么不能照视频直接使用 RTSP

视频中的方法适用于开放 RTSP/ONVIF 的萤石摄像头。CB2 是电池式摄像机，萤石 App 中没有“本地服务/RTSP”入口；实际探测也确认它没有开放 RTSP、ONVIF 或海康本地 SDK 视频端口。因此不能使用常见的 `rtsp://admin:验证码@IP:554/...` 链路。

本工作区已还原官方 SDK 的 `NewDirectReverse` 流程，让摄像头主动回连
Mac 上的局域网 TCP 监听器：

```text
萤石 API/CAS（仅鉴权、唤醒和邀请）
                    ↓
CB2 私网 IP → 反向直连 TCP → 私有帧拆包 → MPEG-TS → go2rtc → HomeKit
```

视频和音频不再经过 VTM/VTDU 云媒体节点。CAS 邀请确认后，取流进程会
应用 macOS 进程沙箱，禁止新建任何外网连接，只保留已经建立的摄像头
局域网媒体 socket。整条链路不截图、不录屏，也不读取 Mac 或 iPhone
上显示的预览画面。

## 已准备的组件

- 中国区协议适配器以 `pyEzvizApiCN` 提交 `717a768185ccbb92f09eacf3f9273352696d8a91` 为基线。`scripts/patches/003-pyezviz-direct-reverse.patch` 加入已验证的 CAS 两层加密帧、反向回连身份检查、实时预览邀请和独立流头解析。
- `scripts/ezviz_direct_media.py` 按 `$ + 类型 + 大端长度` 拆除萤石私有帧，再移除每个媒体块的 4 字节前缀，恢复标准 MPEG-PS。FFmpeg 只做本地转封装，随后由 go2rtc 输出 RTSP/HomeKit。
- `scripts/install-ezviz-cloud-bridge.sh` 保留旧文件名以兼容现有入口；它现在会同时安装并测试局域网直连补丁。遇到不同源码或本地冲突会停止，不会强制覆盖。
- `scripts/ezviz_warm_controller.py` 根据设备实时供电状态切换策略：常电时常驻本地预加载；纯电池时退出观看后保温 10 分钟，并用匹配序列号的 PIR/人体告警提前预热。默认只预加载原始 H.265/AAC，打开“家庭”时才启动 H.264/Opus 转码；也可配置为持续转码。告警推送注册不可用时自动改为 15 秒 HTTPS 轮询；它只用云端读取控制/告警元数据，媒体仍只连接本机 RTSP，不能切换到云媒体。
- go2rtc 的 Web UI、摄像头 API 和 RTSP 转发均不对局域网开放；HomeKit 媒体通过 SRTP 传输。
- 萤石账号和密码只用于首次登录，不写入文件；本机仅保存权限为 `600` 的会话令牌 `.tmp/ezviz_token.json`。
- CB2 局域网原始流实测为 HEVC 1920×1080 视频和 AAC 音频；macOS VideoToolbox 将视频转为 HomeKit 所需的 H.264，音频转为 Opus。
- 正式验收中，源进程在禁用新外网连接后经 go2rtc 连续解码 10 分钟，得到 9003 帧，`dup_frames=0`、`drop_frames=0`。连接表中媒体源唯一远端为摄像头私网 IP。

## 安装与登录

安装或重新验证依赖：

```bash
./scripts/install-ezviz-cloud-bridge.sh
```

首次登录或会话过期后运行：

```bash
./scripts/login-ezviz-cloud.sh
```

脚本会在终端询问萤石账号、密码以及可能出现的短信验证码。密码和验证码输入时不显示。登录仅访问萤石中国区官方接口 `api.ys7.com`，并在确认账号中存在目标 CB2 后保存会话。

## 启动与配对

```bash
./scripts/start-ezviz-homekit.sh
```

启动脚本会验证完整链路。只有摄像头完成局域网回连，并同时收到 HomeKit
兼容的 H.264 视频和 Opus 音频后，才会提示桥接已经就绪；失败时会输出
不含令牌、操作码或媒体内容的阶段诊断。配置中没有云媒体备用源，因此
直连失败时会明确失败，不会悄悄切回 VTM/VTDU。

默认使用自适应预热：设备明确报告持续供电时保持局域网流常驻；其余情况按
纯电池处理，最后一次观看后默认保温 10 分钟（600 秒），并监听 PIR/人体
告警提前预热。
固件供电状态误报时可在启动前用 `EZVIZ_POWER_MODE=mains` 或 `battery`
覆盖；只有确实持续连接 5V 电源时才应强制 `mains`。

macOS 启动脚本支持以下覆盖项：

```bash
EZVIZ_WARM_SECONDS=600          # 60–86400 秒；退出保温与 PIR 预热共用
EZVIZ_PIR_PREHEAT=on            # on 或 off
EZVIZ_PIR_POLL_SECONDS=15       # 5–300 秒；推送离线时的轮询周期
EZVIZ_POWER_REFRESH_SECONDS=300 # 不小于 30 秒
```

FFmpeg 默认通过 `PATH` 自动查找；自定义安装位置可设置 `FFMPEG_BIN` 和
`FFPROBE_BIN`。

HomeKit 转码默认按需启动，无人观看时常驻的只是摄像头原始流：

```bash
EZVIZ_HOMEKIT_TRANSCODE=on_demand ./scripts/start-ezviz-homekit.sh
```

若机器持续供电且更看重最短打开延迟，可改为持续转码：

```bash
EZVIZ_HOMEKIT_TRANSCODE=continuous ./scripts/start-ezviz-homekit.sh
```

验证成功后保持该进程运行。若“家庭”App 中已有 `EZVIZ CB2`，直接打开即可；否则选择：

```text
添加配件 → 更多选项 → EZVIZ CB2
```

配对信息由启动脚本显示。配对后可在“家庭”App 中将配件改名为“萤石摄像头”。go2rtc `v1.9.14` 会替换 mDNS 实例名中的非 ASCII 字符，所以发现阶段使用英文名称。

另开终端可重复检查完整 HomeKit 媒体轨道：

```bash
./scripts/check-ezviz-stream.sh
```

成功结果应同时包含 `h264` 视频和 `opus` 音频。

## 使用限制

- 这是“云控制、局域网媒体”，不是完全脱离萤石服务的冷启动方案。每次新建预览仍需互联网完成鉴权、唤醒和 CAS 邀请；媒体连接建立后不再需要外网。若在启动前就断开 WAN，当前固件无法自行建立这条反向连接。
- 持续供电且首次预加载成功后，打开“家庭”无需重新唤醒摄像头。默认按需转码会多出编码器启动时间，但远短于摄像头冷启动；`continuous` 模式可消除这部分等待。纯电池模式只在退出后的 10 分钟保温窗口或 PIR 预热窗口内复用热流；窗口到期后仍会休眠，下一次冷启动通常需要十几秒。
- 保温和 PIR 提前预热会增加电池耗电。源进程实际存在时才会续期休眠倒计时，空闲状态不会定时唤醒；适配器最多重试三次 CAS 回连。
- PIR 提前预热依赖萤石互联网控制面。推送不可达时自动使用默认 15 秒告警轮询，因此可能比实时推送晚一个轮询周期；两者都不可用时仍不影响手动查看或退出后的 10 分钟保温。
- 当前设备实际稳定输出主码流；请求子码流时曾只返回会话头，因此正式配置固定使用主码流并在本机转为 HomeKit 编码。
- 萤石若调整私有接口、CAS 证书或协议，可能需要更新本地补丁。更新前应重新验证局域网源、进程级断网和 HomeKit 编码轨道。
- 会话过期时重新运行 `scripts/login-ezviz-cloud.sh`，无需把账号密码写入启动配置。

## 迁移到 Linux 虚拟机

`deploy/linux/` 提供自包含的 Docker Compose 部署。它使用桥接网卡、host
network、固定摄像头回调端口、持久化状态目录和 Linux seccomp，默认以
libx264 软件编码运行，不依赖 Mac、iPhone 画面或 VideoToolbox。

完整的首次部署、Mac 配对状态导入、端口规则和 GPU 可选配置见：

```text
deploy/linux/README.md
```
