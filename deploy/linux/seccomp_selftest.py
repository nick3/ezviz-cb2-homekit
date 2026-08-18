#!/usr/bin/env python3
"""Fail the native Linux image build if process network lockdown is broken."""

from __future__ import annotations

import errno
import os
import socket
import sys

from ezviz_network_lock import deny_new_outbound_network


def main() -> int:
    if sys.platform != "linux":
        print("seccomp self-test skipped outside Linux")
        return 0

    # Match the unprivileged runtime rather than relying on image-build root.
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    child = os.fork()
    if child == 0:
        try:
            client = socket.create_connection(listener.getsockname())
            listener.close()
            deny_new_outbound_network(client.fileno())
            client.sendall(b"existing socket works")
            try:
                socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except OSError as error:
                os._exit(0 if error.errno == errno.EPERM else 2)
            os._exit(3)
        except BaseException:
            os._exit(4)

    connection, _ = listener.accept()
    listener.close()
    with connection:
        if connection.recv(64) != b"existing socket works":
            return 5
    _, status = os.waitpid(child, 0)
    exit_code = os.waitstatus_to_exitcode(status)
    if exit_code != 0:
        print(f"Linux seccomp self-test failed with exit code {exit_code}", file=sys.stderr)
        return exit_code
    print("Linux seccomp self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
