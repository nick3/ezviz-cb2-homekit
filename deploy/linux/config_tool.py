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
LEGACY_MIGRATION_MARKER = ".legacy-bind-migration.json"
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
SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{7,64}$")


def _numeric_id(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a numeric ID") from error
    if not 0 <= result <= 2_147_483_647:
        raise argparse.ArgumentTypeError("must be between 0 and 2147483647")
    return result


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

    legacy = subparsers.add_parser("migrate-bind-state")
    legacy.add_argument("--source-dir", type=Path, required=True)
    legacy.add_argument("--target-dir", type=Path, required=True)
    legacy.add_argument("--serial", default="")
    legacy.add_argument("--uid", type=_numeric_id, required=True)
    legacy.add_argument("--gid", type=_numeric_id, required=True)

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


def _regular_file(path: Path, label: str) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label} must be a regular file, not a symlink")


def _private_json(path: Path, label: str) -> tuple[bytes, dict[str, object]]:
    _regular_file(path, label)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise RuntimeError(f"{label} permissions must be 0600, currently {mode:03o}")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain a JSON object")
    return raw, value


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


def _legacy_marker_payload(
    source_config: bytes,
    source_token: bytes | None,
    serial: str,
) -> bytes:
    value = {
        "version": 1,
        "config_sha256": hashlib.sha256(source_config).hexdigest(),
        "token_sha256": (
            hashlib.sha256(source_token).hexdigest() if source_token is not None else ""
        ),
        "serial": serial,
    }
    return json.dumps(value, sort_keys=True).encode("utf-8") + b"\n"


def _set_migrated_state_owner(
    target_dir: Path,
    paths: tuple[Path, ...],
    owner: tuple[int, int] | None,
) -> None:
    if owner is None:
        return
    uid, gid = owner
    for path in paths:
        try:
            os.chown(path, uid, gid, follow_symlinks=False)
        except FileNotFoundError:
            pass
    os.chown(target_dir, uid, gid, follow_symlinks=False)


def migrate_legacy_bind_state(
    source_dir: Path,
    target_dir: Path,
    serial: str,
    *,
    owner: tuple[int, int] | None = None,
) -> bool:
    """Move the old ./data bind state into an empty named volume once.

    The legacy login command verified the configured serial before saving its token,
    so a token without the newer auth sidecar may be bound only to that validated
    legacy environment value. The HomeKit config is the final commit marker: token
    and auth state are written first, preventing a crash from exposing a new identity.
    """

    target_config = target_dir / "go2rtc.yaml"
    target_token = target_dir / "ezviz_token.json"
    target_auth = target_dir / AUTH_STATE_FILE_NAME
    marker = target_dir / LEGACY_MIGRATION_MARKER
    if target_config.exists():
        if marker.exists():
            _regular_file(marker, "Legacy migration marker")
            _set_migrated_state_owner(
                target_dir,
                (target_config, target_token, target_auth),
                owner,
            )
            marker.unlink()
        return False

    source_config_path = source_dir / "go2rtc.yaml"
    try:
        _regular_file(source_config_path, "Legacy HomeKit config")
    except FileNotFoundError:
        return False

    source_config = source_config_path.read_bytes()
    source_text = source_config.decode("utf-8")
    _homekit_pin(_section(source_text, "homekit"))

    source_token_path = source_dir / "ezviz_token.json"
    source_auth_path = source_dir / AUTH_STATE_FILE_NAME
    source_token: bytes | None = None
    source_auth: bytes | None = None
    token_value: dict[str, object] | None = None
    try:
        source_token, token_value = _private_json(
            source_token_path, "Legacy EZVIZ token"
        )
    except FileNotFoundError:
        if source_auth_path.exists():
            raise RuntimeError("Legacy auth binding exists without its EZVIZ token")

    normalized_serial = serial.strip().upper()
    serial_is_valid = bool(normalized_serial) and bool(
        SERIAL_PATTERN.fullmatch(normalized_serial)
    )
    if token_value is not None and not token_value.get("session_id"):
        raise RuntimeError("Legacy EZVIZ token does not contain a usable session")

    try:
        source_auth, auth_value = _private_json(
            source_auth_path, "Legacy EZVIZ auth binding"
        )
    except FileNotFoundError:
        auth_value = None
    if auth_value is not None:
        bound_serial = str(auth_value.get("serial") or "").strip().upper()
        if bound_serial and SERIAL_PATTERN.fullmatch(bound_serial) is None:
            raise RuntimeError("Legacy EZVIZ auth binding has an invalid serial")
        if bound_serial and serial_is_valid and bound_serial != normalized_serial:
            raise RuntimeError("Legacy EZVIZ auth binding does not match EZVIZ_SERIAL")

    marker_payload = _legacy_marker_payload(
        source_config,
        source_token,
        normalized_serial if serial_is_valid else "",
    )
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target_dir.chmod(0o700)
    entries = {path.name for path in target_dir.iterdir()}
    resumable_entries = {
        LEGACY_MIGRATION_MARKER,
        target_token.name,
        target_auth.name,
    }
    temporary_entries = {
        f".{LEGACY_MIGRATION_MARKER}.tmp",
        f".{target_config.name}.tmp",
        f".{target_token.name}.tmp",
        f".{target_auth.name}.tmp",
    }
    if marker.exists():
        existing_marker, _ = _private_json(marker, "Legacy migration marker")
        if entries - resumable_entries - temporary_entries:
            raise RuntimeError(
                "Named volume contains unrelated state; refusing legacy migration"
            )
        for name in temporary_entries:
            (target_dir / name).unlink(missing_ok=True)
        if existing_marker != marker_payload:
            target_token.unlink(missing_ok=True)
            target_auth.unlink(missing_ok=True)
            _secure_write(marker, marker_payload)
    elif entries - temporary_entries:
        raise RuntimeError(
            "Named volume is not empty; refusing to overwrite it with legacy state"
        )
    else:
        for name in temporary_entries:
            (target_dir / name).unlink(missing_ok=True)
        _secure_write(marker, marker_payload)

    try:
        if source_token is not None:
            _secure_write(target_token, source_token)
            if source_auth is not None:
                _secure_write(target_auth, source_auth)
            else:
                auth_state = {
                    "state": (
                        "legacy_upgrade" if serial_is_valid else "unbound_import"
                    ),
                    "serial": normalized_serial if serial_is_valid else "",
                }
                _secure_write(
                    target_auth,
                    json.dumps(
                        auth_state,
                        ensure_ascii=False,
                        indent=2,
                    ).encode("utf-8")
                    + b"\n",
                )
        _secure_write(target_config, source_config)
        _set_migrated_state_owner(
            target_dir,
            (target_config, target_token, target_auth),
            owner,
        )
        marker.unlink()
    except BaseException:
        target_config.unlink(missing_ok=True)
        target_token.unlink(missing_ok=True)
        target_auth.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        raise
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
        elif args.command == "migrate-bind-state":
            if migrate_legacy_bind_state(
                args.source_dir,
                args.target_dir,
                args.serial,
                owner=(args.uid, args.gid),
            ):
                print("已从旧版绑定目录迁移 HomeKit 身份和萤石会话。")
        else:
            raise RuntimeError(f"Unsupported command: {args.command}")
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"状态操作失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
