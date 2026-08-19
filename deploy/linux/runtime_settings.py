#!/usr/bin/env python3
"""Persistent, non-secret runtime settings for the Linux bridge."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SETTINGS_VERSION = 1
SETTINGS_FILE_NAME = "settings.json"
AUTH_STATE_FILE_NAME = "ezviz_auth.json"
SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{7,64}$")
HOST_PATTERN = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
TIMEZONE_PATTERN = re.compile(r"^[A-Za-z0-9_+./-]{1,64}$")
SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")

DEFAULT_SETTINGS: dict[str, Any] = {
    "version": SETTINGS_VERSION,
    "serial": "",
    "camera_ip": "",
    "listen_ip": "",
    "callback_port": 39000,
    "encoder": "software",
    "power_mode": "auto",
    "homekit_transcode": "on_demand",
    "warm_seconds": 600,
    "pir_preheat": "on",
    "pir_poll_seconds": 15,
    "power_refresh_seconds": 300,
    "region": "api.ys7.com",
    "homekit_name": "EZVIZ CB2",
    "timezone": "Asia/Shanghai",
}

ENVIRONMENT_FIELDS = {
    "EZVIZ_SERIAL": "serial",
    "EZVIZ_CAMERA_IP": "camera_ip",
    "EZVIZ_LISTEN_IP": "listen_ip",
    "EZVIZ_CALLBACK_PORT": "callback_port",
    "EZVIZ_ENCODER": "encoder",
    "EZVIZ_POWER_MODE": "power_mode",
    "EZVIZ_HOMEKIT_TRANSCODE": "homekit_transcode",
    "EZVIZ_WARM_SECONDS": "warm_seconds",
    "EZVIZ_PIR_PREHEAT": "pir_preheat",
    "EZVIZ_PIR_POLL_SECONDS": "pir_poll_seconds",
    "EZVIZ_POWER_REFRESH_SECONDS": "power_refresh_seconds",
    "EZVIZ_REGION": "region",
    "HOMEKIT_NAME": "homekit_name",
    "TZ": "timezone",
}


class SettingsError(ValueError):
    """A user-facing settings validation error."""


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise SettingsError(f"{label} 必须是整数")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise SettingsError(f"{label} 必须是整数") from error
    if not minimum <= result <= maximum:
        raise SettingsError(f"{label} 必须在 {minimum} 到 {maximum} 之间")
    return result


def _ipv4(value: Any, label: str, *, optional: bool = False) -> str:
    text = str(value or "").strip()
    if optional and not text:
        return ""
    try:
        address = ipaddress.ip_address(text)
    except ValueError as error:
        raise SettingsError(f"{label} 不是有效的 IPv4 地址") from error
    if not isinstance(address, ipaddress.IPv4Address):
        raise SettingsError(f"{label} 必须是 IPv4 地址")
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise SettingsError(f"{label} 必须是可访问的局域网单播地址")
    if not (
        address.is_private or address.is_link_local or address in SHARED_ADDRESS_SPACE
    ):
        raise SettingsError(f"{label} 必须是私有局域网地址")
    return str(address)


def usable_lan_ipv4(value: Any) -> str:
    try:
        return _ipv4(value, "IP")
    except SettingsError:
        return ""


def normalize_region(value: Any) -> str:
    region = str(value or "").strip().lower()
    official_region = region == "api.ys7.com" or region.endswith(".ezvizlife.com")
    if HOST_PATTERN.fullmatch(region) is None or ".." in region or not official_region:
        raise SettingsError("萤石 API 区域必须使用官方 ys7.com 或 ezvizlife.com 地址")
    return region


def normalize_settings(
    value: Mapping[str, Any],
    *,
    base: Mapping[str, Any] | None = None,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Return a complete, validated settings document."""

    unknown = set(value) - set(DEFAULT_SETTINGS)
    if unknown:
        raise SettingsError(f"包含不支持的配置项：{', '.join(sorted(unknown))}")

    merged = dict(DEFAULT_SETTINGS)
    if base:
        merged.update({key: base[key] for key in DEFAULT_SETTINGS if key in base})
    merged.update(value)
    merged["version"] = SETTINGS_VERSION

    serial = str(merged.get("serial") or "").strip().upper()
    if serial and SERIAL_PATTERN.fullmatch(serial) is None:
        raise SettingsError("摄像头序列号格式不正确")
    if require_complete and not serial:
        raise SettingsError("请填写完整摄像头序列号")
    merged["serial"] = serial

    camera_ip = str(merged.get("camera_ip") or "").strip()
    if camera_ip:
        merged["camera_ip"] = _ipv4(camera_ip, "摄像头 IP")
    elif require_complete:
        raise SettingsError("请填写或自动发现摄像头 IP")
    else:
        merged["camera_ip"] = ""
    merged["listen_ip"] = _ipv4(merged.get("listen_ip"), "Linux 主机 IP", optional=True)

    merged["callback_port"] = _integer(
        merged.get("callback_port"), "回连端口", 1, 65535
    )
    merged["warm_seconds"] = _integer(merged.get("warm_seconds"), "保温时长", 60, 86400)
    merged["pir_poll_seconds"] = _integer(
        merged.get("pir_poll_seconds"), "PIR 轮询周期", 5, 300
    )
    merged["power_refresh_seconds"] = _integer(
        merged.get("power_refresh_seconds"), "供电状态刷新周期", 30, 86400
    )

    choices = {
        "encoder": {"software", "auto", "vaapi", "cuda", "v4l2m2m", "rkmpp"},
        "power_mode": {"auto", "mains", "battery"},
        "homekit_transcode": {"on_demand", "continuous"},
        "pir_preheat": {"on", "off"},
    }
    labels = {
        "encoder": "编码器",
        "power_mode": "供电模式",
        "homekit_transcode": "HomeKit 转码策略",
        "pir_preheat": "PIR 提前预热",
    }
    for field, accepted in choices.items():
        selected = str(merged.get(field) or "").strip()
        if selected not in accepted:
            raise SettingsError(f"{labels[field]}的取值不受支持")
        merged[field] = selected

    merged["region"] = normalize_region(merged.get("region"))

    name = str(merged.get("homekit_name") or "").strip()
    if not 1 <= len(name) <= 64 or any(ord(char) < 32 for char in name):
        raise SettingsError("HomeKit 名称必须是 1 到 64 个可见字符")
    merged["homekit_name"] = name

    timezone = str(merged.get("timezone") or "").strip()
    if TIMEZONE_PATTERN.fullmatch(timezone) is None or ".." in timezone:
        raise SettingsError("时区格式不正确")
    merged["timezone"] = timezone
    return merged


