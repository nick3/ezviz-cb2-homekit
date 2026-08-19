#!/usr/bin/env python3
"""Check the supervisor without waking the battery camera."""

from __future__ import annotations

import json
from http.client import HTTPConnection, HTTPException
import os


port = int(os.environ.get("EZVIZ_SETUP_PORT", "8099"))
connection = HTTPConnection("127.0.0.1", port, timeout=2)
try:
    connection.request("GET", "/api/health")
    response = connection.getresponse()
    value = json.load(response)
    if response.status != 200 or value.get("healthy") is not True:
        raise SystemExit(1)
except (HTTPException, OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(1) from None
finally:
    connection.close()
