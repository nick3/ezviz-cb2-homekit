#!/usr/bin/env python3

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from pyezvizapi import EzvizClient
from pyezvizapi.exceptions import EzvizAuthVerificationCode, PyEzvizError


REGION = "api.ys7.com"
PROJECT_DIR = Path(__file__).resolve().parent.parent
TOKEN_FILE = PROJECT_DIR / ".tmp" / "ezviz_token.json"


def arguments() -> argparse.Namespace:
    serial = os.environ.get("EZVIZ_SERIAL")
    parser = argparse.ArgumentParser(
        description="Log in to the official EZVIZ API and save a session token",
    )
    parser.add_argument(
        "--serial",
        default=serial,
        required=serial is None,
        help="Camera serial number to verify after login",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(os.environ.get("EZVIZ_TOKEN_FILE", TOKEN_FILE)),
        help="Destination for the permission-0600 session token",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("EZVIZ_REGION", REGION),
        help="Official EZVIZ API region host",
    )
    return parser.parse_args()


def prompt_nonempty(label: str, *, secret: bool = False) -> str:
    while True:
        value = getpass.getpass(label) if secret else input(label)
        value = value.strip()
        if value:
            return value
        print("输入不能为空。", file=sys.stderr)


def save_token(token: dict[str, object], token_file: Path) -> None:
    token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = token_file.with_suffix(".tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(token, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, token_file)
    token_file.chmod(0o600)


def main() -> int:
    args = arguments()
    print(f"仅登录萤石官方接口 {args.region}。")
    print("账号和密码不会保存；本机只保存权限为 600 的会话令牌。")
    account = prompt_nonempty("萤石账号（手机号或登录名）：")
    password = prompt_nonempty("萤石账号密码（输入不会显示）：", secret=True)

    client = EzvizClient(account, password, args.region)
    try:
        try:
            client.login()
        except EzvizAuthVerificationCode:
            code = prompt_nonempty("短信验证码（输入不会显示）：", secret=True)
            if not code.isdigit():
                print("短信验证码格式不正确。", file=sys.stderr)
                return 1
            client.login(sms_code=int(code))

        device = client.get_device_infos(args.serial)
        if not device:
            print(
                f"账号中未找到目标摄像头（{args.serial}），未保存会话。",
                file=sys.stderr,
            )
            return 1

        save_token(client.export_token(), args.token_file)
        print(f"登录成功，已确认账号中的目标摄像头（{args.serial}）。")
        return 0
    except PyEzvizError as error:
        print(f"登录或设备检查失败：{error}", file=sys.stderr)
        return 1
    finally:
        client.close_session()


if __name__ == "__main__":
    raise SystemExit(main())
