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
    assert healthcheck.healthcheck_host("::") == "::1"
    assert healthcheck.healthcheck_host("192.168.50.10") == "192.168.50.10"
    assert healthcheck.healthcheck_host("[fd12::10]") == "fd12::10"


def test_healthcheck_connects_to_the_configured_tls_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, int]] = []
    trust_stores: list[str] = []

    class Response(BytesIO):
        status = 200

    class Connection:
        def __init__(self, host: str, port: int, **_kwargs: object) -> None:
            opened.append((host, port))

        def request(self, method: str, path: str) -> None:
            assert (method, path) == ("GET", "/api/health")

        def getresponse(self) -> Response:
            return Response(b'{"healthy":true}')

        def close(self) -> None:
            pass

    monkeypatch.setenv("EZVIZ_SETUP_HOST", "192.168.50.10")
    monkeypatch.setenv("EZVIZ_SETUP_PORT", "9443")
    monkeypatch.setenv("EZVIZ_DATA_DIR", "/state")
    monkeypatch.setattr(healthcheck, "HTTPSConnection", Connection)
    monkeypatch.setattr(
        healthcheck.ssl,
        "create_default_context",
        lambda *, cafile: trust_stores.append(cafile) or object(),
    )

    assert healthcheck.main() == 0
    assert opened == [("192.168.50.10", 9443)]
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
