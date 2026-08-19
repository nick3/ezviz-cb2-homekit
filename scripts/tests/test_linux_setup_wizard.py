from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import stat
import sys
import threading

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
LINUX_DIR = PROJECT_DIR / "deploy" / "linux"
sys.path.insert(0, str(LINUX_DIR))

import runtime_settings  # noqa: E402
import setup_wizard  # noqa: E402


class VerificationRequired(Exception):
    pass


class ApiError(Exception):
    pass


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, account: str, password: str, region: str) -> None:
        self.account = account
        self.password = password
        self.region = region
        self.closed = False
        self.__class__.instances.append(self)

    def login(self, sms_code: int | None = None) -> None:
        if sms_code is None:
            raise VerificationRequired
        if sms_code != 123456:
            raise VerificationRequired

    def get_device_infos(self, serial: str | None = None) -> dict[str, object]:
        device = {
            "deviceInfos": {"deviceSerial": "TESTCB2123456", "deviceType": "CS-CB2"},
            "WIFI": {"address": "192.168.50.21"},
            "CONNECTION": {"localIp": "192.168.50.20"},
        }
        return device if serial is not None else {"TESTCB2123456": device}

    def export_token(self) -> dict[str, object]:
        return {"session_id": "private", "api_url": "https://api.ys7.com"}

    def close_session(self) -> None:
        self.closed = True


def _application(tmp_path: Path) -> tuple[setup_wizard.WizardApplication, threading.Event]:
    data = tmp_path / "data"
    store = runtime_settings.SettingsStore(data)
    store.prepare()
    store.save({"serial": "TESTCB2123456", "camera_ip": "192.168.50.21"})
    config = data / "go2rtc.yaml"
    config.write_text("homekit:\n  ezviz:\n    pin: 321-54-678\n")
    config.chmod(0o600)
    html = tmp_path / "wizard.html"
    html.write_text("<html>__CSRF_TOKEN__</html>")
    reloaded = threading.Event()
    application = setup_wizard.WizardApplication(
        settings_store=store,
        token_file=data / "ezviz_token.json",
        config_file=config,
        html_file=html,
        reload_callback=reloaded.set,
        bridge_status=lambda: {"state": "waiting", "message": "test"},
        discover=lambda **_: [
            {"serial": "TESTCB2123456", "ip": "192.168.50.21", "matches_hint": True}
        ],
        login_dependencies=(FakeClient, VerificationRequired, ApiError),
    )
    return application, reloaded


def test_mfa_login_keeps_password_only_in_memory_and_saves_private_token(
    tmp_path: Path,
) -> None:
    application, reloaded = _application(tmp_path)

    first = application.run_login({"account": "owner", "password": "secret"})
    assert first["state"] == "sms_required"
    assert not application.token_file.exists()

    second = application.run_login({"sms_code": "123456"})
    assert second["state"] == "authenticated"
    assert runtime_settings.token_matches_serial(application.token_file, "TESTCB2123456")
    assert stat.S_IMODE(application.token_file.stat().st_mode) == 0o600
    assert "secret" not in application.token_file.read_text()
    assert json.loads(
        application.token_file.with_name("ezviz_auth.json").read_text()
    ) == {"serial": "TESTCB2123456"}
    assert reloaded.is_set()
    assert FakeClient.instances[-1].closed is True


def test_cloud_assisted_identification_expands_suffix_and_returns_lan_ip(
    tmp_path: Path,
) -> None:
    application, _ = _application(tmp_path)

    first = application.run_identify(
        {"serial_hint": "123456", "account": "owner", "password": "secret"}
    )
    assert first["state"] == "sms_required"

    identified = application.run_identify({"sms_code": "123456"})
    assert identified["state"] == "identified"
    assert identified["device"] == {
        "serial": "TESTCB2123456",
        "ip": "192.168.50.21",
        "model": "CS-CB2",
        "source": "cloud_metadata",
        "matches_hint": True,
    }
    assert runtime_settings.token_matches_serial(
        application.token_file, "TESTCB2123456"
    )


def test_http_server_requires_csrf_for_mutations_and_reports_status(
    tmp_path: Path,
) -> None:
    application, _ = _application(tmp_path)
    try:
        server = setup_wizard.create_server(application, "127.0.0.1", 0)
    except PermissionError:
        pytest.skip("the execution sandbox forbids local listener sockets")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request("GET", "/")
        page = connection.getresponse()
        html = page.read().decode()
        assert page.status == 200
        assert application.csrf_token in html
        assert "__CSRF_TOKEN__" not in html

        connection.request("GET", "/api/status")
        response = connection.getresponse()
        status = json.loads(response.read())
        assert response.status == 200
        assert status["configured"] is True
        assert status["homekit_pin"] == "321-54-678"

        body = json.dumps({"serial_hint": "123456"})
        connection.request(
            "POST",
            "/api/discover",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        forbidden = connection.getresponse()
        forbidden.read()
        assert forbidden.status == 403

        connection.request(
            "POST",
            "/api/discover",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": application.csrf_token,
            },
        )
        accepted = connection.getresponse()
        discovered = json.loads(accepted.read())
        assert accepted.status == 200
        assert discovered["devices"][0]["ip"] == "192.168.50.21"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        application.close()
