#!/usr/bin/env python3
"""Supervise go2rtc and keep an EZVIZ battery stream warm when appropriate."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable


PIR_ALERT_CODES = frozenset(
    {
        2402,  # Unified-message motion filter
        2403,  # Unified-message person filter
        10000,  # PIR / body-feel
        10002,  # Motion detection
        10010,  # Person alarm
        10033,  # Intrusion
        10035,  # Baby motion
        10079,  # Intelligent detection
        15010,  # CB2 unified-message motion/PIR alarm
    }
)
POWER_CHANGE_ALERT_CODE = 10036
PIR_TEXT_HINTS = ("pir", "motion", "person", "human", "人体", "移动", "有人")
PRELOAD_PROFILES = {
    "on_demand": (
        "rtsp://127.0.0.1:8554/ezviz_raw?video=h265&audio=aac",
        "原始 H.265/AAC",
    ),
    "continuous": (
        "rtsp://127.0.0.1:8554/ezviz?video=h264&audio=opus",
        "HomeKit H.264/Opus",
    ),
}
ACTIVITY_GRACE_SECONDS = 30
MQTT_FALLBACK_GRACE_SECONDS = 10
MQTT_RECREATE_SECONDS = 120


def _report(message: str) -> None:
    print(f"[CB2 保温] {message}", file=sys.stderr, flush=True)


def preload_profile(mode: str) -> tuple[str, str]:
    try:
        return PRELOAD_PROFILES[mode]
    except KeyError as error:
        raise ValueError(f"unsupported HomeKit transcode mode: {mode}") from error


def _to_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _optionals(record: object) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    return _mapping(_mapping(record.get("STATUS")).get("optionals"))


@dataclass(frozen=True)
class PowerState:
    mode: str
    awake_refresh_seconds: int
    work_mode: int | None = None
    power_status: int | None = None


def classify_power(record: object, override: str = "auto") -> PowerState:
    """Return a conservative mains/battery decision from EZVIZ status metadata."""

    optional = _optionals(record)
    work_mode = _to_int(optional.get("batteryCameraWorkMode"))
    power_status = _to_int(optional.get("powerStatus"))
    battery_work = _mapping(optional.get("Battery_WorkStatus"))
    work_time = _to_int(battery_work.get("WorkTime")) or 120
    refresh = max(30, min(300, work_time - 15 if work_time > 45 else work_time))

    if override in {"mains", "battery"}:
        mode = override
    else:
        # The CB2 reports both values as 2 in its official plugged-in mode.
        # Requiring both avoids treating a stale work-mode preference as proof
        # that external power is physically present.
        mode = "mains" if work_mode == 2 and power_status == 2 else "battery"
    return PowerState(mode, refresh, work_mode, power_status)


def _message_fields(message: object) -> tuple[str | None, int | None, str]:
    if not isinstance(message, dict):
        return None, None, ""
    ext = _mapping(message.get("ext"))
    serial = ext.get("device_serial") or message.get("deviceSerial")
    code = _to_int(
        ext.get("alert_type_code")
        or ext.get("alarmType")
        or message.get("alarmType")
        or message.get("alertType")
    )
    text = " ".join(
        str(message.get(key, ""))
        for key in ("alert", "title", "content", "sampleName", "alarmName")
    )
    text += " " + str(ext.get("alarmName", ""))
    text = text.lower()
    return str(serial) if serial else None, code, text


def is_pir_event(message: object, serial: str) -> bool:
    event_serial, code, text = _message_fields(message)
    if event_serial is None or event_serial.upper() != serial.upper():
        return False
    return code in PIR_ALERT_CODES or any(hint in text for hint in PIR_TEXT_HINTS)


def is_power_change_event(message: object, serial: str) -> bool:
    event_serial, code, _ = _message_fields(message)
    return (
        event_serial is not None
        and event_serial.upper() == serial.upper()
        and code == POWER_CHANGE_ALERT_CODE
    )


class PirMessagePoller:
    """Deduplicate EZVIZ unified messages without replaying old alarms."""

    def __init__(self, max_seen: int = 200) -> None:
        self.max_seen = max_seen
        self.initialized = False
        self._seen: set[str] = set()
        self._order: list[str] = []

    def reset(self) -> None:
        self.initialized = False
        self._seen.clear()
        self._order.clear()

    def _remember(self, message_id: str) -> None:
        if message_id in self._seen:
            return
        self._seen.add(message_id)
        self._order.append(message_id)
        while len(self._order) > self.max_seen:
            self._seen.discard(self._order.pop(0))

    def ingest(self, response: object, serial: str) -> bool:
        if not isinstance(response, dict):
            messages: list[object] = []
        else:
            value = response.get("message") or response.get("messages") or []
            messages = value if isinstance(value, list) else []

        fresh: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            ext = _mapping(message.get("ext"))
            message_id = message.get("msgId") or ext.get("rawid")
            if message_id is None:
                continue
            key = str(message_id)
            if key not in self._seen:
                fresh.append(message)
            self._remember(key)

        if not self.initialized:
            self.initialized = True
            return False
        return any(is_pir_event(message, serial) for message in fresh)


def _http_status(error: BaseException) -> int | None:
    current: BaseException | None = error
    for _ in range(4):
        response = getattr(current, "response", None)
        status = _to_int(getattr(response, "status_code", None))
        if status is not None:
            return status
        cause = getattr(current, "__cause__", None)
        current = cause if isinstance(cause, BaseException) else None
        if current is None:
            break
    return None


def _load_token(path: Path) -> dict[str, Any]:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RuntimeError("萤石会话文件权限必须是 0600")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not value.get("session_id"):
        raise RuntimeError("萤石会话文件无效")
    return value


def activity_is_live(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        pid = _to_int(value.get("pid")) if isinstance(value, dict) else None
        if pid is None or pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (FileNotFoundError, ProcessLookupError, json.JSONDecodeError, OSError):
        return False


class ActivityTracker:
    """Mask short shared-marker handovers without hiding a stopped source."""

    def __init__(self, grace_seconds: float = ACTIVITY_GRACE_SECONDS) -> None:
        self.grace_seconds = grace_seconds
        self._last_live_at: float | None = None

    def update(self, marker_live: bool, now: float) -> bool:
        if marker_live:
            self._last_live_at = now
            return True
        return (
            self._last_live_at is not None
            and now - self._last_live_at <= self.grace_seconds
        )


class WarmConsumer:
    """Maintain one low-overhead local RTSP consumer for mains/PIR preloading."""

    def __init__(
        self,
        ffmpeg_bin: str,
        rtsp_url: str,
        *,
        profile_label: str = "本地媒体",
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self.rtsp_url = rtsp_url
        self.profile_label = profile_label
        self._popen = popen
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._mains = False
        self._event_deadline = 0.0
        self._retry_at = 0.0
        self._process: subprocess.Popen[bytes] | None = None
        self._closed = False

    def _command(self) -> list[str]:
        return [
            self.ffmpeg_bin,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            self.rtsp_url,
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-f",
            "null",
            "-",
        ]

    def set_mains(self, enabled: bool) -> None:
        with self._lock:
            if self._closed:
                return
            self._mains = enabled

    def trigger(self, seconds: int) -> None:
        with self._lock:
            if self._closed:
                return
            self._event_deadline = max(
                self._event_deadline,
                self._monotonic() + seconds,
            )

    def release_event(self) -> None:
        """Hand an established event stream to go2rtc's linger window."""
        with self._lock:
            if self._closed:
                return
            self._event_deadline = 0.0

    def desired(self, now: float | None = None) -> bool:
        with self._lock:
            current = self._monotonic() if now is None else now
            return not self._closed and (
                self._mains or current < self._event_deadline
            )

    def tick(self) -> None:
        with self._lock:
            if self._closed:
                return
            now = self._monotonic()
            desired = self._mains or now < self._event_deadline
            if self._process is not None and self._process.poll() is not None:
                returncode = self._process.returncode
                self._process = None
                self._retry_at = now + 5
                _report(f"本地预加载消费者已退出（状态 {returncode}），5 秒后重试。")

            if desired and self._process is None and now >= self._retry_at:
                try:
                    self._process = self._popen(
                        self._command(),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    _report(f"已启动 {self.profile_label} 预加载消费者。")
                except OSError as error:
                    self._retry_at = now + 10
                    _report(f"预加载消费者启动失败：{type(error).__name__}。")
            elif not desired and self._process is not None:
                self._stop_locked()

    def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        _report("本地预加载消费者已停止；进入流保温或休眠阶段。")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._mains = False
            self._event_deadline = 0
            self._stop_locked()


def _new_client(token_file: Path) -> Any:
    from pyezvizapi.client import EzvizClient

    token = _load_token(token_file)
    return EzvizClient(url=str(token["api_url"]), token=token, timeout=15)


def _close_mqtt(client: Any | None, mqtt: Any | None) -> None:
    if mqtt is not None:
        try:
            mqtt.stop()
        except BaseException:
            pass
    if client is not None:
        try:
            client.mqtt_client = None
        except BaseException:
            pass


def _close_cloud(client: Any | None, mqtt: Any | None) -> None:
    _close_mqtt(client, mqtt)
    if client is not None:
        try:
            client.close_session()
        except BaseException:
            pass


def _mqtt_is_connected(mqtt: Any | None) -> bool:
    if mqtt is None:
        return False
    native = getattr(mqtt, "mqtt_client", None)
    checker = getattr(native, "is_connected", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except (OSError, RuntimeError, ValueError):
        return False


def _mqtt_fallback_due(
    mqtt: Any | None,
    offline_since: float | None,
    now: float,
) -> bool:
    if mqtt is None:
        return True
    return (
        not _mqtt_is_connected(mqtt)
        and offline_since is not None
        and now - offline_since >= MQTT_FALLBACK_GRACE_SECONDS
    )


def run_strategy(
    args: argparse.Namespace,
    stop: threading.Event,
    warm: WarmConsumer,
) -> None:
    messages: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
    current_mode = args.power_mode if args.power_mode != "auto" else "battery"
    awake_refresh = 105
    warm.set_mains(current_mode == "mains")
    client: Any | None = None
    mqtt: Any | None = None
    mqtt_offline_since: float | None = None
    next_client_attempt = 0.0
    next_status = 0.0
    next_mqtt_attempt = 0.0
    next_alarm_poll = 0.0
    next_awake_refresh = 0.0
    status_failures = 0
    mode_reported = False
    poll_reported = False
    poller = PirMessagePoller()
    activity = ActivityTracker()

    transcode_label = (
        "HomeKit 按需转码"
        if args.homekit_transcode == "on_demand"
        else "HomeKit 持续转码"
    )

    try:
        while not stop.is_set():
            now = time.monotonic()
            if client is None and now >= next_client_attempt:
                try:
                    client = _new_client(args.token_file)
                    next_status = 0
                    next_mqtt_attempt = 0
                    status_failures = 0
                except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
                    _report(f"无法读取供电策略状态：{type(error).__name__}。")
                    next_client_attempt = now + 30

            if client is not None and now >= next_status:
                try:
                    record = client.get_device_infos(args.serial)
                    state = classify_power(record, args.power_mode)
                    awake_refresh = state.awake_refresh_seconds
                    if state.mode != current_mode:
                        current_mode = state.mode
                        warm.set_mains(current_mode == "mains")
                        if current_mode == "mains" and mqtt is not None:
                            _close_mqtt(client, mqtt)
                            mqtt = None
                            mqtt_offline_since = None
                        if current_mode == "battery":
                            poller.reset()
                            next_alarm_poll = 0
                            poll_reported = False
                        power_label = (
                            "持续供电常驻预加载"
                            if current_mode == "mains"
                            else "电池 10 分钟保温"
                        )
                        label = f"{power_label}，{transcode_label}"
                        _report(f"供电策略切换为：{label}。")
                        mode_reported = True
                    elif not mode_reported:
                        power_label = (
                            "持续供电常驻预加载"
                            if current_mode == "mains"
                            else "电池 10 分钟保温"
                        )
                        label = f"{power_label}，{transcode_label}"
                        _report(f"当前供电策略：{label}。")
                        mode_reported = True
                    next_status = now + args.power_refresh_seconds
                    status_failures = 0
                except BaseException as error:
                    status_failures += 1
                    next_status = now + min(60, 10 * status_failures)
                    _report(f"供电状态刷新失败：{type(error).__name__}。")
                    if status_failures >= 3:
                        _close_cloud(client, mqtt)
                        client = None
                        mqtt = None
                        mqtt_offline_since = None
                        next_client_attempt = now + 30
                        if args.power_mode == "auto" and current_mode != "battery":
                            current_mode = "battery"
                            warm.set_mains(False)
                            poller.reset()
                            next_alarm_poll = 0
                            poll_reported = False
                            _report("供电状态持续不可用，已安全回退到电池保温策略。")

            if (
                args.pir_preheat
                and current_mode == "battery"
                and client is not None
                and mqtt is None
                and now >= next_mqtt_attempt
            ):
                try:
                    mqtt = client.get_mqtt_client(messages.put)
                    mqtt.connect(clean_session=True)
                    mqtt_offline_since = None
                    poll_reported = False
                    _report("PIR 云推送监听已连接。")
                except BaseException as error:
                    mqtt = None
                    mqtt_offline_since = None
                    try:
                        client.mqtt_client = None
                    except BaseException:
                        pass
                    status = _http_status(error)
                    next_mqtt_attempt = now + (3600 if status == 401 else 300)
                    suffix = f"HTTP {status}" if status is not None else type(error).__name__
                    _report(f"PIR 推送监听不可用（{suffix}），改用告警轮询。")

            if mqtt is not None:
                if _mqtt_is_connected(mqtt):
                    mqtt_offline_since = None
                elif mqtt_offline_since is None:
                    mqtt_offline_since = now
                elif now - mqtt_offline_since >= MQTT_RECREATE_SECONDS:
                    _close_mqtt(client, mqtt)
                    mqtt = None
                    mqtt_offline_since = None
                    next_mqtt_attempt = now + 30
                    _report("PIR 推送长时间离线，将重建连接并保持告警轮询。")

            if (
                args.pir_preheat
                and current_mode == "battery"
                and client is not None
                and _mqtt_fallback_due(mqtt, mqtt_offline_since, now)
                and now >= next_alarm_poll
            ):
                try:
                    response = client.get_device_messages_list(
                        serials=args.serial,
                        s_type="9904,2701",
                        limit=10,
                        date="",
                        end_time="",
                    )
                    if poller.ingest(response, args.serial):
                        warm.trigger(args.warm_seconds)
                        _report(
                            f"轮询发现新的 PIR/人体事件，开始预热 {args.warm_seconds // 60} 分钟。"
                        )
                    if not poll_reported:
                        _report(
                            f"PIR 告警轮询备用路径已就绪（{args.pir_poll_seconds} 秒）。"
                        )
                        poll_reported = True
                    next_alarm_poll = now + args.pir_poll_seconds
                except BaseException as error:
                    next_alarm_poll = now + max(60, args.pir_poll_seconds)
                    _report(f"PIR 告警轮询暂不可用：{type(error).__name__}。")

            force_status = False
            while True:
                try:
                    message = messages.get_nowait()
                except queue.Empty:
                    break
                if is_pir_event(message, args.serial):
                    warm.trigger(args.warm_seconds)
                    _report(f"收到 PIR/人体事件，开始预热 {args.warm_seconds // 60} 分钟。")
                if is_power_change_event(message, args.serial):
                    force_status = True
            if force_status:
                next_status = 0

            warm.tick()

            active = activity.update(activity_is_live(args.activity_file), now)
            if current_mode == "battery" and active:
                # The temporary event consumer only needs to establish media.
                # Once the source is live, go2rtc's linger timer owns the full
                # warm window; otherwise PIR preheat would consume two windows.
                warm.release_event()
                warm.tick()
                if next_awake_refresh == 0:
                    # The stream source already sends one awake request during startup.
                    next_awake_refresh = now + awake_refresh
                elif now >= next_awake_refresh:
                    if client is not None:
                        try:
                            client.delay_battery_device_sleep(
                                args.serial,
                                args.channel,
                                1,
                                max_retries=0,
                            )
                            next_awake_refresh = now + awake_refresh
                            _report("已续期当前电池视频流的休眠倒计时。")
                        except BaseException as error:
                            next_awake_refresh = now + 15
                            _report(f"视频流保活续期失败：{type(error).__name__}。")
                    else:
                        next_awake_refresh = now + 15
            else:
                next_awake_refresh = 0

            stop.wait(1)
    finally:
        _close_cloud(client, mqtt)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run go2rtc with adaptive EZVIZ CB2 stream warming",
    )
    parser.add_argument("--serial", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--activity-file", type=Path, required=True)
    parser.add_argument("--ffmpeg-bin", required=True)
    parser.add_argument("--rtsp-url")
    parser.add_argument(
        "--homekit-transcode",
        choices=("on_demand", "continuous"),
        default="on_demand",
    )
    parser.add_argument("--warm-seconds", type=int, default=600)
    parser.add_argument(
        "--power-mode",
        choices=("auto", "mains", "battery"),
        default="auto",
    )
    parser.add_argument("--power-refresh-seconds", type=int, default=300)
    parser.add_argument("--pir-poll-seconds", type=int, default=15)
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument(
        "--pir-preheat",
        choices=("on", "off"),
        default="on",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.pir_preheat = args.pir_preheat == "on"
    if args.warm_seconds < 60:
        parser.error("--warm-seconds must be at least 60")
    if args.power_refresh_seconds < 30:
        parser.error("--power-refresh-seconds must be at least 30")
    if not 5 <= args.pir_poll_seconds <= 300:
        parser.error("--pir-poll-seconds must be between 5 and 300")
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a go2rtc command is required after --")
    return args


def _normalized_returncode(returncode: int | None) -> int:
    """Map signal termination to the conventional shell exit status."""
    code = int(returncode or 0)
    return 128 - code if code < 0 else code


def main() -> int:
    args = _arguments()
    stop = threading.Event()
    profile_url, profile_label = preload_profile(args.homekit_transcode)
    warm = WarmConsumer(
        args.ffmpeg_bin,
        args.rtsp_url or profile_url,
        profile_label=profile_label,
    )
    child = subprocess.Popen(args.command)

    def handle_signal(signum: int, _frame: object) -> None:
        stop.set()
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    worker = threading.Thread(
        target=run_strategy,
        args=(args, stop, warm),
        name="ezviz-warm-strategy",
        daemon=True,
    )
    worker.start()

    try:
        while child.poll() is None:
            if stop.wait(0.5) and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        return _normalized_returncode(child.returncode)
    finally:
        stop.set()
        warm.close()
        worker.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
