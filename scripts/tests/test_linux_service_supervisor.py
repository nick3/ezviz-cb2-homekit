from __future__ import annotations

from pathlib import Path
import sys


PROJECT_DIR = Path(__file__).resolve().parents[2]
LINUX_DIR = PROJECT_DIR / "deploy" / "linux"
sys.path.insert(0, str(LINUX_DIR))

import runtime_settings  # noqa: E402
import service_supervisor  # noqa: E402


def test_bridge_command_uses_persistent_state_and_selected_policy(tmp_path: Path) -> None:
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


def test_supervisor_waits_until_both_settings_and_token_are_ready(tmp_path: Path) -> None:
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
    assert ready is True
    assert message == "配置已就绪"
