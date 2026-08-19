from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_DIR / "deploy" / "linux" / "config_tool.py"
SPEC = importlib.util.spec_from_file_location("linux_config_tool", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
config_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config_tool)


def _template() -> Path:
    return PROJECT_DIR / "deploy" / "linux" / "go2rtc.yaml.tmpl"


def _legacy_config() -> str:
    return """streams:
  ezviz:
    - legacy-source
homekit:
  ezviz:
    name: Existing Camera
    pin: 321-54-678
    pairings:
      - client_id=private
log:
  level: info
"""


def test_init_creates_private_config_with_random_pin(tmp_path: Path) -> None:
    target = tmp_path / "data" / "go2rtc.yaml"

    config_tool.init_config(_template(), target)

    text = target.read_text()
    assert config_tool.PIN_MARKER not in text
    assert config_tool.CONFIG_VERSION_LINE in text
    assert config_tool.PIN_LINE.search(text) is not None
    assert 'ezviz_raw: "${EZVIZ_LINGER:600s}"' in text
    assert "streams:\n  ezviz_raw:" in text
    assert "ffmpeg:ezviz_raw#video=h264" in text
    assert "ffmpeg:ezviz_raw#audio=opus" in text
    assert (
        "--activity-file=${EZVIZ_ACTIVITY_FILE:/data/ezviz-stream-active.json}" in text
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_init_refuses_to_overwrite_existing_config(tmp_path: Path) -> None:
    target = tmp_path / "go2rtc.yaml"
    target.write_text("existing")

    with pytest.raises(RuntimeError, match="Refusing to overwrite"):
        config_tool.init_config(_template(), target)


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

    assert config_tool.upgrade_config(_template(), target) is True

    migrated = target.read_text()
    backup = target.with_name(f"{target.name}.pre-v{config_tool.CONFIG_VERSION}.bak")
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

    assert config_tool.upgrade_config(_template(), target) is False
    assert backup.read_text() == old_config


def test_upgrade_preserves_current_source_when_fixed_backup_conflicts(
    tmp_path: Path,
) -> None:
    target = tmp_path / "data" / "go2rtc.yaml"
    target.parent.mkdir()
    old_config = """streams:
  ezviz:
    - old-managed-source
homekit:
  ezviz:
    pin: 321-54-678
"""
    target.write_text(old_config)
    fixed_backup = target.with_name(
        f"{target.name}.pre-v{config_tool.CONFIG_VERSION}.bak"
    )
    fixed_backup.write_text("older backup")
    fixed_backup.chmod(0o644)

    assert config_tool.upgrade_config(_template(), target) is True

    unique_backups = list(
        target.parent.glob(f"{target.name}.pre-v{config_tool.CONFIG_VERSION}.*.bak")
    )
    assert fixed_backup.read_text() == "older backup"
    assert stat.S_IMODE(fixed_backup.stat().st_mode) == 0o600
    assert len(unique_backups) == 1
    assert unique_backups[0].read_text() == old_config
    assert stat.S_IMODE(unique_backups[0].stat().st_mode) == 0o600


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
    assert (
        "--activity-file=${EZVIZ_ACTIVITY_FILE:/data/ezviz-stream-active.json}"
        in migrated
    )
    assert stat.S_IMODE(target_config.stat().st_mode) == 0o600
    assert stat.S_IMODE(target_token.stat().st_mode) == 0o600
    auth_state = target_token.with_name(config_tool.AUTH_STATE_FILE_NAME)
    assert json.loads(auth_state.read_text()) == {
        "state": "unbound_import",
        "serial": "",
    }
    assert stat.S_IMODE(auth_state.stat().st_mode) == 0o600


def test_legacy_bind_migration_preserves_pairing_and_binds_verified_token(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "go2rtc.yaml").write_text(_legacy_config())
    source_token = source / "ezviz_token.json"
    source_token.write_text(json.dumps({"session_id": "private"}))
    source_token.chmod(0o600)
    target = tmp_path / "named-volume"

    assert (
        config_tool.migrate_legacy_bind_state(
            source,
            target,
            "testcb2123456",
        )
        is True
    )

    target_config = target / "go2rtc.yaml"
    target_token = target / "ezviz_token.json"
    target_auth = target / config_tool.AUTH_STATE_FILE_NAME
    assert "client_id=private" in target_config.read_text()
    assert target_token.read_bytes() == source_token.read_bytes()
    assert json.loads(target_auth.read_text()) == {
        "state": "legacy_upgrade",
        "serial": "TESTCB2123456",
    }
    assert stat.S_IMODE(target_config.stat().st_mode) == 0o600
    assert stat.S_IMODE(target_token.stat().st_mode) == 0o600
    assert stat.S_IMODE(target_auth.stat().st_mode) == 0o600
    assert not (target / config_tool.LEGACY_MIGRATION_MARKER).exists()

    assert config_tool.upgrade_config(_template(), target_config) is True
    upgraded = target_config.read_text()
    assert config_tool.CONFIG_VERSION_LINE in upgraded
    assert "client_id=private" in upgraded
    assert "legacy-source" not in upgraded


def test_legacy_bind_migration_is_a_noop_without_old_state(tmp_path: Path) -> None:
    source = tmp_path / "empty-legacy"
    source.mkdir()
    target = tmp_path / "named-volume"

    assert config_tool.migrate_legacy_bind_state(source, target, "") is False
    assert not target.exists()


def test_legacy_bind_migration_never_overwrites_named_volume_state(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "go2rtc.yaml").write_text(_legacy_config())
    target = tmp_path / "named-volume"
    target.mkdir()
    existing_config = target / "go2rtc.yaml"
    existing_config.write_text("current-state")

    assert (
        config_tool.migrate_legacy_bind_state(
            source,
            target,
            "TESTCB2123456",
        )
        is False
    )
    assert existing_config.read_text() == "current-state"


def test_legacy_bind_migration_rejects_other_named_volume_state(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "go2rtc.yaml").write_text(_legacy_config())
    target = tmp_path / "named-volume"
    target.mkdir()
    settings = target / "settings.json"
    settings.write_text('{"serial":"current"}\n')

    with pytest.raises(RuntimeError, match="Named volume is not empty"):
        config_tool.migrate_legacy_bind_state(
            source,
            target,
            "TESTCB2123456",
        )

    assert settings.read_text() == '{"serial":"current"}\n'
    assert not (target / "go2rtc.yaml").exists()


def test_legacy_bind_migration_fails_closed_for_an_insecure_token(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "go2rtc.yaml").write_text(_legacy_config())
    source_token = source / "ezviz_token.json"
    source_token.write_text(json.dumps({"session_id": "private"}))
    source_token.chmod(0o644)
    target = tmp_path / "named-volume"

    with pytest.raises(RuntimeError, match="permissions must be 0600"):
        config_tool.migrate_legacy_bind_state(
            source,
            target,
            "TESTCB2123456",
        )

    assert not (target / "go2rtc.yaml").exists()
    assert not (target / "ezviz_token.json").exists()


def test_legacy_bind_migration_recovers_an_interrupted_copy(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    source_config = _legacy_config().encode()
    (source / "go2rtc.yaml").write_bytes(source_config)
    source_token = json.dumps({"session_id": "private"}).encode()
    token_path = source / "ezviz_token.json"
    token_path.write_bytes(source_token)
    token_path.chmod(0o600)
    target = tmp_path / "named-volume"
    target.mkdir()
    marker = target / config_tool.LEGACY_MIGRATION_MARKER
    marker.write_bytes(
        config_tool._legacy_marker_payload(
            source_config,
            source_token,
            "TESTCB2123456",
        )
    )
    marker.chmod(0o600)
    partial_token = target / "ezviz_token.json"
    partial_token.write_text("partial")
    partial_token.chmod(0o600)

    assert (
        config_tool.migrate_legacy_bind_state(
            source,
            target,
            "TESTCB2123456",
        )
        is True
    )
    assert json.loads(partial_token.read_text()) == {"session_id": "private"}
    assert (target / "go2rtc.yaml").exists()
    assert not marker.exists()


def test_legacy_bind_migration_assigns_state_to_the_bridge_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "go2rtc.yaml").write_text(_legacy_config())
    target = tmp_path / "named-volume"
    ownership: list[tuple[Path, int, int, bool]] = []

    def record_chown(
        path: Path,
        uid: int,
        gid: int,
        *,
        follow_symlinks: bool,
    ) -> None:
        if not path.exists():
            raise FileNotFoundError(path)
        ownership.append((path, uid, gid, follow_symlinks))

    monkeypatch.setattr(config_tool.os, "chown", record_chown)

    assert config_tool.migrate_legacy_bind_state(
        source,
        target,
        "TESTCB2123456",
        owner=(1000, 1001),
    )
    assert ownership == [
        (target / "go2rtc.yaml", 1000, 1001, False),
        (target, 1000, 1001, False),
    ]
    assert not (target / config_tool.LEGACY_MIGRATION_MARKER).exists()


def test_read_homekit_pin_ignores_other_sections_and_indentation(
    tmp_path: Path,
) -> None:
    config = tmp_path / "go2rtc.yaml"
    config.write_text(
        """streams:
  unrelated:
    pin: 111-11-111
homekit:
 ezviz:
   pin: '321-54-678'
log:
  level: info
"""
    )

    assert config_tool.read_homekit_pin(config) == "321-54-678"


@pytest.mark.parametrize(
    "pin",
    ["1234567890", "1-2345678", "111-11-111", "\"321-54-678'"],
)
def test_read_homekit_pin_rejects_malformed_or_insecure_values(
    tmp_path: Path, pin: str
) -> None:
    config = tmp_path / "go2rtc.yaml"
    config.write_text(f"homekit:\n  ezviz:\n    pin: {pin}\n")

    with pytest.raises(RuntimeError, match="PIN"):
        config_tool.read_homekit_pin(config)
