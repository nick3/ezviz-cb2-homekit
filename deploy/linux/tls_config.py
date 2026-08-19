#!/usr/bin/env python3
"""Create and validate the per-install TLS identity for the setup wizard."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import secrets
import ssl
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

CERTIFICATE_FILE_NAME = "wizard-cert.pem"
PRIVATE_KEY_FILE_NAME = "wizard-key.pem"
HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
WILDCARD_IPV4 = "0.0.0.0"  # noqa: S104 - certificate SAN filtering sentinel.


class TLSConfigError(RuntimeError):
    """TLS state could not be created or validated safely."""


@dataclass(frozen=True)
class TLSMaterial:
    certificate: Path
    private_key: Path
    fingerprint: str
    created: bool


def _subject_alt_names(host: str, addresses: Iterable[str]) -> tuple[str, ...]:
    names = {"DNS:localhost", "IP:127.0.0.1", "IP:::1"}
    candidates = [host, *addresses]
    for candidate in candidates:
        value = str(candidate).strip().strip("[]").split("%", 1)[0]
        if not value:
            continue
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            if value not in {WILDCARD_IPV4, "::"} and HOSTNAME.fullmatch(value):
                names.add(f"DNS:{value.lower()}")
            continue
        if not address.is_unspecified and not address.is_multicast:
            names.add(f"IP:{address.compressed}")
    return tuple(sorted(names))


def _private_mode(path: Path) -> None:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        path.chmod(0o600)


def _validate_pair(certificate: Path, private_key: Path) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate), str(private_key))


def _fingerprint(certificate: Path) -> str:
    pem = certificate.read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))


def ensure_tls_certificate(
    data_dir: Path,
    *,
    host: str,
    addresses: Iterable[str],
    openssl_bin: str | None = None,
) -> TLSMaterial:
    """Return a private persistent certificate, generating it on first use."""

    certificate = data_dir / CERTIFICATE_FILE_NAME
    private_key = data_dir / PRIVATE_KEY_FILE_NAME
    for path in (certificate, private_key):
        if path.is_symlink():
            raise TLSConfigError(f"TLS 文件不能是符号链接：{path}")

    if certificate.is_file() and private_key.is_file():
        try:
            _private_mode(certificate)
            _private_mode(private_key)
            _validate_pair(certificate, private_key)
            return TLSMaterial(
                certificate,
                private_key,
                _fingerprint(certificate),
                False,
            )
        except (OSError, ValueError, ssl.SSLError):
            # Regenerate a mismatched or truncated pair before the server starts.
            pass

    executable = openssl_bin or "/usr/bin/openssl"
    if not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise TLSConfigError("没有找到 openssl，无法安全启动 Web 配置向导")

    suffix = secrets.token_hex(8)
    temporary_certificate = data_dir / f".{CERTIFICATE_FILE_NAME}.{suffix}.tmp"
    temporary_key = data_dir / f".{PRIVATE_KEY_FILE_NAME}.{suffix}.tmp"
    alt_names = ",".join(_subject_alt_names(host, addresses))
    command = [
        executable,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-sha256",
        "-nodes",
        "-days",
        "825",
        "-subj",
        "/CN=EZVIZ HomeKit Setup",
        "-addext",
        f"subjectAltName={alt_names}",
        "-addext",
        "basicConstraints=critical,CA:FALSE",
        "-addext",
        "keyUsage=critical,digitalSignature,keyEncipherment",
        "-addext",
        "extendedKeyUsage=serverAuth",
        "-keyout",
        str(temporary_key),
        "-out",
        str(temporary_certificate),
    ]
    try:
        subprocess.run(  # noqa: S603 - executable is an absolute verified file
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "LC_ALL": "C"},
        )
        _private_mode(temporary_certificate)
        _private_mode(temporary_key)
        _validate_pair(temporary_certificate, temporary_key)
        os.replace(temporary_key, private_key)
        os.replace(temporary_certificate, certificate)
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ssl.SSLError,
    ) as error:
        raise TLSConfigError("生成 Web 向导 TLS 证书失败") from error
    finally:
        temporary_certificate.unlink(missing_ok=True)
        temporary_key.unlink(missing_ok=True)

    return TLSMaterial(certificate, private_key, _fingerprint(certificate), True)
