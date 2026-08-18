#!/bin/bash

set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/.." && pwd)"
python="$project_dir/.tmp/ezviz-venv/bin/python"
config_file="$project_dir/go2rtc.yaml"

if [[ ! -x "$python" ]]; then
  echo "缺少 CB2 云端适配器，请先运行 scripts/install-ezviz-cloud-bridge.sh。" >&2
  exit 1
fi

serial="${EZVIZ_SERIAL:-}"
if [[ -z "$serial" && -f "$config_file" ]]; then
  serial=$(sed -nE 's/.*--serial ([^ ]]+).*/\1/p' "$config_file" | head -n 1)
fi
if [[ -z "$serial" ]]; then
  echo "缺少摄像头完整序列号；请设置 EZVIZ_SERIAL 后重试。" >&2
  exit 1
fi

exec "$python" "$script_dir/login-ezviz-cloud.py" --serial "$serial"
