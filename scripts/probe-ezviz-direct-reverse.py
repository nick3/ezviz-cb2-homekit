#!/usr/bin/env python3
"""Probe the EZVIZ native reverse-direct callback without exposing credentials."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import stat
import subprocess
import sys
from threading import Thread
import time
from typing import Any
import xml.etree.ElementTree as ET


PROJECT_DIR = Path(__file__).resolve().parent.parent
PY_EZVIZ_DIR = PROJECT_DIR / ".tmp" / "pyEzvizApiCN"
# Device metadata currently points to a legacy Hikvision/EZVIZ CAS endpoint
# serving this expired Entrust certificate. Pinning the exact DER fingerprint
# retains server identity checking without accepting arbitrary certificates.
LEGACY_YS7_CAS_CERT_SHA256 = (
    "234e9cde3a0c2a77d93023f0e4611c10231877c77c007c4efb92ab2d15408424"
)
sys.path.insert(0, str(PY_EZVIZ_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from ezviz_direct_media import (  # noqa: E402
    EzvizDirectMediaDeframer,
    EzvizMediaOutput,
)
from ezviz_network_lock import (  # noqa: E402
    NetworkLockError,
    deny_new_outbound_network,
)

from pyezvizapi.cas import (  # noqa: E402
    CAS_COMMAND_DIRECT_REVERSE_CHECK_REQUEST,
    CAS_COMMAND_DIRECT_REVERSE_CHECK_RESPONSE,
    CAS_FRAME_MAGIC,
    CAS_SOCKET_TIMEOUT,
    CasDeviceSession,
    CasInviteStreamResponse,
    EzvizCAS,
    _build_native_plain_frame,
    _parse_native_frame,
    _recv_native_frame,
)
from pyezvizapi.client import EzvizClient  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ask an owned EZVIZ camera to connect to a local TCP listener",
    )
    parser.add_argument("--serial", required=True, help="Camera serial number")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=PROJECT_DIR / ".tmp" / "ezviz_token.json",
    )
    parser.add_argument("--camera-ip", help="Expected LAN source address")
    parser.add_argument("--listen-ip", help="LAN address to advertise to the camera")
    parser.add_argument("--port", type=int, default=0, help="Listener port (0 = automatic)")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--callback-attempts",
        type=int,
        default=3,
        help="CAS wake/reverse-callback attempts for a sleeping battery camera",
    )
    parser.add_argument("--hold-seconds", type=float, default=5.0)
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument(
        "--stream-type",
        type=int,
        choices=(1, 2),
        default=2,
        help="1 = main stream, 2 = sub stream",
    )
    parser.add_argument("--media-seconds", type=float, default=20.0)
    parser.add_argument(
        "--output-format",
        choices=("none", "mpegps", "mpegts"),
        default="none",
        help=(
            "Write clean MPEG-PS or remuxed MPEG-TS to stdout; diagnostics move "
            "to stderr. With --media-seconds 0, stream until disconnected."
        ),
    )
    parser.add_argument(
        "--ffmpeg-bin",
        default=(
            os.environ.get("FFMPEG_BIN")
            or shutil.which("ffmpeg")
            or "/opt/homebrew/bin/ffmpeg"
        ),
        help="FFmpeg used only for lossless MPEG-PS to MPEG-TS remuxing",
    )
    parser.add_argument(
        "--skip-awake-control",
        action="store_true",
        help="Do not send the official live-preview battery awake-time control",
    )
    parser.add_argument(
        "--deny-internet-after-connect",
        action="store_true",
        help=(
            "After CAS setup closes, apply a process lock that forbids all "
            "new outbound network connections while retaining the accepted "
            "camera LAN socket"
        ),
    )
    parser.add_argument(
        "--strict-camera-ip",
        action="store_true",
        help="Reject callback connections that do not match --camera-ip",
    )
    parser.add_argument(
        "--capture-file",
        type=Path,
        help="Optional 0600 file for the raw direct-reverse media connection",
    )
    return parser.parse_args()


_REPORT_FILE: Any = sys.stdout


def _report(message: str) -> None:
    print(message, file=_REPORT_FILE, flush=True)


def _load_token(path: Path) -> dict[str, Any]:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RuntimeError("Token file permissions must be 0600")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Token file must contain a JSON object")
    return data


def _local_ip_for(camera_ip: str | None) -> str:
    target = camera_ip or "192.0.2.1"
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((target, 9))
        return str(probe.getsockname()[0])
    finally:
        probe.close()


def _callback_response_xml() -> bytes:
    return (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b"<Response>\n"
        b"\t<Result>0</Result>\n"
        b"</Response>\n"
    )


def _subserial_suffix(payload: bytes) -> str | None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    value = root.findtext("SubSerial")
    return value[-6:] if value else None


def _notify_worker(
    cas: EzvizCAS,
    serial: str,
    device_session: CasDeviceSession,
    listener_ip: str,
    listener_port: int,
    result_queue: queue.Queue[BaseException | object],
) -> None:
    try:
        result_queue.put(
            cas.notify_device_direct_reverse(
                serial,
                listener_ip=listener_ip,
                listener_port=listener_port,
                device_session=device_session,
            )
        )
    except BaseException as err:  # Keep the listener alive even on a parse error.
        result_queue.put(err)


def _invite_worker(
    cas: EzvizCAS,
    serial: str,
    device_session: CasDeviceSession,
    listener_ip: str,
    listener_port: int,
    session_flag: str,
    channel: int,
    stream_type: int,
    result_queue: queue.Queue[BaseException | CasInviteStreamResponse],
) -> None:
    try:
        result_queue.put(
            cas.invite_direct_reverse_stream(
                session_flag=session_flag,
                serial=serial,
                listener_ip=listener_ip,
                listener_port=listener_port,
                device_session=device_session,
                channel=channel,
                stream_type=stream_type,
                transport_protocol=1,
                encrypt_stream=True,
            )
        )
    except BaseException as err:
        result_queue.put(err)


def _report_notify_result(
    notify_thread: Thread,
    notify_results: queue.Queue[BaseException | object],
    *,
    wait_seconds: float = 1.0,
) -> None:
    notify_thread.join(timeout=wait_seconds)
    if notify_results.empty():
        _report("CAS 反向直连通知仍未返回。")
        return
    notify_result = notify_results.get_nowait()
    if isinstance(notify_result, BaseException):
        _report(
            "CAS 通知失败或返回未能解析："
            f"{type(notify_result).__name__}: {notify_result}"
        )
    else:
        _report("CAS 反向直连通知已确认。")


def _wait_for_control_threads(threads: list[Thread]) -> bool:
    """Require every cloud control worker to finish before media lockdown."""
    deadline = time.monotonic() + CAS_SOCKET_TIMEOUT + 2.0
    for thread in threads:
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)
    return all(not thread.is_alive() for thread in threads)


def _take_invite_result(
    invite_thread: Thread,
    invite_results: queue.Queue[BaseException | CasInviteStreamResponse],
    *,
    wait_seconds: float,
) -> CasInviteStreamResponse | None:
    invite_thread.join(timeout=wait_seconds)
    if invite_results.empty():
        _report("CAS 开始预览请求仍未返回。")
        return None
    result = invite_results.get_nowait()
    if isinstance(result, BaseException):
        _report(
            "CAS 开始预览失败："
            f"{type(result).__name__}: {result}"
        )
        return None
    _report(
        "CAS 开始预览已确认，已取得独立流头："
        f"{len(result.stream_header)} 字节。"
    )
    return result


def _open_capture(path: Path | None) -> Any | None:
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(descriptor, "wb")


def _handle_callback_connection(
    connection: socket.socket,
    peer: tuple[str, int],
    *,
    cas: EzvizCAS,
    serial: str,
    camera_ip: str | None,
    strict_camera_ip: bool,
    hold_seconds: float,
) -> None:
    """Authenticate and acknowledge the camera's direct-reverse callback."""
    with connection:
        connection.settimeout(CAS_SOCKET_TIMEOUT)
        _report(f"收到局域网 TCP 回连：{peer[0]}:{peer[1]}")
        if camera_ip and peer[0] != camera_ip:
            if strict_camera_ip:
                raise RuntimeError("回连来源不是预期摄像头地址")
            _report("警告：回连来源不是预期摄像头地址。")

        raw_check = _recv_native_frame(connection)
        check = _parse_native_frame(raw_check, key=b"")
        if check.header.command != CAS_COMMAND_DIRECT_REVERSE_CHECK_REQUEST:
            raise RuntimeError(
                f"回连首包命令异常：{check.header.command:#x}"
            )
        if check.header.flags != 0:
            raise RuntimeError("回连首包意外启用了加密")
        suffix = _subserial_suffix(check.plaintext)
        if suffix != serial[-6:]:
            raise RuntimeError("回连首包的设备标识与目标不一致")

        response = _build_native_plain_frame(
            _callback_response_xml(),
            command=CAS_COMMAND_DIRECT_REVERSE_CHECK_RESPONSE,
            sequence=cas.next_native_sequence(),
        )
        connection.sendall(response)
        _report("摄像头回连身份校验已应答。")

        deadline = time.monotonic() + max(hold_seconds, 0)
        extra_bytes = 0
        connection.settimeout(0.5)
        while time.monotonic() < deadline:
            try:
                chunk = connection.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                break
            extra_bytes += len(chunk)
            if chunk.startswith(CAS_FRAME_MAGIC):
                _report("回连通道收到后续协议帧。")
        _report(f"回连通道观察完成，后续收到 {extra_bytes} 字节。")


