#!/bin/bash

set -u
umask 077

script_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$script_dir" || exit 1

pause_on_error() {
  echo
  read -r -p "未能继续。按回车键关闭此窗口……" _
}

clear
echo "萤石 CB2 接入 HomeKit"
echo "================================="
echo

if [[ ! -s "$script_dir/.tmp/ezviz_token.json" ]]; then
  echo "先登录萤石云。密码与短信验证码输入时不会显示。"
  echo
  if ! "$script_dir/scripts/login-ezviz-cloud.sh"; then
    pause_on_error
    exit 1
  fi
fi

echo
echo "正在启动视频桥接……"
echo
exec "$script_dir/scripts/start-ezviz-homekit.sh"
