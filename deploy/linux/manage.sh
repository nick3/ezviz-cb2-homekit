#!/bin/sh

set -eu
umask 077

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "${script_dir}"

usage() {
  cat <<'EOF'
用法：./manage.sh <命令>

  init                         创建 .env 和私有状态目录
  bundle [输出文件]            生成可复制到 Linux 的无凭据源码包
  build                        构建本机镜像
  login                        交互登录萤石并保存会话令牌
  up                           构建并后台启动桥接
  verify                       主动唤醒摄像头并验证 H.264/Opus
  pin                          显示 HomeKit 配对码
  status                       查看容器状态
  logs                         持续查看桥接日志
  restart                      重启桥接
  down                         停止桥接但保留状态
  import-state <配置> <令牌>   在首次启动前导入 Mac 配对和会话状态
EOF
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "没有找到 Docker，请先安装 Docker Engine 与 Compose 插件。" >&2
    exit 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    echo "没有找到 docker compose 插件。" >&2
    exit 1
  fi
}

compose() {
  require_docker
  PUID="${PUID:-$(id -u)}" PGID="${PGID:-$(id -g)}" \
    docker compose --project-directory "${script_dir}" -f compose.yaml "$@"
}

ensure_layout() {
  if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "已创建 deploy/linux/.env，请先填写摄像头和虚拟机地址。"
  fi
  mkdir -p data
  chmod 700 data
}

absolute_file() {
  input=$1
  if [ ! -f "${input}" ]; then
    echo "文件不存在：${input}" >&2
    exit 1
  fi
  directory=$(CDPATH= cd -- "$(dirname -- "${input}")" && pwd)
  printf '%s/%s\n' "${directory}" "$(basename -- "${input}")"
}

command=${1:-help}
case "${command}" in
  init)
    ensure_layout
    ;;
  bundle)
    if [ "$#" -gt 2 ]; then
      usage >&2
      exit 1
    fi
    if [ "$#" -eq 2 ]; then
      "${script_dir}/make-bundle.sh" "$2"
    else
      "${script_dir}/make-bundle.sh"
    fi
    ;;
  build)
    ensure_layout
    compose build
    ;;
  login)
    ensure_layout
    compose run --rm --no-deps --build bridge login
    ;;
  up)
    ensure_layout
    if [ "$(uname -s)" != "Linux" ]; then
      echo "正式启动需要 Linux 主机；macOS Docker Desktop 只能用于构建检查。" >&2
      exit 1
    fi
    compose up -d --build
    ;;
  verify)
    ensure_layout
    compose run --rm --no-deps bridge verify
    ;;
  pin)
    ensure_layout
    compose run --rm --no-deps --build bridge show-pin
    ;;
  status)
    ensure_layout
    compose ps
    ;;
  logs)
    ensure_layout
    compose logs -f bridge
    ;;
  restart)
    ensure_layout
    compose restart bridge
    ;;
  down)
    ensure_layout
    compose down
    ;;
  import-state)
    if [ "$#" -ne 3 ]; then
      usage >&2
      exit 1
    fi
    ensure_layout
    source_config=$(absolute_file "$2")
    source_token=$(absolute_file "$3")
    compose run --rm --no-deps --build \
      -v "${source_config}:/import/go2rtc.yaml:ro" \
      -v "${source_token}:/import/ezviz_token.json:ro" \
      bridge import-state
    echo "已导入 HomeKit 配对身份和萤石会话；请继续填写 .env 后启动。"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
