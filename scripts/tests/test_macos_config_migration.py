from __future__ import annotations

import importlib.util
from pathlib import Path
import stat

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_DIR / "scripts" / "migrate-ezviz-config.py"
SPEC = importlib.util.spec_from_file_location("macos_config_migration", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def _legacy_config() -> str:
    return """# yaml-language-server: schema
app:
  modules: [rtsp, homekit, exec, ffmpeg]

linger:
  ezviz: "${EZVIZ_LINGER:600s}"

streams:
  ezviz:
    - "exec:/private/python probe-ezviz-direct-reverse.py --serial=CAM123 --deny-internet-after-connect#killsignal=15#killtimeout=5"
    - "ffmpeg:ezviz#video=h264#hardware#raw=-r 15"
    - "ffmpeg:ezviz#audio=opus"

homekit:
  ezviz:
    name: Existing Camera
    pin: 321-54-678
    pairings:
      - client_id=private

log:
  level: debug
"""


def test_upgrade_splits_raw_linger_from_homekit_transcoders(
    tmp_path: Path,
) -> None:
    config = tmp_path / "go2rtc.yaml"
    original = _legacy_config()
    config.write_text(original)
    config.chmod(0o644)

    assert migration.upgrade(config) is True

    migrated = config.read_text()
    backup = config.with_name(
        f"{config.name}.pre-v{migration.CONFIG_VERSION}.bak"
    )
    assert migration.CONFIG_VERSION_LINE in migrated
    assert '  ezviz_raw: "${EZVIZ_LINGER:600s}"' in migrated
    assert "streams:\n  ezviz_raw:" in migrated
    assert "--activity-file=${EZVIZ_ACTIVITY_FILE}" in migrated
    assert "ffmpeg:ezviz_raw#video=h264" in migrated
    assert "ffmpeg:ezviz_raw#audio=opus" in migrated
    assert "ffmpeg:ezviz#" not in migrated
    assert "client_id=private" in migrated
    assert "pin: 321-54-678" in migrated
    assert backup.read_text() == original
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    assert migration.upgrade(config) is False
    assert backup.read_text() == original


def test_upgrade_preserves_unrelated_streams(tmp_path: Path) -> None:
    config = tmp_path / "go2rtc.yaml"
    config.write_text(
        _legacy_config().replace(
            "streams:\n",
            "streams:\n  doorbell:\n    - rtsp://camera.example/live\n",
        )
    )

    migration.upgrade(config)

    assert "  doorbell:\n    - rtsp://camera.example/live" in config.read_text()


def test_upgrade_adds_missing_linger_section(tmp_path: Path) -> None:
    config = tmp_path / "go2rtc.yaml"
    config.write_text(
        _legacy_config().replace(
            'linger:\n  ezviz: "${EZVIZ_LINGER:600s}"\n\n',
            "",
        )
    )

    migration.upgrade(config)

    assert 'linger:\n  ezviz_raw: "${EZVIZ_LINGER:600s}"' in config.read_text()


def test_upgrade_refuses_unknown_ezviz_source_without_writing(
    tmp_path: Path,
) -> None:
    config = tmp_path / "go2rtc.yaml"
    original = _legacy_config().replace(
        '    - "ffmpeg:ezviz#audio=opus"',
        '    - "ffmpeg:ezviz#audio=opus"\n    - "rtsp://unexpected/source"',
    )
    config.write_text(original)

    with pytest.raises(RuntimeError, match="未知源"):
        migration.upgrade(config)

    assert config.read_text() == original
    assert not config.with_name(
        f"{config.name}.pre-v{migration.CONFIG_VERSION}.bak"
    ).exists()