def settings_complete(settings: Mapping[str, Any]) -> bool:
    try:
        normalize_settings(settings, require_complete=True)
    except SettingsError:
        return False
    return True


def token_is_ready(path: Path) -> bool:
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and bool(value.get("session_id"))


def token_matches_identity(path: Path, serial: str, region: str) -> bool:
    """Return whether a private token is bound to this camera and API region."""

    expected_serial = serial.strip().upper()
    try:
        expected_region = normalize_region(region)
    except SettingsError:
        return False
    if not expected_serial or not token_is_ready(path):
        return False
    auth_state = path.with_name(AUTH_STATE_FILE_NAME)
    try:
        if stat.S_IMODE(auth_state.stat().st_mode) & 0o077:
            return False
        value = json.loads(auth_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    if value.get("state"):
        return False
    bound_serial = str(value.get("serial") or "").strip().upper()
    try:
        bound_region = normalize_region(value.get("region"))
    except SettingsError:
        return False
    return bound_serial == expected_serial and bound_region == expected_region


def _serialize_settings(settings: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def _yaml_double_quoted_fragment(value: str) -> str:
    """Escape a value inserted inside a YAML double-quoted scalar."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


def secure_write(path: Path, data: bytes) -> None:
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
        temporary.unlink(missing_ok=True)
        raise


def persist_bound_token(path: Path, token: bytes, serial: str, region: str) -> None:
    """Commit a verified token and binding, failing closed on interruption."""

    normalized_serial = serial.strip().upper()
    if SERIAL_PATTERN.fullmatch(normalized_serial) is None:
        raise SettingsError("摄像头序列号格式不正确")
    normalized_region = normalize_region(region)
    try:
        value = json.loads(token)
    except (TypeError, json.JSONDecodeError) as error:
        raise SettingsError("萤石会话令牌格式不正确") from error
    if not isinstance(value, dict) or not value.get("session_id"):
        raise SettingsError("萤石会话令牌缺少有效 session")

    auth_state = path.with_name(AUTH_STATE_FILE_NAME)
    secure_write(
        auth_state,
        b'{"state":"updating","serial":"","region":""}\n',
    )
    secure_write(path, token)
    secure_write(
        auth_state,
        json.dumps(
            {"serial": normalized_serial, "region": normalized_region},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        + b"\n",
    )


class SettingsStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / SETTINGS_FILE_NAME
        self._lock = threading.RLock()

    def prepare(self) -> None:
        try:
            self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.data_dir.chmod(0o700)
        except OSError as error:
            raise OSError(self._ownership_error("无法创建或收紧目录权限")) from error
        if not os.access(self.data_dir, os.W_OK):
            raise OSError(self._ownership_error("目录不可写"))

    def _ownership_error(self, reason: str) -> str:
        try:
            metadata = self.data_dir.stat()
            owner = f"{metadata.st_uid}:{metadata.st_gid}"
            mode = f"{stat.S_IMODE(metadata.st_mode):03o}"
        except OSError:
            owner = "未知"
            mode = "未知"
        return (
            f"状态目录 {self.data_dir} {reason}；目录属主={owner} 权限={mode}，"
            f"当前进程={os.getuid()}:{os.getgid()}。标准命名卷请保持 "
            "PUID=1000、PGID=1000；自定义 UID/GID 前必须先为卷设置匹配属主"
        )

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return dict(DEFAULT_SETTINGS)
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise SettingsError("持久化配置文件损坏，无法解析") from error
            if not isinstance(raw, dict):
                raise SettingsError("持久化配置必须是 JSON 对象")
            if stat.S_IMODE(self.path.stat().st_mode) != 0o600:
                self.path.chmod(0o600)
            return normalize_settings(raw, require_complete=False)

    def save(self, value: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            normalized = normalize_settings(value, require_complete=True)
            secure_write(self.path, _serialize_settings(normalized))
            return normalized

    def bootstrap_from_environment(
        self, environment: Mapping[str, str] | None = None
    ) -> bool:
        """Migrate a legacy .env deployment once, without overriding Web settings."""

        with self._lock:
            if self.path.exists():
                return False
            source = os.environ if environment is None else environment
            values: dict[str, Any] = {}
            for variable, field in ENVIRONMENT_FIELDS.items():
                item = source.get(variable)
                if item is not None and str(item).strip():
                    values[field] = item
            if not values:
                return False
            try:
                normalized = normalize_settings(values, require_complete=False)
            except SettingsError as error:
                print(
                    f"[桥接服务] 忽略无效的旧版环境变量配置：{error}",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            secure_write(self.path, _serialize_settings(normalized))
            return True


def bridge_environment(
    settings: Mapping[str, Any],
    *,
    data_dir: Path,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    normalized = normalize_settings(settings, require_complete=True)
    environment = dict(os.environ if base is None else base)
    encoder = normalized["encoder"]
    hardware_fragment = "" if encoder == "software" else "#hardware"
    if encoder not in {"software", "auto"}:
        hardware_fragment = f"#hardware={encoder}"
    environment.update(
        {
            "EZVIZ_SERIAL": normalized["serial"],
            "EZVIZ_CAMERA_IP": normalized["camera_ip"],
            "EZVIZ_LISTEN_IP": normalized["listen_ip"],
            "EZVIZ_CALLBACK_PORT": str(normalized["callback_port"]),
            "EZVIZ_ENCODER": encoder,
            "EZVIZ_HARDWARE_FRAGMENT": hardware_fragment,
            "EZVIZ_POWER_MODE": normalized["power_mode"],
            "EZVIZ_HOMEKIT_TRANSCODE": normalized["homekit_transcode"],
            "EZVIZ_WARM_SECONDS": str(normalized["warm_seconds"]),
            "EZVIZ_LINGER": f"{normalized['warm_seconds']}s",
            "EZVIZ_PIR_PREHEAT": normalized["pir_preheat"],
            "EZVIZ_PIR_POLL_SECONDS": str(normalized["pir_poll_seconds"]),
            "EZVIZ_POWER_REFRESH_SECONDS": str(normalized["power_refresh_seconds"]),
            "EZVIZ_REGION": normalized["region"],
            "HOMEKIT_NAME": _yaml_double_quoted_fragment(normalized["homekit_name"]),
            "TZ": normalized["timezone"],
            "EZVIZ_DATA_DIR": str(data_dir),
            "EZVIZ_TOKEN_FILE": str(data_dir / "ezviz_token.json"),
            "EZVIZ_ACTIVITY_FILE": str(data_dir / "ezviz-stream-active.json"),
        }
    )
    return environment
