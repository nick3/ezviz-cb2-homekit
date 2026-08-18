#!/bin/sh

set -eu
umask 077

data_dir="${EZVIZ_DATA_DIR:-/data}"
config_file="${GO2RTC_CONFIG_FILE:-${data_dir}/go2rtc.yaml}"
token_file="${EZVIZ_TOKEN_FILE:-${data_dir}/ezviz_token.json}"
template="/app/deploy/linux/go2rtc.yaml.tmpl"
config_tool="/app/deploy/linux/config_tool.py"

prepare_data_dir() {
  mkdir -p "${data_dir}"
  chmod 700 "${data_dir}"
  if [ ! -w "${data_dir}" ]; then
    echo "状态目录不可写：${data_dir}" >&2
    exit 1
  fi
}

initialize_config() {
  prepare_data_dir
  if [ ! -e "${config_file}" ]; then
    python3 "${config_tool}" init --template "${template}" --target "${config_file}"
    echo "已生成新的 HomeKit 身份；需要配对时运行 ./manage.sh pin 查看配对码。"
  fi
  chmod 600 "${config_file}"
}

require_value() {
  variable_name="$1"
  eval "variable_value=\${${variable_name}:-}"
  if [ -z "${variable_value}" ]; then
    echo "缺少 ${variable_name}，请编辑 deploy/linux/.env。" >&2
    exit 1
  fi
}

prepare_runtime_environment() {
  require_value EZVIZ_SERIAL
  require_value EZVIZ_CAMERA_IP

  callback_port="${EZVIZ_CALLBACK_PORT:-39000}"
  case "${callback_port}" in
    *[!0-9]*|'')
      echo "EZVIZ_CALLBACK_PORT 必须是 1 到 65535 的整数。" >&2
      exit 1
      ;;
  esac
  if [ "${callback_port}" -lt 1 ] || [ "${callback_port}" -gt 65535 ]; then
    echo "EZVIZ_CALLBACK_PORT 必须是 1 到 65535 的整数。" >&2
    exit 1
  fi
  export EZVIZ_CALLBACK_PORT="${callback_port}"

  encoder="${EZVIZ_ENCODER:-software}"
  case "${encoder}" in
    software)
      EZVIZ_HARDWARE_FRAGMENT=""
      ;;
    auto)
      EZVIZ_HARDWARE_FRAGMENT="#hardware"
      ;;
    vaapi|cuda|v4l2m2m|rkmpp)
      EZVIZ_HARDWARE_FRAGMENT="#hardware=${encoder}"
      ;;
    *)
      echo "不支持的 EZVIZ_ENCODER：${encoder}" >&2
      exit 1
      ;;
  esac
  export EZVIZ_HARDWARE_FRAGMENT
  export EZVIZ_LISTEN_IP="${EZVIZ_LISTEN_IP:-}"
}

require_token() {
  if [ ! -s "${token_file}" ]; then
    echo "尚未登录萤石，请先运行 ./manage.sh login。" >&2
    exit 1
  fi
  chmod 600 "${token_file}"
}

command="${1:-start}"
case "${command}" in
  start)
    initialize_config
    prepare_runtime_environment
    require_token
    exec /usr/local/bin/go2rtc -c "${config_file}"
    ;;
  login)
    initialize_config
    require_value EZVIZ_SERIAL
    exec python3 /app/scripts/login-ezviz-cloud.py \
      --serial "${EZVIZ_SERIAL}" \
      --region "${EZVIZ_REGION:-api.ys7.com}" \
      --token-file "${token_file}"
    ;;
  verify)
    exec python3 /app/deploy/linux/verify.py
    ;;
  show-pin)
    initialize_config
    exec python3 "${config_tool}" show-pin --config "${config_file}"
    ;;
  import-state)
    prepare_data_dir
    exec python3 "${config_tool}" import-state \
      --template "${template}" \
      --source-config /import/go2rtc.yaml \
      --source-token /import/ezviz_token.json \
      --target-config "${config_file}" \
      --target-token "${token_file}"
    ;;
  *)
    exec "$@"
    ;;
esac
