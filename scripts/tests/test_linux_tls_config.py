from __future__ import annotations

import shutil
import ssl
import stat
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[2]
LINUX_DIR = PROJECT_DIR / "deploy" / "linux"
sys.path.insert(0, str(LINUX_DIR))

import tls_config  # noqa: E402


def test_subject_alt_names_include_listener_and_discovered_addresses() -> None:
    names = tls_config._subject_alt_names(
        "192.168.50.10",
        ["192.168.50.11", "0.0.0.0", "invalid address"],
    )

    assert "DNS:localhost" in names
    assert "IP:127.0.0.1" in names
    assert "IP:192.168.50.10" in names
    assert "IP:192.168.50.11" in names
    assert all("0.0.0.0" not in name for name in names)


def test_tls_identity_is_private_valid_and_persistent(tmp_path: Path) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required by the Linux runtime image")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)

    first = tls_config.ensure_tls_certificate(
        data_dir,
        host="0.0.0.0",
        addresses=["192.168.50.10"],
        openssl_bin=openssl,
    )

    assert first.created is True
    assert stat.S_IMODE(first.certificate.stat().st_mode) == 0o600
    assert stat.S_IMODE(first.private_key.stat().st_mode) == 0o600
    assert len(first.fingerprint.split(":")) == 32
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(first.certificate), str(first.private_key))
    certificate = first.certificate.read_bytes()
    private_key = first.private_key.read_bytes()

    second = tls_config.ensure_tls_certificate(
        data_dir,
        host="0.0.0.0",
        addresses=["192.168.50.10"],
        openssl_bin=openssl,
    )

    assert second.created is False
    assert second.fingerprint == first.fingerprint
    assert second.certificate.read_bytes() == certificate
    assert second.private_key.read_bytes() == private_key
