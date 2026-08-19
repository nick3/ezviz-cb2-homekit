#!/bin/bash

set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/.." && pwd)"
token_file="$project_dir/.tmp/ezviz_token.json"
dependency_dir="$project_dir/.tmp/pyEzvizApiCN"
python_bin="$project_dir/.tmp/ezviz-venv/bin/python"
go2rtc_binary="$project_dir/go2rtc"
go2rtc_config="$project_dir/go2rtc.yaml"
go2rtc_log="$project_dir/.tmp/ezviz-homekit.log"
activity_file="$project_dir/.tmp/ezviz-stream-active.json"
go2rtc_pid=""

cleanup() {
  if [[ -n "$go2rtc_pid" ]]; then
    kill "$go2rtc_pid" 2>/dev/null || true
    wait "$go2rtc_pid" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

if [[ ! -x "$python_bin" ]] \
  || [[ ! -f "$dependency_dir/pyezvizapi/cas.py" ]] \
  || [[ ! -f "$script_dir/probe-ezviz-direct-reverse.py" ]] \
  || [[ ! -f "$script_dir/ezviz_direct_media.py" ]] \
  || [[ ! -f "$script_dir/ezviz_warm_controller.py" ]] \
  || [[ ! -f "$script_dir/migrate-ezviz-config.py" ]] \
  || [[ ! -f "$script_dir/ezviz_network_lock.py" ]]; then
  echo "缺少 CB2 局域网直连适配器，请先运行 scripts/install-ezviz-cloud-bridge.sh。" >&2
  exit 1
fi

if [[ ! -x "$go2rtc_binary" ]]; then
  echo "缺少 go2rtc 可执行文件。" >&2
  exit 1
fi

if [[ ! -s "$go2rtc_config" ]]; then
  echo "缺少本地 go2rtc.yaml 配置。" >&2
  exit 1
fi

if ! "$python_bin" "$script_dir/migrate-ezviz-config.py" \
  --config "$go2rtc_config"; then
  echo "本地 go2rtc.yaml 无法安全升级；原配置未被覆盖。" >&2
  exit 1
fi

ezviz_serial="${EZVIZ_SERIAL:-}"
if [[ -z "$ezviz_serial" ]]; then
  ezviz_serial="$(
    sed -nE 's/.*--serial[=[:space:]]+([[:alnum:]-]+).*/\1/p' \
      "$go2rtc_config" | head -n 1
  )"
fi
if [[ ! "$ezviz_serial" =~ ^[[:alnum:]-]{6,}$ ]]; then
  echo "无法从本地配置读取完整设备序列号；请设置 EZVIZ_SERIAL。" >&2
  exit 1
fi

homekit_pin="$(
  sed -nE 's/^[[:space:]]*pin:[[:space:]]*"?([0-9]{3}-[0-9]{2}-[0-9]{3})"?.*/\1/p' \
    "$go2rtc_config"
)"
homekit_pin="${homekit_pin%%$'\n'*}"
homekit_is_paired=false
if grep -Eq '^[[:space:]]*-[[:space:]]*client_id=' "$go2rtc_config"; then
  homekit_is_paired=true
fi

if [[ ! -s "$token_file" ]]; then
  echo "尚未登录萤石云，请先运行 scripts/login-ezviz-cloud.sh。" >&2
  exit 1
fi

if [[ "$(stat -f '%Lp' "$token_file")" != "600" ]]; then
  echo "萤石会话文件权限不安全；请执行 chmod 600 .tmp/ezviz_token.json。" >&2
  exit 1
fi

ffmpeg_bin="${FFMPEG_BIN:-}"
if [[ -z "$ffmpeg_bin" ]]; then
  ffmpeg_bin="$(command -v ffmpeg || true)"
fi
ffprobe_bin="${FFPROBE_BIN:-}"
if [[ -z "$ffprobe_bin" ]]; then
  ffprobe_bin="$(command -v ffprobe || true)"
fi
if [[ ! -x "$ffmpeg_bin" ]] || [[ ! -x "$ffprobe_bin" ]]; then
  echo "缺少可执行的 FFmpeg/ffprobe，无法建立或验证 CB2 媒体链路。" >&2
  exit 1
fi
export FFMPEG_BIN="$ffmpeg_bin"
export FFPROBE_BIN="$ffprobe_bin"

if lsof -nP -iTCP:1984 -sTCP:LISTEN 2>/dev/null | grep -q .; then
  echo "端口 1984 已被占用；请先停止现有 go2rtc 实例。" >&2
  exit 1
fi

if lsof -nP -iTCP:8554 -sTCP:LISTEN 2>/dev/null | grep -q .; then
  echo "端口 8554 已被占用；请先停止现有 go2rtc 实例。" >&2
  exit 1
fi

if lsof -nP -iUDP:8443 2>/dev/null | grep -q .; then
  echo "端口 8443 已被占用；请先停止占用它的服务。" >&2
  exit 1
fi

cd "$project_dir"

