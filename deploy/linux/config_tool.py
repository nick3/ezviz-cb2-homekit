#!/usr/bin/env python3
"""Initialise and migrate sensitive go2rtc state without exposing it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path

PIN_MARKER = "__HOMEKIT_PIN__"
AUTH_STATE_FILE_NAME = "ezviz_auth.json"
CONFIG_VERSION = 3
CONFIG_VERSION_LINE = f"# ezviz-cb2-config-version: {CONFIG_VERSION}"
CURRENT_CONFIG_MARKERS = (
    CONFIG_VERSION_LINE,
    "linger:",
    "ezviz_raw:",
    "--activity-file=",
    "ffmpeg:ezviz_raw#",
)
INSECURE_PINS = {
    "00000000",
    "11111111",
    "22222222",
    "33333333",
    "44444444",
    "55555555",
    "66666666",
    "77777777",
    "88888888",
    "99999999",
    "12345678",
    "87654321",
}
TOP_LEVEL_SECTION = re.compile(r"(?m)^(?P<name>[A-Za-z0-9_-]+):(?:[ \t].*)?$")
PIN_LINE = re.compile(
    r"(?m)^[ \t]+pin:[ \t]*(?P<quote>[\"']?)"
    r"(?P<pin>[0-9]{3}-[0-9]{2}-[0-9]{3})(?P=quote)[ \t]*(?:#.*)?$"
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--template", type=Path, required=True)
    init.add_argument("--target", type=Path, required=True)

    upgrade = subparsers.add_parser("upgrade")
    upgrade.add_argument("--template", type=Path, required=True)
    upgrade.add_argument("--target", type=Path, required=True)

    show_pin = subparsers.add_parser("show-pin")
    show_pin.add_argument("--config", type=Path, required=True)

    migrate = subparsers.add_parser("import-state")
    migrate.add_argument("--template", type=Path, required=True)
    migrate.add_argument("--source-config", type=Path, required=True)
    migrate.add_argument("--source-token", type=Path, required=True)
    migrate.add_argument("--target-config", type=Path, required=True)
    migrate.add_argument("--target-token", type=Path, required=True)

    return parser.parse_args()


def _new_pin() -> str:
    while True:
        compact = f"{secrets.randbelow(100_000_000):08d}"
        if compact not in INSECURE_PINS:
            return f"{compact[:3]}-{compact[3:5]}-{compact[5:]}"


def _secure_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _secure_backup(target: Path, data: bytes) -> Path:
    backup = target.with_name(f"{target.name}.pre-v{CONFIG_VERSION}.bak")
    if backup.exists():
        backup.chmod(0o600)
        if backup.read_bytes() == data:
            return backup
        digest = hashlib.sha256(data).hexdigest()[:16]
        backup = target.with_name(f"{target.name}.pre-v{CONFIG_VERSION}.{digest}.bak")
        if backup.exists():
            backup.chmod(0o600)
            if backup.read_bytes() != data:
                raise RuntimeError(
                    "Config backup hash collision; refusing to overwrite it"
                )
            return backup
    _secure_write(backup, data)
    return backup


def _render_new_config(template: Path) -> str:
    text = template.read_text(encoding="utf-8")
    if text.count(PIN_MARKER) != 1:
        raise RuntimeError("Config template must contain exactly one PIN marker")
    return text.replace(PIN_MARKER, _new_pin())


def _section(text: str, name: str) -> str:
    matches = list(TOP_LEVEL_SECTION.finditer(text))
    for index, match in enumerate(matches):
        if match.group("name") != name:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        return text[match.start() : end].rstrip() + "\n\n"
    raise RuntimeError(f"Config does not contain a top-level {name!r} section")


def _replace_section(text: str, name: str, replacement: str) -> str:
    current = _section(text, name)
    return text.replace(current, replacement, 1)


def _validate_token(path: Path) -> bytes:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RuntimeError(
            f"Source token permissions must be 0600, currently {mode:03o}"
        )
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Source token must contain a JSON object")
    return raw


def _homekit_pin(homekit: str) -> str:
    match = PIN_LINE.search(homekit)
    if match is None:
        raise RuntimeError("HomeKit PIN was not found in the persistent config")
    pin = match.group("pin")
    if pin.replace("-", "") in INSECURE_PINS:
        raise RuntimeError("HomeKit PIN is insecure")
    return pin


def read_homekit_pin(config: Path) -> str:
    """Read the PIN only from the top-level HomeKit section."""

    text = config.read_text(encoding="utf-8")
    return _homekit_pin(_section(text, "homekit"))


def init_config(template: Path, target: Path) -> None:
    if target.exists():
        raise RuntimeError(f"Refusing to overwrite existing config: {target}")
    _secure_write(target, _render_new_config(template).encode())


def upgrade_config(template: Path, target: Path) -> bool:
    """Upgrade a managed config while preserving its HomeKit identity."""
    source_config = target.read_text(encoding="utf-8")
    if all(marker in source_config for marker in CURRENT_CONFIG_MARKERS):
        return False

    homekit = _section(source_config, "homekit")
    _homekit_pin(homekit)

    migrated = _render_new_config(template)
    migrated = _replace_section(migrated, "homekit", homekit)
    _secure_backup(target, source_config.encode())
    _secure_write(target, migrated.encode())
    return True


def _show_pin(config: Path) -> None:
    print(read_homekit_pin(config))


def _import_state(args: argparse.Namespace) -> None:
    target_auth_state = args.target_token.with_name(AUTH_STATE_FILE_NAME)
    if (
        args.target_config.exists()
        or args.target_token.exists()
        or target_auth_state.exists()
    ):
        raise RuntimeError(
            "Refusing to overwrite existing Linux state; "
            "import into an empty data directory"
        )

    source_config = args.source_config.read_text(encoding="utf-8")
    homekit = _section(source_config, "homekit")
    _homekit_pin(homekit)
    token = _validate_token(args.source_token)

    target_config = _render_new_config(args.template)
    target_config = _replace_section(target_config, "homekit", homekit)
    _secure_write(args.target_config, target_config.encode())
    try:
        _secure_write(args.target_token, token)
        _secure_write(
            target_auth_state,
            json.dumps(
                {"state": "unbound_import", "serial": ""},
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            + b"\n",
        )
    except BaseException:
        args.target_config.unlink(missing_ok=True)
        args.target_token.unlink(missing_ok=True)
        target_auth_state.unlink(missing_ok=True)
        raise


def main() -> int:
    args = _arguments()
    try:
        if args.command == "init":
            init_config(args.template, args.target)
        elif args.command == "upgrade":
            if upgrade_config(args.template, args.target):
                print(f"配置已升级到版本 {CONFIG_VERSION}；旧配置已安全备份。")
        elif args.command == "show-pin":
            _show_pin(args.config)
        elif args.command == "import-state":
            _import_state(args)
        else:
            raise RuntimeError(f"Unsupported command: {args.command}")
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"状态操作失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