def main() -> int:
    global _REPORT_FILE
    args = _arguments()
    if args.output_format != "none":
        _REPORT_FILE = sys.stderr
    if not 0 <= args.port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if args.callback_attempts < 1:
        raise ValueError("callback-attempts must be at least 1")
    token = _load_token(args.token_file)
    listener_ip = args.listen_ip or _local_ip_for(args.camera_ip)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((listener_ip, args.port))
    listener.listen(4)
    listener.settimeout(args.timeout)
    listener_port = int(listener.getsockname()[1])
    _report(f"本地反向直连监听已就绪：{listener_ip}:{listener_port}")

    api_client = EzvizClient(url=str(token["api_url"]), token=token)
    try:
        device = api_client.get_device_infos(args.serial).get("deviceInfos", {})
        if not args.skip_awake_control:
            try:
                # Official App mapping recovered from
                # BasePlayerItemPresenter.delayBatterySleep: type 1 is live
                # preview (type 4 is talk). This is a control request only; it
                # does not open or relay a cloud media stream.
                api_client.delay_battery_device_sleep(
                    args.serial,
                    args.channel,
                    1,
                    max_retries=0,
                )
                _report("电池摄像头的实时预览保活请求已确认。")
            except BaseException:
                _report("实时预览保活暂未确认，将继续用 CAS 反向唤醒。")
    finally:
        api_client.close_session()
    cas_host = device.get("casIp") if isinstance(device, dict) else None
    cas_port = device.get("casPort") if isinstance(device, dict) else None
    if not isinstance(cas_host, str) or not isinstance(cas_port, int):
        listener.close()
        _report(
            "设备元数据没有返回可用的 CAS 地址；萤石会话可能已过期，"
            "请重新登录后重试。"
        )
        return 8

    cas = EzvizCAS(
        token,
        cas_host=cas_host,
        cas_port=cas_port,
        cas_certificate_sha256=LEGACY_YS7_CAS_CERT_SHA256,
    )
    device_session = CasDeviceSession.from_response(cas.cas_get_encryption(args.serial))
    callback_ready = False
    control_threads: list[Thread] = []
    for attempt in range(1, args.callback_attempts + 1):
        notify_results: queue.Queue[BaseException | object] = queue.Queue(maxsize=1)
        notify_thread = Thread(
            target=_notify_worker,
            args=(
                cas,
                args.serial,
                device_session,
                listener_ip,
                listener_port,
                notify_results,
            ),
            daemon=True,
        )
        control_threads.append(notify_thread)
        notify_thread.start()

        try:
            connection, peer = listener.accept()
        except TimeoutError:
            _report(
                "等待摄像头局域网回连超时"
                f"（第 {attempt}/{args.callback_attempts} 次）。"
            )
            _report_notify_result(notify_thread, notify_results, wait_seconds=1.0)
            continue

        try:
            _handle_callback_connection(
                connection,
                peer,
                cas=cas,
                serial=args.serial,
                camera_ip=args.camera_ip,
                strict_camera_ip=args.strict_camera_ip,
                hold_seconds=args.hold_seconds,
            )
        except (RuntimeError, ValueError) as err:
            _report(f"摄像头回连校验失败：{err}。")
            listener.close()
            return 3
        _report_notify_result(notify_thread, notify_results)
        callback_ready = True
        break

    if not callback_ready:
        listener.close()
        return 2
    if not _wait_for_control_threads(control_threads):
        listener.close()
        _report("CAS 控制线程未能安全结束，已拒绝继续建立媒体流。")
        return 7

    session_flag = (
        f"ClientReverse-1-{args.serial}-{args.channel}-{args.stream_type}"
    )
    invite_results: queue.Queue[BaseException | CasInviteStreamResponse] = queue.Queue(
        maxsize=1
    )
    invite_thread = Thread(
        target=_invite_worker,
        args=(
            cas,
            args.serial,
            device_session,
            listener_ip,
            listener_port,
            session_flag,
            args.channel,
            args.stream_type,
            invite_results,
        ),
        daemon=True,
    )
    invite_thread.start()
    _report("已下发局域网开始预览请求，等待独立媒体回连……")

    listener.settimeout(args.timeout)
    try:
        media_connection, media_peer = listener.accept()
    except TimeoutError:
        _report("等待摄像头媒体回连超时。")
        _take_invite_result(
            invite_thread,
            invite_results,
            wait_seconds=2.0,
        )
        listener.close()
        return 4
    finally:
        listener.close()

    invite_result = _take_invite_result(
        invite_thread,
        invite_results,
        wait_seconds=5.0,
    )
    invite_thread.join(timeout=1.0)
    if invite_thread.is_alive():
        media_connection.close()
        _report("CAS 开始预览线程未能安全结束。")
        return 7
    _report(f"收到第二条局域网 TCP 回连：{media_peer[0]}:{media_peer[1]}")
    if args.camera_ip and media_peer[0] != args.camera_ip:
        if args.strict_camera_ip:
            media_connection.close()
            _report("媒体回连来源不是预期摄像头地址。")
            return 3
        _report("警告：媒体回连来源不是预期摄像头地址。")

    if invite_result is not None and args.capture_file is not None:
        header_capture = _open_capture(
            args.capture_file.with_name(args.capture_file.name + ".header")
        )
        assert header_capture is not None
        try:
            header_capture.write(invite_result.stream_header)
        finally:
            header_capture.close()

    if args.deny_internet_after_connect:
        if invite_result is None:
            media_connection.close()
            _report("CAS 开始预览未确认，不能安全进入进程级断网阶段。")
            return 4
        try:
            enforcement = deny_new_outbound_network(media_connection.fileno())
        except NetworkLockError as err:
            media_connection.close()
            _report(f"进程级断网启用失败：{err}。")
            return 7
        _report(
            f"进程级断网已启用（{enforcement}）：禁止新建外网连接，"
            "仅保留现有摄像头局域网媒体连接。"
        )

    sample = bytearray()
    total_bytes = 0
    first_data_at: float | None = None
    last_data_at: float | None = None
    capture = _open_capture(args.capture_file)
    deframer = EzvizDirectMediaDeframer(expected_session_flag=session_flag)
    output: EzvizMediaOutput | None = None
    output_status = 0
    output_bytes = 0
    output_broken = False
    try:
        with media_connection:
            media_connection.settimeout(0.5)
            deadline = (
                None
                if args.media_seconds <= 0
                else time.monotonic() + args.media_seconds
            )
            while deadline is None or time.monotonic() < deadline:
                try:
                    chunk = media_connection.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                now = time.monotonic()
                first_data_at = first_data_at or now
                last_data_at = now
                total_bytes += len(chunk)
                if len(sample) < 1024 * 1024:
                    sample.extend(chunk[: 1024 * 1024 - len(sample)])
                if capture is not None:
                    capture.write(chunk)
                try:
                    fragments = deframer.feed(chunk)
                    if fragments and args.output_format != "none":
                        if output is None:
                            output = EzvizMediaOutput(
                                args.output_format,
                                ffmpeg_bin=args.ffmpeg_bin,
                            )
                        output_bytes += output.write(fragments)
                except BrokenPipeError:
                    output_broken = True
                    break
    finally:
        if capture is not None:
            capture.close()
        if not output_broken:
            deframer.finish()
        if output is not None:
            try:
                output_status = output.close()
            except (BrokenPipeError, subprocess.TimeoutExpired):
                output_status = 0 if output_broken else 1
    if output_status != 0 and not output_broken:
        _report(f"FFmpeg 无损转封装失败，退出码 {output_status}。")
        return 6

    duration = (
        max(0.0, last_data_at - first_data_at)
        if first_data_at is not None and last_data_at is not None
        else 0.0
    )
    native_header = (
        len(sample) >= 4
        and sample[0] == 0x24
        and int.from_bytes(sample[2:4], "big") == 0x40
    )
    h264_markers = sample.count(b"\x00\x00\x00\x01\x67") + sample.count(
        b"\x00\x00\x01\x67"
    )
    h265_markers = sample.count(b"\x00\x00\x00\x01\x40") + sample.count(
        b"\x00\x00\x01\x40"
    )
    _report(
        "局域网媒体观察结果："
        f"{total_bytes} 字节，持续 {duration:.1f} 秒，"
        f"私有流头={'是' if native_header else '否'}，"
        f"H.264/H.265 参数集标记={h264_markers}/{h265_markers}，"
        f"标准 MPEG-PS={output_bytes} 字节。"
    )
    if total_bytes == 0:
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
