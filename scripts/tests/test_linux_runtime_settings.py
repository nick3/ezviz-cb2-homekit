from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[2]
LINUX_DIR = PROJECT_DIR / "deploy" / "linux"
sys.path.insert(0, str(LINUX_DIR))

import runtime_settings  # noqa: E402


def _complete() -> dict[str, object]:
    return {
        "serial": "testcb2123456",
        "camera_ip": "192.168.50.21",
    }


def test_normalize_applies_defaults_and_canonicalizes_device() -> None:
    settings = runtime_settings.normalize_settings(_complete())

    assert settings["serial"] == "TESTCB2123456"
    assert settings["camera_ip"] == "192.168.50.21"
    assert settings["homekit_transcode"] == "on_demand"
    assert settings["warm_seconds"] == 600
    assert settings["version"] == runtime_settings.SETTINGS_VERSION


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "239.1.2.3", "8.8.8.8", "not-an-ip", "::1"],
)
def test_normalize_rejects_non_lan_camera_addresses(address: str) -> None:
    with pytest.raises(runtime_settings.SettingsError):
        runtime_settings.normalize_settings(
            {"serial": "TESTCB2123456", "camera_ip": address}
        )


def test_normalize_rejects_non_official_login_endpoint() -> None:
    with pytest.raises(runtime_settings.SettingsError, match="官方"):
        runtime_settings.normalize_settings(
            {**_complete(), "region": "login.attacker.example"}
        )


def test_store_uses_private_atomic_file_and_reloads(tmp_path: Path) -> None:
    store = runtime_settings.SettingsStore(tmp_path / "data")
    store.prepare()

    saved = store.save(_complete())

    assert store.load() == saved
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.data_dir.stat().st_mode) == 0o700
    assert not store.path.with_name(".settings.json.tmp").exists()


def test_load_does_not_chmod_an_already_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = runtime_settings.SettingsStore(tmp_path / "data")
    store.prepare()
    expected = store.save(_complete())

    def unexpected_chmod(_path: Path, _mode: int) -> None:
        raise AssertionError("load should not chmod an already-private file")

    monkeypatch.setattr(Path, "chmod", unexpected_chmod)
    assert store.load() == expected


def test_environment_bootstrap_is_one_time_and_never_overwrites_web_settings(
    tmp_path: Path,
) -> None:
    store = runtime_settings.SettingsStore(tmp_path / "data")
    store.prepare()
    environment = {
        "EZVIZ_SERIAL": "TESTCB2123456",
        "EZVIZ_CAMERA_IP": "192.168.50.21",
        "EZVIZ_WARM_SECONDS": "1200",
    }

    assert store.bootstrap_from_environment(environment) is True
    assert store.load()["warm_seconds"] == 1200
    environment["EZVIZ_WARM_SECONDS"] = "1800"
    assert store.bootstrap_from_environment(environment) is False
    assert store.load()["warm_seconds"] == 1200


def test_invalid_environment_bootstrap_warns_and_keeps_wizard_available(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = runtime_settings.SettingsStore(tmp_path / "data")
    store.prepare()

    assert (
        store.bootstrap_from_environment(
            {
                "EZVIZ_SERIAL": "TESTCB2123456",
                "EZVIZ_CAMERA_IP": "192.168.50.21",
                "EZVIZ_WARM_SECONDS": "not-a-number",
            }
        )
        is False
    )
    assert not store.path.exists()
    assert "忽略无效" in capsys.readouterr().err


def test_prepare_explains_named_volume_ownership_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = runtime_settings.SettingsStore(tmp_path / "data")
    monkeypatch.setattr(runtime_settings.os, "access", lambda *_: False)

    with pytest.raises(OSError, match="PUID=1000、PGID=1000"):
        store.prepare()


def test_token_requires_private_permissions_and_session_id(tmp_path: Path) -> None:
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"session_id": "private"}))
    token.chmod(0o644)
    assert runtime_settings.token_is_ready(token) is False

    token.chmod(0o600)
    assert runtime_settings.token_is_ready(token) is True
    assert (
        runtime_settings.token_matches_identity(token, "TESTCB2123456", "api.ys7.com")
        is False
    )

    runtime_settings.secure_write(
        token.with_name(runtime_settings.AUTH_STATE_FILE_NAME),
        b'{"state":"unbound_import","serial":"","region":""}\n',
    )
    assert runtime_settings.token_matches_identity(token, "", "api.ys7.com") is False
    assert (
        runtime_settings.token_matches_identity(token, "TESTCB2123456", "api.ys7.com")
        is False
    )

    runtime_settings.secure_write(
        token.with_name(runtime_settings.AUTH_STATE_FILE_NAME),
        b'{"serial":"OTHER123"}\n',
    )
    assert (
        runtime_settings.token_matches_identity(token, "OTHER123", "api.ys7.com")
        is False
    )

    runtime_settings.secure_write(
        token.with_name(runtime_settings.AUTH_STATE_FILE_NAME),
        b'{"serial":"OTHER123","region":"api.ys7.com"}\n',
    )
    assert (
        runtime_settings.token_matches_identity(token, "TESTCB2123456", "api.ys7.com")
        is False
    )
    assert (
        runtime_settings.token_matches_identity(token, "OTHER123", "api.ys7.com")
        is True
    )
    assert (
        runtime_settings.token_matches_identity(
            token, "OTHER123", "api.eu.ezvizlife.com"
        )
        is False
    )


def test_bridge_environment_keeps_secrets_out_of_settings(tmp_path: Path) -> None:
    settings = runtime_settings.normalize_settings(
        {**_complete(), "encoder": "vaapi", "warm_seconds": 900}
    )

    environment = runtime_settings.bridge_environment(
        settings,
        data_dir=tmp_path,
        base={"PATH": "/bin"},
    )

    assert environment["EZVIZ_SERIAL"] == "TESTCB2123456"
    assert environment["EZVIZ_HARDWARE_FRAGMENT"] == "#hardware=vaapi"
    assert environment["EZVIZ_LINGER"] == "900s"
    assert environment["EZVIZ_TOKEN_FILE"] == str(tmp_path / "ezviz_token.json")
    joined = " ".join(f"{key}={value}" for key, value in environment.items()).lower()
    assert "password" not in joined
    assert "secret" not in joined


def test_bridge_environment_escapes_homekit_name_for_quoted_yaml(
    tmp_path: Path,
) -> None:
    name = 'Front "Door" \\ Camera'
    settings = runtime_settings.normalize_settings(
        {**_complete(), "homekit_name": name}
    )

    environment = runtime_settings.bridge_environment(
        settings,
        data_dir=tmp_path,
        base={},
    )

    escaped = environment["HOMEKIT_NAME"]
    assert escaped == 'Front \\"Door\\" \\\\ Camera'
    assert json.loads(f'"{escaped}"') == name