warm_seconds="${EZVIZ_WARM_SECONDS:-600}"
if [[ ! "$warm_seconds" =~ ^[0-9]+$ ]] \
  || (( warm_seconds < 60 || warm_seconds > 86400 )); then
  echo "EZVIZ_WARM_SECONDS 必须是 60 到 86400 的整数。" >&2
  exit 1
fi
power_mode="${EZVIZ_POWER_MODE:-auto}"
if [[ "$power_mode" != "auto" && "$power_mode" != "mains" && "$power_mode" != "battery" ]]; then
  echo "EZVIZ_POWER_MODE 只能是 auto、mains 或 battery。" >&2
  exit 1
fi
homekit_transcode="${EZVIZ_HOMEKIT_TRANSCODE:-on_demand}"
if [[ "$homekit_transcode" != "on_demand" && "$homekit_transcode" != "continuous" ]]; then
  echo "EZVIZ_HOMEKIT_TRANSCODE 只能是 on_demand 或 continuous。" >&2
  exit 1
fi
pir_preheat="${EZVIZ_PIR_PREHEAT:-on}"
if [[ "$pir_preheat" != "on" && "$pir_preheat" != "off" ]]; then
  echo "EZVIZ_PIR_PREHEAT 只能是 on 或 off。" >&2
  exit 1
fi
power_refresh_seconds="${EZVIZ_POWER_REFRESH_SECONDS:-300}"
if [[ ! "$power_refresh_seconds" =~ ^[0-9]+$ ]] \
  || (( power_refresh_seconds < 30 )); then
  echo "EZVIZ_POWER_REFRESH_SECONDS 必须是不小于 30 的整数。" >&2
  exit 1
fi
pir_poll_seconds="${EZVIZ_PIR_POLL_SECONDS:-15}"
if [[ ! "$pir_poll_seconds" =~ ^[0-9]+$ ]] \
  || (( pir_poll_seconds < 5 || pir_poll_seconds > 300 )); then
  echo "EZVIZ_PIR_POLL_SECONDS 必须是 5 到 300 的整数。" >&2
  exit 1
fi

rm -f "$activity_file"
export EZVIZ_ACTIVITY_FILE="$activity_file"
export EZVIZ_LINGER="${warm_seconds}s"

"$python_bin" "$script_dir/ezviz_warm_controller.py" \
  --serial "$ezviz_serial" \
  --token-file "$token_file" \
  --activity-file "$activity_file" \
  --ffmpeg-bin "$ffmpeg_bin" \
  --warm-seconds "$warm_seconds" \
  --power-mode "$power_mode" \
  --homekit-transcode "$homekit_transcode" \
  --power-refresh-seconds "$power_refresh_seconds" \
  --pir-poll-seconds "$pir_poll_seconds" \
  --pir-preheat "$pir_preheat" \
  -- "$go2rtc_binary" -c "$project_dir/go2rtc.yaml" \
  >"$go2rtc_log" 2>&1 &
go2rtc_pid=$!

for _ in {1..100}; do
  if ! kill -0 "$go2rtc_pid" 2>/dev/null; then
    echo "HomeKit 桥接启动失败：" >&2
    tail -n 30 "$go2rtc_log" >&2
    exit 1
  fi
  if nc -z 127.0.0.1 8554 2>/dev/null; then
    break
  fi
  sleep 0.1
done

if ! nc -z 127.0.0.1 8554 2>/dev/null; then
  echo "go2rtc 没有在预期时间内就绪。" >&2
  tail -n 30 "$go2rtc_log" >&2
  exit 1
fi

echo "正在验证 CB2 局域网直连媒体和 HomeKit 编码，请稍候……"
if ! probe_output=$("$script_dir/check-ezviz-stream.sh" 2>&1); then
  echo "CB2 局域网直连媒体或 HomeKit 编码验证失败：" >&2
  echo "$probe_output" >&2
  echo "最近的桥接诊断：" >&2
  tail -n 40 "$go2rtc_log" >&2
  exit 1
fi

echo
echo "CB2 局域网直连链路以及 HomeKit 视频/音频编码已经验证。"
echo "摄像头媒体直接从设备私网 IP 回连；没有云媒体节点或自动云回退。"
echo "开始取流后，源进程会禁止新建任何外网连接。"
echo "整个链路未使用 Mac 或 iPhone 的显示画面。"
echo "保持此窗口打开。若“家庭”App 里已经有 EZVIZ CB2，直接打开即可。"
if [[ "$homekit_is_paired" == true ]]; then
  echo "当前 HomeKit 身份已经配对，无需再次显示或输入配对码。"
else
  echo "若尚未添加，则选择："
  echo "  添加配件 → 更多选项 → EZVIZ CB2"
  if [[ -n "$homekit_pin" ]]; then
    echo "  配对码：$homekit_pin"
  else
    echo "  当前配置没有设置 homekit.pin，请先在本地 go2rtc.yaml 中生成配对码。"
  fi
  echo "  配对成功后可在“家庭”App 中改名为“萤石摄像头”。"
fi
echo

wait "$go2rtc_pid"
