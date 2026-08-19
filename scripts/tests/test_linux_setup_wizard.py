from __future__ import annotations

import json
import shutil
import ssl
import stat
import sys
import threading
from collections.abc import Callable
from http.client import HTTPSConnection
from pathlib import Path
from typing import ClassVar

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[2]
LINUX_DIR = PROJECT_DIR / "deploy" / "linux"
sys.path.insert(0, str(LINUX_DIR))

import runtime_settings  # noqa: E402
import setup_wizard  # noqa: E402
import tls_config  # noqa: E402


class VerificationRequired(Exception):
    pass


class ApiError(Exception):
    pass


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

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


@pytest.fixture(autouse=True)
def _reset_fake_clients() -> None:
    FakeClient.instances.clear()


def _application(
    tmp_path: Path,
) -> tuple[setup_wizard.WizardApplication, threading.Event]:
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


def _https_server(
    application: setup_wizard.WizardApplication,
    tmp_path: Path,
) -> setup_wizard.ThreadingHTTPServer:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required by the Linux runtime image")
    tls = tls_config.ensure_tls_certificate(
        tmp_path / "data",
        host="127.0.0.1",
        addresses=["127.0.0.1"],
        openssl_bin=openssl,
    )
    try:
        return setup_wizard.create_server(
            application,
            "127.0.0.1",
            0,
            certificate=tls.certificate,
            private_key=tls.private_key,
        )
    except PermissionError:
        pytest.skip("the execution sandbox forbids local listener sockets")


def _trusted_tls_context(tmp_path: Path) -> ssl.SSLContext:
    return ssl.create_default_context(
        cafile=str(tmp_path / "data" / tls_config.CERTIFICATE_FILE_NAME)
    )


def test_mfa_login_keeps_password_only_in_memory_and_saves_private_token(
    tmp_path: Path,
) -> None:
    application, reloaded = _application(tmp_path)

    first = application.run_login({"account": "owner", "password": "secret"})
    assert first["state"] == "sms_required"
    assert not application.token_file.exists()

    second = application.run_login({"sms_code": "123456"})
    assert second["state"] == "authenticated"
    assert runtime_settings.token_matches_serial(
        application.token_file, "TESTCB2123456"
    )
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


def test_failed_token_replacement_leaves_the_binding_invalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, reloaded = _application(tmp_path)
    runtime_settings.secure_write(
        application.token_file,
        b'{"session_id":"old-session"}\n',
    )
    runtime_settings.secure_write(
        application.token_file.with_name(runtime_settings.AUTH_STATE_FILE_NAME),
        b'{"serial":"TESTCB2123456"}\n',
    )
    real_secure_write = setup_wizard.secure_write

    def fail_token_write(path: Path, data: bytes) -> None:
        if path == application.token_file:
            raise OSError("simulated token write failure")
        real_secure_write(path, data)

    monkeypatch.setattr(setup_wizard, "secure_write", fail_token_write)
    assert (
        application.run_login({"account": "owner", "password": "secret"})["state"]
        == "sms_required"
    )

    with pytest.raises(OSError, match="token write failure"):
        application.run_login({"sms_code": "123456"})

    assert json.loads(
        application.token_file.with_name("ezviz_auth.json").read_text()
    ) == {"state": "updating", "serial": ""}
    assert json.loads(application.token_file.read_text()) == {
        "session_id": "old-session"
    }
    assert reloaded.is_set() is False
    assert FakeClient.instances[-1].closed is True


def test_mfa_completion_failure_closes_and_discards_pending_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, _ = _application(tmp_path)
    assert (
        application.run_login({"account": "owner", "password": "secret"})["state"]
        == "sms_required"
    )
    client = FakeClient.instances[-1]
    monkeypatch.setattr(client, "get_device_infos", lambda _serial=None: {})

    with pytest.raises(setup_wizard.WizardError, match="没有找到摄像头"):
        application.run_login({"sms_code": "123456"})

    assert client.closed is True
    with pytest.raises(setup_wizard.WizardError, match="会话已失效"):
        application.run_login({"sms_code": "123456"})


def test_only_waiting_is_healthy_before_authentication(tmp_path: Path) -> None:
    application, _ = _application(tmp_path)

    healthy, _ = application.healthy()
    assert healthy is True

    application.bridge_status = lambda: {
        "state": "error",
        "message": "HomeKit 配置初始化失败",
    }
    healthy, status = application.healthy()

    assert healthy is False
    assert status["bridge"]["state"] == "error"


