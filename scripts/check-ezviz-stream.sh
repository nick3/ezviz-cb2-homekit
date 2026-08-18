#!/bin/bash

set -euo pipefail

ffprobe_bin="/opt/homebrew/bin/ffprobe"

if [[ ! -x "$ffprobe_bin" ]]; then
  echo "缺少 ffprobe，无法检查视频流。" >&2
  exit 1
fi

if ! nc -z -G 1 -w 1 127.0.0.1 8554 >/dev/null 2>&1; then
  echo "go2rtc 尚未运行。请先执行 scripts/start-ezviz-homekit.sh。" >&2
  exit 1
fi

probe_output=$("$ffprobe_bin" \
  -v error \
  -rw_timeout 45000000 \
  -rtsp_transport tcp \
  -show_entries stream=index,codec_type,codec_name,width,height,sample_rate,channels \
  -of json \
  'rtsp://127.0.0.1:8554/ezviz?video=h264&audio=opus')

if ! printf '%s\n' "$probe_output" | grep -q '"codec_type": "video"'; then
  echo "未收到摄像头局域网直连视频轨道。" >&2
  exit 1
fi

if ! printf '%s\n' "$probe_output" | grep -q '"codec_name": "h264"'; then
  echo "未收到 HomeKit 所需的 H.264 视频轨道。" >&2
  exit 1
fi

if ! printf '%s\n' "$probe_output" | grep -q '"codec_name": "opus"'; then
  echo "未收到 HomeKit 所需的 Opus 音频轨道。" >&2
  exit 1
fi

printf '%s\n' "$probe_output"
