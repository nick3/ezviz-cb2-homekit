from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[2]
CONTROLLER_PATH = PROJECT_DIR / "scripts" / "ezviz_warm_controller.py"
CONTROLLER_SPEC = importlib.util.spec_from_file_location(
    "ezviz_warm_controller",
    CONTROLLER_PATH,
)
assert CONTROLLER_SPEC is not None and CONTROLLER_SPEC.loader is not None
controller = importlib.util.module_from_spec(CONTROLLER_SPEC)
sys.modules[CONTROLLER_SPEC.name] = controller
CONTROLLER_SPEC.loader.exec_module(controller)

PROBE_PATH = PROJECT_DIR / "scripts" / "probe-ezviz-direct-reverse.py"
PROBE_SPEC = importlib.util.spec_from_file_location("ezviz_direct_probe", PROBE_PATH)
assert PROBE_SPEC is not None and PROBE_SPEC.loader is not None
probe = importlib.util.module_from_spec(PROBE_SPEC)
PROBE_SPEC.loader.exec_module(probe)


def _record(work_mode: int, power_status: int, work_time: int = 120) -> dict[str, Any]:
    return {
        "STATUS": {
            "optionals": {
                "batteryCameraWorkMode": work_mode,
                "powerStatus": power_status,
                "Battery_WorkStatus": {
                    "WorkTime": work_time,
                    "KeepAlive": 60,
                },
            }
        }
    }


def test_power_auto_requires_live_plugged_in_signals() -> None:
    state = controller.classify_power(_record(2, 2))
    assert state.mode == "mains"
    assert state.awake_refresh_seconds == 105

    assert controller.classify_power(_record(2, 0)).mode == "battery"
    assert controller.classify_power(_record(1, 2)).mode == "battery"
    assert controller.classify_power({}, "mains").mode == "mains"
    assert controller.classify_power(_record(2, 2), "battery").mode == "battery"


def test_preload_profiles_default_to_raw_or_continuous_homekit_tracks() -> None:
    on_demand_url, on_demand_label = controller.preload_profile("on_demand")
    assert "/ezviz_raw?" in on_demand_url
    assert "video=h265&audio=aac" in on_demand_url
    assert on_demand_label == "原始 H.265/AAC"

    continuous_url, continuous_label = controller.preload_profile("continuous")
    assert "/ezviz?" in continuous_url
    assert "video=h264&audio=opus" in continuous_url
    assert continuous_label == "HomeKit H.264/Opus"


def test_cli_defaults_to_on_demand_homekit_transcoding(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ezviz_warm_controller.py",
            "--serial",
            "CAM123",
            "--token-file",
            str(tmp_path / "token.json"),
            "--activity-file",
            str(tmp_path / "active.json"),
            "--ffmpeg-bin",
            "/usr/bin/ffmpeg",
            "--",
            "/bin/true",
        ],
    )

    args = controller._arguments()

    assert args.homekit_transcode == "on_demand"
    assert args.rtsp_url is None


def test_pir_event_filters_serial_and_alarm_type() -> None:
    message = {
        "ext": {
            "device_serial": "CAM123",
            "alert_type_code": 10000,
        }
    }
    assert controller.is_pir_event(message, "cam123") is True
    assert controller.is_pir_event(message, "OTHER") is False
    assert controller.is_pir_event(
        {"deviceSerial": "CAM123", "alarmType": 10036},
        "CAM123",
    ) is False
    assert controller.is_power_change_event(
        {"deviceSerial": "CAM123", "alarmType": 10036},
        "CAM123",
    ) is True
    assert controller.is_pir_event(
        {
            "deviceSerial": "CAM123",
            "msgId": "alarm-1",
            "ext": {"alarmType": 15010},
        },
        "CAM123",
    ) is True


def test_unified_message_poller_seeds_then_reports_only_new_pir() -> None:
    poller = controller.PirMessagePoller()

    baseline = {
        "message": [
            {
                "deviceSerial": "CAM123",
                "msgId": "old",
                "ext": {"alarmType": 15010},
            }
        ]
    }
    assert poller.ingest(baseline, "CAM123") is False
    assert poller.ingest(baseline, "CAM123") is False

    new_alarm = {
        "message": baseline["message"]
        + [
            {
                "deviceSerial": "CAM123",
                "msgId": "new",
                "ext": {"alarmType": 15010},
            }
        ]
    }
    assert poller.ingest(new_alarm, "CAM123") is True
    assert poller.ingest(new_alarm, "CAM123") is False

    wrong_camera = {
        "message": [
            {
                "deviceSerial": "OTHER",
                "msgId": "other",
                "ext": {"alarmType": 15010},
            }
        ]
    }
    assert poller.ingest(wrong_camera, "CAM123") is False


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        assert self.returncode is not None
        return self.returncode


