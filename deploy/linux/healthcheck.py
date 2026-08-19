#!/usr/bin/env python3
"""Check the supervisor without waking the battery camera."""

from __future__ import annotations

import json
import os
import ssl
from http.client import HTTPException, HTTPSConnection
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


def main() -> int:
    connection: HTTPSConnection | None = None
    try:
        port = int(os.environ.get("EZVIZ_SETUP_PORT", "8099"))
        host = healthcheck_host(os.environ.get("EZVIZ_SETUP_HOST", WILDCARD_IPV4))
        certificate = Path(os.environ.get("EZVIZ_DATA_DIR", "/data")) / (
            CERTIFICATE_FILE_NAME
        )
        context = ssl.create_default_context(cafile=str(certificate))
        connection = HTTPSConnection(host, port, timeout=2, context=context)
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
