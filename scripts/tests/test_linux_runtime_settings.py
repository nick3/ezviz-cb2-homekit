from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
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


@pytest.mark.parametrize(
    "name",
    ["Front Door\x7f", "Front\x85Door", "Front Door\x9f", "Front Door\ud800"],
)
def test_normalize_rejects_yaml_forbidden_homekit_name_characters(name: str) -> None:
    with pytest.raises(runtime_settings.SettingsError, match="HomeKit 名称"):
        runtime_settings.normalize_settings({**_complete(), "homekit_name": name})


def test_normalize_accepts_unicode_homekit_name() -> None:
    settings = runtime_settings.normalize_settings(
        {**_complete(), "homekit_name": "门口 📷"}
    )

    assert settings["homekit_name"] == "门口 📷"


def test_store_uses_private_atomic_file_and_reloads(tmp_path: Path) -> None:
    store = runtime_settings.SettingsStore(tmp_path / "data")
    store.prepare()

    saved = store.save(_complete())

    assert store.load() == saved
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.data_dir.stat().st_mode) == 0o700
    assert not list(store.path.parent.glob(".settings.json.*.tmp"))


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
        b'{"state":"legacy_upgrade","serial":"OTHER123","region":"api.ys7.com"}\n',
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


def test_persist_bound_token_commits_the_verified_identity(tmp_path: Path) -> None:
    token = tmp_path / "ezviz_token.json"

    runtime_settings.persist_bound_token(
        token,
        b'{"session_id":"private"}\n',
        "testcb2123456",
        "API.EU.EZVIZLIFE.COM",
    )

    assert runtime_settings.token_matches_identity(
        token,
        "TESTCB2123456",
        "api.eu.ezvizlife.com",
    )
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert (
        stat.S_IMODE(
            token.with_name(runtime_settings.AUTH_STATE_FILE_NAME).stat().st_mode
        )
        == 0o600
    )


def test_persist_bound_token_fails_closed_if_the_final_binding_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / "ezviz_token.json"
    real_secure_write = runtime_settings.secure_write
    writes = 0

    def fail_final_write(path: Path, data: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("auth commit failed")
        real_secure_write(path, data)

    monkeypatch.setattr(runtime_settings, "secure_write", fail_final_write)
    with pytest.raises(OSError, match="auth commit failed"):
        runtime_settings.persist_bound_token(
            token,
            b'{"session_id":"replacement"}\n',
            "TESTCB2123456",
            "api.ys7.com",
        )

    assert json.loads(
        token.with_name(runtime_settings.AUTH_STATE_FILE_NAME).read_text()
    ) == {"state": "updating", "serial": "", "region": ""}
    assert (
        runtime_settings.token_matches_identity(
            token,
            "TESTCB2123456",
            "api.ys7.com",
        )
        is False
    )


def test_persist_bound_token_waits_for_a_cross_process_transaction_lock(
    tmp_path: Path,
) -> None:
    token = tmp_path / "ezviz_token.json"
    started = tmp_path / "writer-started"
    environment = {
        **os.environ,
        "PYTHONPATH": str(LINUX_DIR),
        "TOKEN_PATH": str(token),
        "STARTED_PATH": str(started),
    }
    script = (
        "import os\n"
        "from pathlib import Path\n"
        "from runtime_settings import persist_bound_token\n"
        "Path(os.environ['STARTED_PATH']).write_text('started')\n"
        "persist_bound_token(Path(os.environ['TOKEN_PATH']), "
        'b\'{"session_id":"subprocess"}\\n\', '
        "'TESTCB2123456', 'api.ys7.com')\n"
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        with runtime_settings._token_transaction_lock(token):
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 3
            while not started.exists() and process.poll() is None:
                if time.monotonic() >= deadline:
                    raise AssertionError("subprocess did not reach the token write")
                time.sleep(0.01)
            assert started.exists()
            assert process.poll() is None
            assert token.exists() is False

        stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == 0, (stdout, stderr)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=3)

    assert runtime_settings.token_matches_identity(
        token,
        "TESTCB2123456",
        "api.ys7.com",
    )
    assert (
        stat.S_IMODE(
            token.with_name(runtime_settings.BINDING_LOCK_FILE_NAME).stat().st_mode
        )
        == 0o600
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
