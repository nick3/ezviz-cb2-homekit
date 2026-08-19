#!/bin/sh

set -eu
umask 077

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "${script_dir}/../.." && pwd)
output=${1:-"${project_dir}/.tmp/ezviz-homekit-linux-src.tar.gz"}

case "${output}" in
  /*) ;;
  *) output="$(pwd)/${output}" ;;
esac

mkdir -p "$(dirname -- "${output}")"

cd "${project_dir}"
tar -czf "${output}" \
  --exclude='**/__pycache__' \
  --exclude='deploy/linux/.env' \
  --exclude='deploy/linux/data' \
  .dockerignore \
  .gitignore \
  LICENSE \
  go.mod \
  go.sum \
  main.go \
  internal \
  pkg \
  www \
  scripts/probe-ezviz-direct-reverse.py \
  scripts/ezviz_warm_controller.py \
  scripts/ezviz_direct_media.py \
  scripts/ezviz_network_lock.py \
  scripts/login-ezviz-cloud.py \
  scripts/patches \
  scripts/tests \
  deploy/linux

chmod 600 "${output}"
if command -v sha256sum >/dev/null 2>&1; then
  digest=$(sha256sum "${output}" | awk '{print $1}')
else
  digest=$(shasum -a 256 "${output}" | awk '{print $1}')
fi
printf '%s  %s\n' "${digest}" "$(basename -- "${output}")" >"${output}.sha256"
chmod 600 "${output}.sha256"
echo "已生成不含令牌、账号、配对数据和设备配置的 Linux 源码包："
echo "${output}"
echo "校验文件：${output}.sha256"
