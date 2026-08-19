from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[2]
LINUX_DIR = PROJECT_DIR / "deploy" / "linux"
sys.path.insert(0, str(LINUX_DIR))

import runtime_settings  # noqa: E402
import service_supervisor  # noqa: E402


class FakeProcess:
    next_pid: ClassVar[int] = 4000

    def __init__(
        self, *, returncode: int | None = None, stubborn: bool = False
    ) -> None:
        self.returncode = returncode
        self.stubborn = stubborn
        self.calls: list[object] = []
        self.pid = self.__class__.next_pid
        self.__class__.next_pid += 1

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.calls.append("terminate")
        if not self.stubborn:
            self.returncode = 0

    def kill(self) -> None:
        self.calls.append("kill")
        if not self.stubborn:
            self.returncode = -9

    def wait(self, timeout: float) -> int:
        self.calls.append(("wait", timeout))
        if self.stubborn:
            raise subprocess.TimeoutExpired("fake-bridge", timeout)
        assert self.returncode is not None
        return self.returncode


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for supervisor state")


def _ready_supervisor(
    tmp_path: Path,
    *,
    popen: Callable[..., FakeProcess] | None = None,
) -> service_supervisor.BridgeSupervisor:
    store = runtime_settings.SettingsStore(tmp_path / "data")
    store.prepare()
    store.save({"serial": "TESTCB2123456", "camera_ip": "192.168.50.21"})
    token = store.data_dir / "ezviz_token.json"
    runtime_settings.secure_write(token, b'{"session_id":"private"}\n')
    runtime_settings.secure_write(
        token.with_name(runtime_settings.AUTH_STATE_FILE_NAME),
        json.dumps({"serial": "TESTCB2123456"}).encode() + b"\n",
    )
    options: dict[str, object] = {}
    if popen is not None:
        options["popen"] = popen
    return service_supervisor.BridgeSupervisor(
        settings_store=store,
        token_file=token,
        config_file=store.data_dir / "go2rtc.yaml",
        project_dir=Path("/app"),
        data_dir=store.data_dir,
        **options,
    )


def test_bridge_command_uses_persistent_state_and_selected_policy(
    tmp_path: Path,
) -> None:
    settings = runtime_settings.normalize_settings(
        {
            "serial": "TESTCB2123456",
            "camera_ip": "192.168.50.21",
            "homekit_transcode": "continuous",
            "power_mode": "battery",
        }
    )

    command = service_supervisor.bridge_command(
        settings,
        project_dir=Path("/app"),
        data_dir=Path("/data"),
        config_file=Path("/data/go2rtc.yaml"),
    )

    assert command[0] == "/usr/local/bin/python3"
    assert "/app/scripts/ezviz_warm_controller.py" in command
    assert command[command.index("--homekit-transcode") + 1] == "continuous"
    assert command[command.index("--power-mode") + 1] == "battery"
    assert command[-2:] == ["-c", "/data/go2rtc.yaml"]


def test_supervisor_waits_until_both_settings_and_token_are_ready(
    tmp_path: Path,
) -> None:
    store = runtime_settings.SettingsStore(tmp_path / "data")
    store.prepare()
    supervisor = service_supervisor.BridgeSupervisor(
        settings_store=store,
        token_file=store.data_dir / "ezviz_token.json",
        config_file=store.data_dir / "go2rtc.yaml",
        project_dir=Path("/app"),
        data_dir=store.data_dir,
    )

    _, ready, message = supervisor._ready()
    assert ready is False
    assert "配置" in message

    store.save({"serial": "TESTCB2123456", "camera_ip": "192.168.50.21"})
    _, ready, message = supervisor._ready()
    assert ready is False
    assert "登录" in message

    runtime_settings.secure_write(
        supervisor.token_file,
        b'{"session_id":"private"}\n',
    )
    _, ready, message = supervisor._ready()
    assert ready is False
    assert "尚未绑定" in message

    runtime_settings.secure_write(
        supervisor.token_file.with_name(runtime_settings.AUTH_STATE_FILE_NAME),
        b'{"serial":"TESTCB2123456"}\n',
    )
    _, ready, message = supervisor._ready()
    assert ready is True
    assert message == "配置已就绪"


def test_mtime_returns_none_when_state_path_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert service_supervisor._mtime_ns(missing) is None
    missing.write_text("ready")
    assert isinstance(service_supervisor._mtime_ns(missing), int)


def test_stop_child_preserves_a_process_that_cannot_be_reaped(tmp_path: Path) -> None:
    supervisor = _ready_supervisor(tmp_path)
    process = FakeProcess(stubborn=True)
    supervisor._process = process  # type: ignore[assignment]

    assert supervisor._stop_child() is False
    assert supervisor._process is process
    assert process.calls == ["terminate", ("wait", 12), "kill", ("wait", 3)]
    assert "不会启动新实例" in supervisor.status()["message"]


def test_supervisor_run_starts_reloads_and_stops_one_child_at_a_time(
    tmp_path: Path,
) -> None:
    processes: list[FakeProcess] = []

    def fake_popen(_command: list[str], **_kwargs: object) -> FakeProcess:
        process = FakeProcess()
        processes.append(process)
        return process

    supervisor = _ready_supervisor(tmp_path, popen=fake_popen)
    thread = threading.Thread(target=supervisor.run, daemon=True)
    thread.start()
    try:
        _wait_until(lambda: len(processes) == 1)
        supervisor.request_reload()
        _wait_until(lambda: len(processes) == 2)
        assert "terminate" in processes[0].calls
    finally:
        supervisor.request_stop()
        thread.join(timeout=3)

    assert not thread.is_alive()
    assert "terminate" in processes[-1].calls


def test_supervisor_run_retries_after_an_unexpected_child_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[FakeProcess] = []

    def fake_popen(_command: list[str], **_kwargs: object) -> FakeProcess:
        process = FakeProcess(returncode=7 if not processes else None)
        processes.append(process)
        return process

    monkeypatch.setattr(service_supervisor, "RESTART_DELAY_SECONDS", 0.0)
    supervisor = _ready_supervisor(tmp_path, popen=fake_popen)
    thread = threading.Thread(target=supervisor.run, daemon=True)
    thread.start()
    try:
        _wait_until(lambda: len(processes) == 2)
        assert supervisor.status()["state"] == "running"
    finally:
        supervisor.request_stop()
        thread.join(timeout=3)

    assert not thread.is_alive()


def test_recoverable_initialization_errors_are_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = runtime_settings.SettingsStore(tmp_path / "data")
    store.prepare()

    def fail_bootstrap() -> bool:
        raise OSError("settings unavailable")

    def fail_homekit(_config: Path, _template: Path) -> None:
        raise RuntimeError("config invalid")

    monkeypatch.setattr(store, "bootstrap_from_environment", fail_bootstrap)
    monkeypatch.setattr(service_supervisor, "_initialize_homekit_config", fail_homekit)

    message = service_supervisor._initialize_persistent_state(
        store, tmp_path / "go2rtc.yaml", tmp_path / "template.yaml"
    )

    assert "旧版环境变量迁移失败" in message
    assert "HomeKit 配置初始化失败" in message
    supervisor = service_supervisor.BridgeSupervisor(
        settings_store=store,
        token_file=tmp_path / "token.json",
        config_file=tmp_path / "go2rtc.yaml",
        project_dir=Path("/app"),
        data_dir=store.data_dir,
        startup_error=message,
    )
    _, ready, reason = supervisor._ready()
    assert ready is False
    assert reason == message
    assert supervisor.status()["state"] == "error"
