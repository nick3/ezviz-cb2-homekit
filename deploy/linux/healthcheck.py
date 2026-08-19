#!/usr/bin/env python3
"""Check the supervisor without waking the battery camera."""

from __future__ import annotations

import json
import os
import ssl
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path

from tls_config import CERTIFICATE_FILE_NAME

WILDCARD_IPV4 = "0.0.0.0"  # noqa: S104 - sentinel only; this module never binds.


def healthcheck_host(value: str) -> str:
    host = value.strip()
    if not host or host == WILDCARD_IPV4:
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host.strip("[]")


def healthcheck_hosts(value: str) -> tuple[str, str]:
    """Return the socket host and the certificate-verification host."""

    connect_host = healthcheck_host(value)
    verification_host = (
        connect_host.split("%", 1)[0] if ":" in connect_host else connect_host
    )
    return connect_host, verification_host


class HealthcheckHTTPSConnection(HTTPSConnection):
    """Connect to a scoped address while verifying its unscoped IP SAN."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        context: ssl.SSLContext,
        verification_host: str,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._verification_host = verification_host

    def connect(self) -> None:
        HTTPConnection.connect(self)
        if self.sock is None:
            raise OSError("TLS healthcheck socket was not created")
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self._verification_host,
        )


def main() -> int:
    connection: HealthcheckHTTPSConnection | None = None
    try:
        port = int(os.environ.get("EZVIZ_SETUP_PORT", "8099"))
        host, verification_host = healthcheck_hosts(
            os.environ.get("EZVIZ_SETUP_HOST", WILDCARD_IPV4)
        )
        certificate = Path(os.environ.get("EZVIZ_DATA_DIR", "/data")) / (
            CERTIFICATE_FILE_NAME
        )
        context = ssl.create_default_context(cafile=str(certificate))
        connection = HealthcheckHTTPSConnection(
            host,
            port,
            timeout=2,
            context=context,
            verification_host=verification_host,
        )
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        value = json.load(response)
        if response.status != 200 or value.get("healthy") is not True:
            return 1
    except (HTTPException, OSError, ValueError, json.JSONDecodeError):
        return 1
    finally:
        if connection is not None:
            connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
