from __future__ import annotations

import errno
import os
from pathlib import Path
import socket
import sys

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from ezviz_network_lock import _assert_only_expected_socket, deny_new_outbound_network


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux seccomp")
def test_linux_lock_keeps_existing_socket_and_blocks_new_socket() -> None:
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
        assert connection.recv(64) == b"existing socket works"
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0


@pytest.mark.skipif(sys.platform != "linux", reason="requires /proc socket audit")
def test_linux_socket_audit_rejects_an_unexpected_socket() -> None:
    first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="unexpected sockets"):
            _assert_only_expected_socket(first.fileno())
    finally:
        first.close()
        second.close()
