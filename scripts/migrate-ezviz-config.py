#!/usr/bin/env python3
"""Upgrade the private macOS CB2 config without losing HomeKit state."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import sys


CONFIG_VERSION = 3
CONFIG_VERSION_LINE = f"# ezviz-cb2-config-version: {CONFIG_VERSION}"
VERSION_LINE = re.compile(r"(?m)^# ezviz-cb2-config-version:[ \t]*\d+[ \t]*$")
TOP_LEVEL_SECTION = re.compile(
    r"(?m)^(?P<name>[A-Za-z0-9_-]+):(?:[ \t].*)?$"
)
NESTED_SECTION = re.compile(
    r"(?m)^  (?P<name>[A-Za-z0-9_-]+):(?:[ \t].*)?$"
)
LIST_ENTRY = re.compile(r"(?m)^[ \t]{4}-[ \t]+.+$")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate a private EZVIZ go2rtc config in place",
    )
    parser.add_argument("--config", type=Path, required=True)
    return parser.parse_args()


def _section_bounds(text: str, name: str) -> tuple[int, int]:
    matches = list(TOP_LEVEL_SECTION.finditer(text))
    for index, match in enumerate(matches):
        if match.group("name") != name:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        return match.start(), end
    raise RuntimeError(f"配置缺少顶层 {name!r} 段")


def _nested_bounds(section: str, name: str) -> tuple[int, int]:
    matches = list(NESTED_SECTION.finditer(section))
    for index, match in enumerate(matches):
        if match.group("name") != name:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        return match.start(), end
    raise RuntimeError(f"streams 段缺少 {name!r} 流")


def _secure_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
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


def _secure_backup(config: Path, source: str) -> Path:
    payload = source.encode("utf-8")
    backup = config.with_name(f"{config.name}.pre-v{CONFIG_VERSION}.bak")
    if backup.exists():
        backup.chmod(0o600)
        if backup.read_bytes() == payload:
            return backup
        digest = hashlib.sha256(payload).hexdigest()[:16]
        backup = config.with_name(
            f"{config.name}.pre-v{CONFIG_VERSION}.{digest}.bak"
        )
        if backup.exists():
            backup.chmod(0o600)
            if backup.read_bytes() != payload:
                raise RuntimeError("配置备份哈希冲突，拒绝覆盖现有备份")
            return backup
    _secure_write(backup, source)
    return backup


def _is_current(text: str) -> bool:
    try:
        linger_start, linger_end = _section_bounds(text, "linger")
        stream_start, stream_end = _section_bounds(text, "streams")
    except RuntimeError:
        return False
    linger = text[linger_start:linger_end]
    streams = text[stream_start:stream_end]
    return (
        CONFIG_VERSION_LINE in text
        and re.search(r"(?m)^  ezviz_raw:[ \t]*", linger) is not None
        and re.search(r"(?m)^  ezviz_raw:[ \t]*$", streams) is not None
        and re.search(r"(?m)^  ezviz:[ \t]*$", streams) is not None
        and "--activity-file=" in streams
        and "ffmpeg:ezviz_raw#video=h264" in streams
        and "ffmpeg:ezviz_raw#audio=opus" in streams
        and "ffmpeg:ezviz#" not in streams
    )


def _with_activity_file(entry: str) -> str:
    if "--activity-file=" in entry:
        return entry
    fragment = "#killsignal="
    if fragment not in entry:
        raise RuntimeError(
            "局域网源缺少 go2rtc exec 终止参数，无法确定安全插入位置"
        )
    return entry.replace(
        fragment,
        " --activity-file=${EZVIZ_ACTIVITY_FILE}" + fragment,
        1,
    )


def _migrate_streams(text: str) -> str:
    section_start, section_end = _section_bounds(text, "streams")
    section = text[section_start:section_end]
    legacy_start, legacy_end = _nested_bounds(section, "ezviz")
    legacy = section[legacy_start:legacy_end]
    entries = LIST_ENTRY.findall(legacy)

    raw = [entry for entry in entries if "probe-ezviz-direct-reverse.py" in entry]
    transcodes = [
        entry
        for entry in entries
        if "ffmpeg:ezviz#" in entry or "ffmpeg:ezviz_raw#" in entry
    ]
    recognized = raw + transcodes
    if len(raw) != 1:
        raise RuntimeError("ezviz 流必须恰好包含一个局域网反向直连源")
    if not any("#video=h264" in entry for entry in transcodes):
        raise RuntimeError("ezviz 流缺少 HomeKit H.264 转码源")
    if not any("#audio=opus" in entry for entry in transcodes):
        raise RuntimeError("ezviz 流缺少 HomeKit Opus 转码源")
    if len(recognized) != len(entries):
        raise RuntimeError("ezviz 流包含未知源，拒绝自动覆盖")

    raw_entry = _with_activity_file(raw[0])
    transcode_entries = [
        entry.replace("ffmpeg:ezviz#", "ffmpeg:ezviz_raw#", 1)
        for entry in transcodes
    ]
    replacement = "\n".join(
        ["  ezviz_raw:", raw_entry, "  ezviz:", *transcode_entries]
    )
    migrated_section = (
        section[:legacy_start]
        + replacement
        + "\n"
        + section[legacy_end:].lstrip("\n")
    )
    return text[:section_start] + migrated_section + text[section_end:]


def _migrate_linger(text: str) -> str:
    try:
        start, end = _section_bounds(text, "linger")
    except RuntimeError:
        streams_start, _ = _section_bounds(text, "streams")
        section = 'linger:\n  ezviz_raw: "${EZVIZ_LINGER:600s}"\n\n'
        return text[:streams_start] + section + text[streams_start:]

    section = text[start:end]
    raw_line = re.compile(r"(?m)^  ezviz_raw:[^\n]*$")
    legacy_line = re.compile(r"(?m)^  ezviz:[^\n]*$")
    if raw_line.search(section):
        migrated = section
    elif legacy_line.search(section):
        migrated = legacy_line.sub(
            '  ezviz_raw: "${EZVIZ_LINGER:600s}"',
            section,
            count=1,
        )
    else:
        migrated = section.rstrip() + '\n  ezviz_raw: "${EZVIZ_LINGER:600s}"\n'
    return text[:start] + migrated + text[end:]


def _set_version(text: str) -> str:
    if VERSION_LINE.search(text):
        return VERSION_LINE.sub(CONFIG_VERSION_LINE, text, count=1)
    first_newline = text.find("\n")
    if first_newline == -1:
        return CONFIG_VERSION_LINE + "\n" + text
    return (
        text[: first_newline + 1]
        + CONFIG_VERSION_LINE
        + "\n"
        + text[first_newline + 1 :]
    )


def upgrade(config: Path) -> bool:
    source = config.read_text(encoding="utf-8")
    if _is_current(source):
        return False

    migrated = _set_version(_migrate_linger(_migrate_streams(source)))
    if not _is_current(migrated):
        raise RuntimeError("迁移后的配置未通过结构校验")

    _secure_backup(config, source)
    _secure_write(config, migrated)
    return True


def main() -> int:
    args = _arguments()
    try:
        if upgrade(args.config):
            print(
                f"本地配置已升级到版本 {CONFIG_VERSION}；旧配置已安全备份。",
                file=sys.stderr,
            )
    except (OSError, RuntimeError, UnicodeError) as error:
        print(f"本地配置升级失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
