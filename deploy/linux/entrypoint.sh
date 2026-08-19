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
  python3 "${config_tool}" upgrade --template "${template}" --target "${config_file}"
  chmod 600 "${config_file}"
}

require_value() {
  variable_name="$1"
  eval "variable_value=\${${variable_name}:-}"
  if [ -z "${variable_value}" ]; then
    echo "缺少 ${variable_name}；请使用 Web 向导，或通过环境变量提供。" >&2
    exit 1
  fi
}

command="${1:-start}"
case "${command}" in
  start)
    exec python3 /app/deploy/linux/service_supervisor.py
    ;;
  migrate-legacy)
    exec python3 "${config_tool}" migrate-bind-state \
      --source-dir "${EZVIZ_LEGACY_DATA_DIR:-/legacy-data}" \
      --target-dir "${data_dir}" \
      --serial "${EZVIZ_SERIAL:-}" \
      --region "${EZVIZ_REGION:-api.ys7.com}" \
      --uid "${EZVIZ_RUNTIME_UID:-1000}" \
      --gid "${EZVIZ_RUNTIME_GID:-1000}"
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
