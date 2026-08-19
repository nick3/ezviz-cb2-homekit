#!/bin/sh

set -eu
umask 077

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "${script_dir}"

usage() {
  cat <<'EOF'
用法：./manage.sh <命令>

  init                         拉取镜像并启动 Web 配置向导
  bundle [输出文件]            生成可复制到 Linux 的无凭据源码包
  pull                         拉取最新的预构建多架构镜像
  login                        显示 Web 登录向导说明
  up                           拉取镜像并后台启动向导/桥接
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
  docker compose --project-directory "${script_dir}" -f compose.yaml "$@"
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
    if [ "$(uname -s)" != "Linux" ]; then
      echo "正式启动需要 Linux 主机。" >&2
      exit 1
    fi
    compose up -d
    echo "Web 配置向导已启动，请在浏览器访问 http://<Linux-IP>:${EZVIZ_SETUP_PORT:-8099}。"
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
  pull)
    compose pull bridge
    ;;
  login)
    echo "请在浏览器访问 http://<Linux-IP>:${EZVIZ_SETUP_PORT:-8099}，通过向导登录萤石。"
    ;;
  up)
    if [ "$(uname -s)" != "Linux" ]; then
      echo "正式启动需要 Linux 主机；macOS Docker Desktop 只能用于构建检查。" >&2
      exit 1
    fi
    compose up -d
    echo "Web 配置向导：http://<Linux-IP>:${EZVIZ_SETUP_PORT:-8099}"
    ;;
  verify)
    compose exec bridge python3 /app/deploy/linux/verify.py
    ;;
  pin)
    compose exec bridge python3 /app/deploy/linux/config_tool.py \
      show-pin --config /data/go2rtc.yaml
    ;;
  status)
    compose ps
    ;;
  logs)
    compose logs -f bridge
    ;;
  restart)
    compose restart bridge
    ;;
  down)
    compose down
    ;;
  import-state)
    if [ "$#" -ne 3 ]; then
      usage >&2
      exit 1
    fi
    source_config=$(absolute_file "$2")
    source_token=$(absolute_file "$3")
    compose run --rm --no-deps \
      -v "${source_config}:/import/go2rtc.yaml:ro" \
      -v "${source_token}:/import/ezviz_token.json:ro" \
      bridge import-state
    echo "已导入 HomeKit 配对身份和萤石会话；请执行 docker compose up -d 后打开 Web 向导。"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
