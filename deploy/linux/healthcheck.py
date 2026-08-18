#!/usr/bin/env python3
"""Check only local listeners; never wake the battery camera."""

from __future__ import annotations

import socket


for port in (1984, 8554):
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        pass
