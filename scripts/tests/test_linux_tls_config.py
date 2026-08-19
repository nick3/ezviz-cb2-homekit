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


def test_tls_identity_is_renewed_before_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    certificate = first.certificate.read_bytes()
    private_key = first.private_key.read_bytes()
    monkeypatch.setattr(
        tls_config,
        "CERTIFICATE_RENEWAL_SECONDS",
        826 * 24 * 60 * 60,
    )

    renewed = tls_config.ensure_tls_certificate(
        data_dir,
        host="0.0.0.0",
        addresses=["192.168.50.10"],
        openssl_bin=openssl,
    )

    assert renewed.created is True
    assert renewed.certificate.read_bytes() != certificate
    assert renewed.private_key.read_bytes() != private_key


@pytest.mark.parametrize(
    ("first_host", "first_addresses", "next_host", "next_addresses"),
    [
        ("0.0.0.0", ["192.168.50.10"], "0.0.0.0", ["192.168.50.11"]),
        ("setup-old.example", [], "setup-new.example", []),
    ],
)
def test_tls_identity_is_renewed_when_required_sans_change(
    tmp_path: Path,
    first_host: str,
    first_addresses: list[str],
    next_host: str,
    next_addresses: list[str],
) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required by the Linux runtime image")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)

    first = tls_config.ensure_tls_certificate(
        data_dir,
        host=first_host,
        addresses=first_addresses,
        openssl_bin=openssl,
    )
    certificate = first.certificate.read_bytes()
    private_key = first.private_key.read_bytes()

    renewed = tls_config.ensure_tls_certificate(
        data_dir,
        host=next_host,
        addresses=next_addresses,
        openssl_bin=openssl,
    )

    assert renewed.created is True
    assert renewed.certificate.read_bytes() != certificate
    assert renewed.private_key.read_bytes() != private_key
    reused = tls_config.ensure_tls_certificate(
        data_dir,
        host=next_host,
        addresses=next_addresses,
        openssl_bin=openssl,
    )
    assert reused.created is False
    assert reused.fingerprint == renewed.fingerprint
