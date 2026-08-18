from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from ezviz_direct_media import EzvizDirectMediaDeframer, EzvizMediaOutput


def _private_frame(message_type: int, payload: bytes) -> bytes:
    return b"$" + bytes([message_type]) + len(payload).to_bytes(2, "big") + payload


def test_deframer_handles_arbitrary_tcp_boundaries() -> None:
    session_flag = "ClientReverse-1-CAM123456-1-2"
    raw = b"".join(
        [
            _private_frame(1, session_flag.encode().ljust(64, b"\0")),
            _private_frame(2, b"\x00\x00\x11\x80\x00\x00\x01\xba-video"),
            _private_frame(2, b"\x00\x01\x12\x0d\x00\x00\x01\xc0-audio"),
        ]
    )
    deframer = EzvizDirectMediaDeframer(expected_session_flag=session_flag)

    fragments: list[bytes] = []
    for offset in range(0, len(raw), 7):
        fragments.extend(deframer.feed(raw[offset : offset + 7]))
    deframer.finish()

    assert b"".join(fragments) == (
        b"\x00\x00\x01\xba-video\x00\x00\x01\xc0-audio"
    )
    assert deframer.session_verified is True
    assert deframer.private_frames == 3
    assert deframer.media_frames == 2
    assert deframer.pending_bytes == 0


def test_deframer_rejects_wrong_session() -> None:
    deframer = EzvizDirectMediaDeframer(expected_session_flag="expected")

    with pytest.raises(ValueError, match="session identifier mismatch"):
        deframer.feed(_private_frame(1, b"unexpected"))


def test_deframer_accepts_firmware_wire_stream_enum() -> None:
    expected = "ClientReverse-1-CAM123456-1-2"
    deframer = EzvizDirectMediaDeframer(expected_session_flag=expected)

    assert deframer.feed(
        _private_frame(1, b"ClientReverse-1-CAM123456-1-1".ljust(64, b"\0"))
    ) == []
    assert deframer.session_verified is True


def test_deframer_rejects_truncated_tail() -> None:
    deframer = EzvizDirectMediaDeframer()
    deframer.feed(b"$\x02\x00\x08partial")

    with pytest.raises(ValueError, match="incomplete bytes"):
        deframer.finish()


def test_mpegps_output_is_byte_exact() -> None:
    output = BytesIO()
    sink = EzvizMediaOutput(
        "mpegps",
        ffmpeg_bin="unused",
        output=output,
    )

    assert sink.write([b"abc", b"def"]) == 6
    assert sink.close() == 0
    assert output.getvalue() == b"abcdef"
