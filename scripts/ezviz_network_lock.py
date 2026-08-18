"""Process-local network lockdown for an established EZVIZ LAN stream.

The reverse-direct adapter needs Internet access only while it authenticates,
wakes the camera and submits the CAS invitation.  Once the camera has opened
its media TCP connection, this module prevents the adapter and every child it
starts afterwards from creating another network socket or connecting an
already-open socket.

This is deliberately process-local.  It does not alter host firewall rules,
routes, Docker networking or other applications on the machine.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
from pathlib import Path
import sys


class NetworkLockError(RuntimeError):
    """Raised when the process cannot enter the requested fail-closed state."""


def _process_socket_fds() -> set[int]:
    """Return socket file descriptors currently open by this Linux process."""
    directory = Path("/proc/self/fd")
    if not directory.is_dir():
        raise NetworkLockError("Linux socket audit requires /proc/self/fd")

    sockets: set[int] = set()
    for entry in directory.iterdir():
        try:
            target = os.readlink(entry)
        except (FileNotFoundError, OSError):
            continue
        if target.startswith("socket:["):
            sockets.add(int(entry.name))
    return sockets


def _assert_only_expected_socket(expected_socket_fd: int) -> None:
    sockets = _process_socket_fds()
    if expected_socket_fd not in sockets:
        raise NetworkLockError(
            "The established camera media socket is no longer open"
        )
    unexpected = sockets - {expected_socket_fd}
    if unexpected:
        values = ", ".join(str(value) for value in sorted(unexpected))
        raise NetworkLockError(
            "Refusing network lockdown while unexpected sockets remain open: "
            f"fd {values}"
        )


def _raise_for_seccomp(result: int, operation: str) -> None:
    if result < 0:
        error_number = -result
        raise NetworkLockError(
            f"{operation} failed: {os.strerror(error_number)}"
        )


def _deny_linux_network(expected_socket_fd: int) -> None:
    """Install a thread-synchronised libseccomp filter on Linux."""
    _assert_only_expected_socket(expected_socket_fd)

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int

    # PR_SET_NO_NEW_PRIVS is required before an unprivileged process may load
    # its own seccomp filter. It is irreversible and inherited by children.
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise NetworkLockError(
            f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(error_number)}"
        )

    library = ctypes.util.find_library("seccomp") or "libseccomp.so.2"
    try:
        seccomp = ctypes.CDLL(library, use_errno=True)
    except OSError as error:
        raise NetworkLockError("libseccomp is not installed") from error

    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.restype = None
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_attr_set.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    seccomp.seccomp_attr_set.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int

    scmp_act_allow = 0x7FFF0000
    scmp_act_errno = 0x00050000 | errno.EPERM
    scmp_fltr_attr_ctl_tsync = 4

    context = seccomp.seccomp_init(scmp_act_allow)
    if not context:
        raise NetworkLockError("libseccomp could not allocate a filter")
    try:
        _raise_for_seccomp(
            seccomp.seccomp_attr_set(
                context,
                scmp_fltr_attr_ctl_tsync,
                1,
            ),
            "seccomp thread synchronisation",
        )
        for syscall_name in (b"socket", b"connect"):
            syscall_number = seccomp.seccomp_syscall_resolve_name(syscall_name)
            if syscall_number < 0:
                raise NetworkLockError(
                    f"libseccomp cannot resolve {syscall_name.decode()}"
                )
            _raise_for_seccomp(
                seccomp.seccomp_rule_add(
                    context,
                    scmp_act_errno,
                    syscall_number,
                    0,
                ),
                f"seccomp rule for {syscall_name.decode()}",
            )
        _raise_for_seccomp(seccomp.seccomp_load(context), "seccomp load")
    finally:
        seccomp.seccomp_release(context)


def _deny_macos_network() -> None:
    """Apply Apple's process sandbox after the camera socket is accepted."""
    sandbox = ctypes.CDLL("/usr/lib/libsandbox.dylib")
    sandbox.sandbox_init.argtypes = [
        ctypes.c_char_p,
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.c_char_p),
    ]
    sandbox.sandbox_init.restype = ctypes.c_int
    sandbox.sandbox_free_error.argtypes = [ctypes.c_char_p]
    sandbox.sandbox_free_error.restype = None
    error = ctypes.c_char_p()
    profile = b"(version 1)(allow default)(deny network-outbound)"
    result = sandbox.sandbox_init(profile, 0, ctypes.byref(error))
    if result == 0:
        return
    message = error.value.decode("utf-8", errors="replace") if error.value else ""
    if error.value:
        sandbox.sandbox_free_error(error)
    raise NetworkLockError(message or "macOS rejected the process sandbox")


def deny_new_outbound_network(expected_socket_fd: int) -> str:
    """Deny new outbound networking and return the enforcement backend name."""
    if sys.platform == "linux":
        _deny_linux_network(expected_socket_fd)
        return "Linux seccomp"
    if sys.platform == "darwin":
        _deny_macos_network()
        return "macOS sandbox"
    raise NetworkLockError(
        f"Process-local network lockdown is unsupported on {sys.platform}"
    )
