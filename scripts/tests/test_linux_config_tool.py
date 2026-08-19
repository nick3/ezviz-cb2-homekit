from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_DIR / "deploy" / "linux" / "config_tool.py"
SPEC = importlib.util.spec_from_file_location("linux_config_tool", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
config_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config_tool)


def _template() -> Path:
    return PROJECT_DIR / "deploy" / "linux" / "go2rtc.yaml.tmpl"


def test_init_creates_private_config_with_random_pin(tmp_path: Path) -> None:
    target = tmp_path / "data" / "go2rtc.yaml"

    config_tool._init(_template(), target)

    text = target.read_text()
    assert config_tool.PIN_MARKER not in text
    assert config_tool.CONFIG_VERSION_LINE in text
    assert config_tool.PIN_LINE.search(text) is not None
    assert 'ezviz_raw: "${EZVIZ_LINGER:600s}"' in text
    assert "streams:\n  ezviz_raw:" in text
    assert "ffmpeg:ezviz_raw#video=h264" in text
    assert "ffmpeg:ezviz_raw#audio=opus" in text
    assert "--activity-file=${EZVIZ_ACTIVITY_FILE:/data/ezviz-stream-active.json}" in text
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_init_refuses_to_overwrite_existing_config(tmp_path: Path) -> None:
    target = tmp_path / "go2rtc.yaml"
    target.write_text("existing")

    with pytest.raises(RuntimeError, match="Refusing to overwrite"):
        config_tool._init(_template(), target)


def test_upgrade_preserves_homekit_state_and_backs_up_old_config(
    tmp_path: Path,
) -> None:
    target = tmp_path / "data" / "go2rtc.yaml"
    target.parent.mkdir()
    old_config = """streams:
  ezviz:
    - old-managed-source
homekit:
  ezviz:
    name: Existing Camera
    pin: 321-54-678
    pairings:
      - client_id=private
log:
  level: debug
"""
    target.write_text(old_config)
    target.chmod(0o600)

    assert config_tool._upgrade(_template(), target) is True

    migrated = target.read_text()
    backup = target.with_name(
        f"{target.name}.pre-v{config_tool.CONFIG_VERSION}.bak"
    )
    assert config_tool.CONFIG_VERSION_LINE in migrated
    assert "Existing Camera" in migrated
    assert "321-54-678" in migrated
    assert "client_id=private" in migrated
    assert "old-managed-source" not in migrated
    assert 'ezviz_raw: "${EZVIZ_LINGER:600s}"' in migrated
    assert "streams:\n  ezviz_raw:" in migrated
    assert "ffmpeg:ezviz_raw#video=h264" in migrated
    assert (
        "--activity-file=${EZVIZ_ACTIVITY_FILE:/data/ezviz-stream-active.json}"
        in migrated
    )
    assert backup.read_text() == old_config
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    assert config_tool._upgrade(_template(), target) is False
    assert backup.read_text() == old_config


def test_import_keeps_homekit_state_but_uses_linux_stream(tmp_path: Path) -> None:
    source_config = tmp_path / "mac.yaml"
    source_config.write_text(
        """streams:
  ezviz:
    - mac-only-source
homekit:
  ezviz:
    name: Existing Camera
    pin: 321-54-678
    pairings:
      - client_id=private
log:
  level: info
"""
    )
    source_token = tmp_path / "token.json"
    source_token.write_text(json.dumps({"session_id": "secret"}))
    source_token.chmod(0o600)
    target_config = tmp_path / "data" / "go2rtc.yaml"
    target_token = tmp_path / "data" / "ezviz_token.json"
    args = type(
        "Args",
        (),
        {
            "template": _template(),
            "source_config": source_config,
            "source_token": source_token,
            "target_config": target_config,
            "target_token": target_token,
        },
    )()

    config_tool._import_state(args)

    migrated = target_config.read_text()
    assert "Existing Camera" in migrated
    assert config_tool.CONFIG_VERSION_LINE in migrated
    assert "client_id=private" in migrated
    assert "mac-only-source" not in migrated
    assert "/app/scripts/probe-ezviz-direct-reverse.py" in migrated
    assert 'ezviz_raw: "${EZVIZ_LINGER:600s}"' in migrated
    assert "streams:\n  ezviz_raw:" in migrated
    assert "ffmpeg:ezviz_raw#audio=opus" in migrated
    assert "--activity-file=${EZVIZ_ACTIVITY_FILE:/data/ezviz-stream-active.json}" in migrated
    assert stat.S_IMODE(target_config.stat().st_mode) == 0o600
    assert stat.S_IMODE(target_token.stat().st_mode) == 0o600
