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

homekit_pin="$(
  sed -nE 's/^[[:space:]]*pin:[[:space:]]*"?([0-9]{3}-[0-9]{2}-[0-9]{3})"?.*/\1/p' \
    "$go2rtc_config"
)"
homekit_pin="${homekit_pin%%$'\n'*}"

if [[ ! -s "$token_file" ]]; then
  echo "尚未登录萤石云，请先运行 scripts/login-ezviz-cloud.sh。" >&2
  exit 1
fi

if [[ "$(stat -f '%Lp' "$token_file")" != "600" ]]; then
  echo "萤石会话文件权限不安全；请执行 chmod 600 .tmp/ezviz_token.json。" >&2
  exit 1
fi

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

"$go2rtc_binary" -c "$project_dir/go2rtc.yaml" >"$go2rtc_log" 2>&1 &
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
echo "若尚未添加，则选择："
echo "  添加配件 → 更多选项 → EZVIZ CB2"
if [[ -n "$homekit_pin" ]]; then
  echo "  配对码：$homekit_pin"
else
  echo "  当前配置没有设置 homekit.pin，请先在本地 go2rtc.yaml 中生成配对码。"
fi
echo "  配对成功后可在“家庭”App 中改名为“萤石摄像头”。"
echo

wait "$go2rtc_pid"
