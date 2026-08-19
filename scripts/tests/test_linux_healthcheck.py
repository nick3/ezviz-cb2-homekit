from __future__ import annotations

import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[2]
LINUX_DIR = PROJECT_DIR / "deploy" / "linux"
HEALTHCHECK = PROJECT_DIR / "deploy" / "linux" / "healthcheck.py"
sys.path.insert(0, str(LINUX_DIR))

import healthcheck  # noqa: E402


def test_healthcheck_uses_the_configured_listener_address() -> None:
    assert healthcheck.healthcheck_host("0.0.0.0") == "127.0.0.1"
    assert healthcheck.healthcheck_host("[0.0.0.0]") == "127.0.0.1"
    assert healthcheck.healthcheck_host("::") == "::1"
    assert healthcheck.healthcheck_host("[::]") == "::1"
    assert healthcheck.healthcheck_host("192.168.50.10") == "192.168.50.10"
    assert healthcheck.healthcheck_host("[fd12::10]") == "fd12::10"
    assert healthcheck.healthcheck_hosts("fe80::1%eth0") == (
        "fe80::1%eth0",
        "fe80::1",
    )
    assert healthcheck.healthcheck_hosts("[fe80::1%eth0]") == (
        "fe80::1%eth0",
        "fe80::1",
    )


def test_scoped_ipv6_connection_uses_unscoped_tls_verification_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_socket = object()
    wrapped_socket = object()
    wrapped: list[tuple[object, str]] = []

    class Context:
        def wrap_socket(self, sock: object, *, server_hostname: str) -> object:
            wrapped.append((sock, server_hostname))
            return wrapped_socket

    monkeypatch.setattr(
        healthcheck.HTTPConnection,
        "connect",
        lambda connection: setattr(connection, "sock", raw_socket),
    )
    connection = healthcheck.HealthcheckHTTPSConnection(
        "fe80::1%eth0",
        8099,
        timeout=2,
        context=Context(),  # type: ignore[arg-type]
        verification_host="fe80::1",
    )

    connection.connect()

    assert connection.host == "fe80::1%eth0"
    assert connection.sock is wrapped_socket
    assert wrapped == [(raw_socket, "fe80::1")]


def test_healthcheck_connects_to_the_configured_tls_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, int, str]] = []
    trust_stores: list[str] = []

    class Response(BytesIO):
        status = 200

    class Connection:
        def __init__(self, host: str, port: int, **kwargs: object) -> None:
            opened.append((host, port, str(kwargs["verification_host"])))

        def request(self, method: str, path: str) -> None:
            assert (method, path) == ("GET", "/api/health")

        def getresponse(self) -> Response:
            return Response(b'{"healthy":true}')

        def close(self) -> None:
            pass

    monkeypatch.setenv("EZVIZ_SETUP_HOST", "192.168.50.10")
    monkeypatch.setenv("EZVIZ_SETUP_PORT", "9443")
    monkeypatch.setenv("EZVIZ_DATA_DIR", "/state")
    monkeypatch.setattr(healthcheck, "HealthcheckHTTPSConnection", Connection)
    monkeypatch.setattr(
        healthcheck.ssl,
        "create_default_context",
        lambda *, cafile: trust_stores.append(cafile) or object(),
    )

    assert healthcheck.main() == 0
    assert opened == [("192.168.50.10", 9443, "192.168.50.10")]
    assert trust_stores == ["/state/wizard-cert.pem"]


def test_invalid_setup_port_exits_cleanly_without_a_traceback() -> None:
    environment = dict(os.environ)
    environment["EZVIZ_SETUP_PORT"] = "not-a-port"
    result = subprocess.run(
        [sys.executable, str(HEALTHCHECK)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