def test_warm_consumer_handles_mains_and_timed_preheat() -> None:
    now = [100.0]
    processes: list[FakeProcess] = []
    commands: list[list[str]] = []

    def fake_popen(command: list[str], **_: object) -> FakeProcess:
        commands.append(command)
        process = FakeProcess()
        processes.append(process)
        return process

    warm = controller.WarmConsumer(
        "/usr/bin/ffmpeg",
        "rtsp://127.0.0.1:8554/ezviz?video=h264&audio=opus",
        popen=fake_popen,
        monotonic=lambda: now[0],
    )

    warm.set_mains(True)
    warm.tick()
    assert len(processes) == 1
    assert "video=h264&audio=opus" in commands[0][commands[0].index("-i") + 1]
    assert "-rw_timeout" not in commands[0]

    warm.set_mains(False)
    warm.tick()
    assert processes[0].terminated is True

    warm.trigger(600)
    warm.tick()
    assert len(processes) == 2
    warm.release_event()
    warm.tick()
    assert processes[1].terminated is True

    warm.trigger(600)
    warm.tick()
    assert len(processes) == 3
    now[0] += 601
    warm.tick()
    assert processes[2].terminated is True


def test_warm_consumer_close_is_terminal() -> None:
    processes: list[FakeProcess] = []

    def fake_popen(_command: list[str], **_: object) -> FakeProcess:
        process = FakeProcess()
        processes.append(process)
        return process

    warm = controller.WarmConsumer(
        "/usr/bin/ffmpeg",
        "rtsp://127.0.0.1:8554/ezviz",
        popen=fake_popen,
    )
    warm.set_mains(True)
    warm.tick()
    assert len(processes) == 1

    warm.close()
    warm.set_mains(True)
    warm.trigger(600)
    warm.release_event()
    warm.tick()

    assert processes[0].terminated is True
    assert len(processes) == 1
    assert warm.desired() is False


def test_activity_tracker_tolerates_short_marker_handover() -> None:
    activity = controller.ActivityTracker(grace_seconds=30)
    assert activity.update(True, 100.0) is True
    assert activity.update(False, 129.0) is True
    assert activity.update(False, 131.0) is False


class FakeNativeMqtt:
    def __init__(self, connected: bool) -> None:
        self.connected = connected

    def is_connected(self) -> bool:
        return self.connected


class FakeMqtt:
    def __init__(self, connected: bool) -> None:
        self.mqtt_client = FakeNativeMqtt(connected)


def test_mqtt_disconnect_enables_polling_before_connection_rebuild() -> None:
    mqtt = FakeMqtt(False)
    assert controller._mqtt_fallback_due(mqtt, 100.0, 109.0) is False
    assert controller._mqtt_fallback_due(mqtt, 100.0, 110.0) is True

    mqtt.mqtt_client.connected = True
    assert controller._mqtt_fallback_due(mqtt, 100.0, 200.0) is False
    assert controller._mqtt_fallback_due(None, None, 200.0) is True


def test_signal_return_codes_are_normalized() -> None:
    assert controller._normalized_returncode(None) == 0
    assert controller._normalized_returncode(7) == 7
    assert controller._normalized_returncode(-15) == 143


def test_activity_marker_is_private_and_process_scoped(tmp_path: Path) -> None:
    marker = tmp_path / "active.json"
    assert probe._mark_stream_active(marker) is True
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert controller.activity_is_live(marker) is True

    marker.write_text('{"pid":99999999}', encoding="utf-8")
    assert controller.activity_is_live(marker) is False

    marker.write_text(f'{{"pid":{os.getpid()}}}', encoding="utf-8")
    probe._clear_stream_active(marker)
    assert marker.exists() is False


def test_activity_marker_can_be_reasserted_after_overlapping_process_exits(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "active.json"
    assert probe._refresh_stream_active(marker, pid=101) is True
    assert json.loads(marker.read_text())["pid"] == 101

    assert probe._refresh_stream_active(marker, pid=202) is True
    assert json.loads(marker.read_text())["pid"] == 202
    probe._clear_stream_active(marker, pid=202)
    assert marker.exists() is False

    assert probe._refresh_stream_active(marker, pid=101) is True
    assert json.loads(marker.read_text())["pid"] == 101