def test_abandoned_mfa_login_is_closed_when_its_timer_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeTimer:
        def __init__(
            self,
            interval: float,
            function: Callable[..., None],
            args: tuple[object, ...] = (),
        ) -> None:
            self.interval = interval
            self.function = function
            self.args = args
            self.daemon = False
            self.started = False
            self.cancelled = False
            timers.append(self)

        def start(self) -> None:
            self.started = True

        def cancel(self) -> None:
            self.cancelled = True

        def fire(self) -> None:
            self.function(*self.args)

    timers: list[FakeTimer] = []

    monkeypatch.setattr(setup_wizard.threading, "Timer", FakeTimer)
    coordinator = setup_wizard.LoginCoordinator(
        tmp_path / "ezviz_token.json",
        reload_callback=lambda: None,
        dependencies=(FakeClient, VerificationRequired, ApiError),
    )

    result = coordinator.begin(
        account="owner",
        password="secret",  # noqa: S106 - synthetic test credential
        serial="TESTCB2123456",
        region="api.ys7.com",
    )
    client = FakeClient.instances[-1]
    timer = timers[-1]

    assert result["state"] == "sms_required"
    assert timer.started is True
    assert timer.daemon is True
    assert 0 < timer.interval <= setup_wizard.PENDING_LOGIN_SECONDS

    timer.fire()

    assert client.closed is True
    assert timer.cancelled is True
    with pytest.raises(setup_wizard.WizardError, match="会话已失效"):
        coordinator.finish_sms("123456", expected_mode="configured")


def test_http_server_requires_csrf_for_mutations_and_reports_status(
    tmp_path: Path,
) -> None:
    application, _ = _application(tmp_path)
    server = _https_server(application, tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPSConnection(
        "127.0.0.1",
        server.server_port,
        timeout=3,
        context=_trusted_tls_context(tmp_path),
    )
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


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("127.0.0.1", True),
        ("192.168.50.10", True),
        ("172.20.0.10", True),
        ("100.64.1.10", True),
        ("fd12::10", True),
        ("8.8.8.8", False),
        ("2001:4860:4860::8888", False),
        ("not-an-address", False),
    ],
)
def test_setup_wizard_accepts_only_local_or_private_clients(
    address: str, allowed: bool
) -> None:
    assert setup_wizard._client_allowed(address) is allowed


def test_same_origin_accepts_http_and_https_for_the_exact_host() -> None:
    assert setup_wizard._same_origin(None, "camera.local:8099") is True
    assert (
        setup_wizard._same_origin("http://camera.local:8099", "camera.local:8099")
        is True
    )
    assert (
        setup_wizard._same_origin("https://camera.local:8099", "camera.local:8099")
        is True
    )
    assert (
        setup_wizard._same_origin("https://attacker.example", "camera.local:8099")
        is False
    )


@pytest.mark.parametrize("host", ["::", "::1", "fd12::10", "fe80::1%eth0"])
def test_setup_server_selects_ipv6_address_family(host: str) -> None:
    assert (
        setup_wizard._server_class(host).address_family == setup_wizard.socket.AF_INET6
    )


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "camera.local"])
def test_setup_server_keeps_ipv4_address_family(host: str) -> None:
    assert (
        setup_wizard._server_class(host).address_family == setup_wizard.socket.AF_INET
    )


def test_http_server_rejects_disallowed_clients_before_read_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, _ = _application(tmp_path)
    monkeypatch.setattr(setup_wizard, "_client_allowed", lambda _address: False)
    server = _https_server(application, tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPSConnection(
        "127.0.0.1",
        server.server_port,
        timeout=3,
        context=_trusted_tls_context(tmp_path),
    )
    try:
        connection.request("GET", "/api/status")
        denied_read = connection.getresponse()
        denied_read.read()
        assert denied_read.status == 403

        connection.request(
            "POST",
            "/api/discover",
            body="{}",
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": application.csrf_token,
            },
        )
        denied_write = connection.getresponse()
        denied_write.read()
        assert denied_write.status == 403
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        application.close()


def test_wizard_markup_uses_step_semantics_and_serial_status_polling() -> None:
    html = (LINUX_DIR / "wizard.html").read_text()

    assert '<nav class="steps" aria-label="配置步骤">' in html
    assert html.count('aria-controls="panel-') == 4
    assert html.count('aria-labelledby="step-') == 4
    assert 'aria-current="step"' in html
    assert "setInterval(refreshStatus" not in html
    assert "catch (error)" in html
    assert "window.setTimeout(pollStatus, 3000)" in html
