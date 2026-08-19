#!/bin/bash

set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/.." && pwd)"
dependency_dir="$project_dir/.tmp/pyEzvizApiCN"
venv_dir="$project_dir/.tmp/ezviz-venv"
go2rtc_binary="$project_dir/go2rtc"
runtime_python="/Users/nick/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
dependency_repo="https://github.com/liuzexier/pyEzvizApiCN.git"
dependency_commit="717a768185ccbb92f09eacf3f9273352696d8a91"
patches=(
  "$project_dir/scripts/patches/001-pyezviz-cloud-stream-runtime.patch"
  "$project_dir/scripts/patches/002-pyezviz-cloud-stream-tests.patch"
  "$project_dir/scripts/patches/003-pyezviz-direct-reverse.patch"
)

mkdir -p "$project_dir/.tmp"

if [[ ! -x "$runtime_python" ]]; then
  echo "缺少 Python 3.12 运行环境。" >&2
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

go_bin="${GO_BIN:-}"
if [[ -z "$go_bin" ]]; then
  go_bin="$(command -v go || true)"
fi
if [[ ! -x "$go_bin" ]]; then
  echo "缺少 Go 工具链，无法构建 go2rtc。" >&2
  exit 1
fi

if [[ ! -d "$dependency_dir/.git" ]]; then
  git clone --filter=blob:none "$dependency_repo" "$dependency_dir"
  git -C "$dependency_dir" checkout --quiet --detach "$dependency_commit"
fi

if [[ "$(git -C "$dependency_dir" rev-parse HEAD)" != "$dependency_commit" ]]; then
  echo "CB2 云流适配源码基线与已验证版本不一致；为保护本地改动，已停止安装。" >&2
  exit 1
fi

for patch_file in "${patches[@]}"; do
  if [[ ! -s "$patch_file" ]]; then
    echo "缺少 CB2 云流适配补丁：$patch_file" >&2
    exit 1
  fi
  if git -C "$dependency_dir" apply --reverse --check "$patch_file" 2>/dev/null; then
    continue
  fi
  if ! git -C "$dependency_dir" apply --check "$patch_file"; then
    echo "CB2 云流适配补丁与现有源码冲突；未覆盖任何本地改动。" >&2
    exit 1
  fi
  git -C "$dependency_dir" apply "$patch_file"
done

if [[ ! -x "$venv_dir/bin/python" ]]; then
  "$runtime_python" -m venv "$venv_dir"
fi

"$venv_dir/bin/python" -m pip install \
  --disable-pip-version-check \
  --no-input \
  --editable "$dependency_dir" \
  pytest
"$venv_dir/bin/python" -m pip check

(
  cd "$dependency_dir"
  "$venv_dir/bin/python" -m pytest -q \
    tests/test_stream.py \
    tests/test_cli.py \
    tests/test_cas.py
)

"$venv_dir/bin/python" -m pytest -q \
  "$project_dir/scripts/tests/test_ezviz_direct_media.py" \
  "$project_dir/scripts/tests/test_ezviz_network_lock.py" \
  "$project_dir/scripts/tests/test_ezviz_warm_controller.py" \
  "$project_dir/scripts/tests/test_macos_config_migration.py" \
  "$project_dir/scripts/tests/test_linux_config_tool.py" \
  "$project_dir/scripts/tests/test_linux_runtime_settings.py" \
  "$project_dir/scripts/tests/test_ezviz_discovery.py" \
  "$project_dir/scripts/tests/test_linux_setup_wizard.py" \
  "$project_dir/scripts/tests/test_linux_service_supervisor.py"

(
  cd "$project_dir"
  GOCACHE="$project_dir/.tmp/go-build-cache" "$go_bin" test \
    ./internal/homekit ./internal/streams
  # The wider upstream ffmpeg suite contains OS-specific golden strings. Gate
  # the exact H.264/Opus producer path used by this HomeKit bridge on every Mac.
  GOCACHE="$project_dir/.tmp/go-build-cache" "$go_bin" test \
    ./internal/ffmpeg -run '^TestHomeKitTranscodeArgs$'
  GOCACHE="$project_dir/.tmp/go-build-cache" "$go_bin" build -o "$go2rtc_binary" .
)

echo "CB2 局域网反向直连适配器和 HomeKit 桥接依赖均已安装并通过测试。"
