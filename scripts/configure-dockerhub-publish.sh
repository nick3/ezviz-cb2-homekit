#!/bin/bash

set -euo pipefail
umask 077

repository="${GITHUB_REPOSITORY:-nick3/ezviz-cb2-homekit}"

if ! command -v gh >/dev/null 2>&1; then
  echo "没有找到 gh 命令。" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "请先运行 gh auth login。" >&2
  exit 1
fi

read -r -p "Docker Hub 用户名：" dockerhub_username
if [[ -z "$dockerhub_username" ]]; then
  echo "用户名不能为空。" >&2
  exit 1
fi

read -r -s -p "Docker Hub Read & Write 访问令牌：" dockerhub_token
echo
if [[ -z "$dockerhub_token" ]]; then
  echo "访问令牌不能为空。" >&2
  exit 1
fi

printf '%s' "$dockerhub_username" \
  | gh variable set DOCKERHUB_USERNAME --repo "$repository"
printf '%s' "$dockerhub_token" \
  | gh secret set DOCKERHUB_TOKEN --repo "$repository"

unset dockerhub_token

echo "Docker Hub 发布凭据已安全写入 $repository。"
echo "正在重新触发镜像发布……"
gh workflow run docker-publish.yml --repo "$repository" --ref main
