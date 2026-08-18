"""Streaming helpers for the EZVIZ reverse-direct TCP media connection.

The camera wraps every callback message in a four-byte header::

    '$' | message type | payload length (big endian)

Type 1 carries the private session identifier. Type 2 carries an additional
four-byte transport prefix followed by a fragment of a standard MPEG-PS byte
stream. Fragment boundaries do not necessarily align with MPEG pack or PES
boundaries, so callers must concatenate the returned payloads in order.
"""

from __future__ import annotations

from collections.abc import Iterable
import subprocess
import sys
from typing import BinaryIO


PRIVATE_FRAME_MAGIC = 0x24
PRIVATE_FRAME_HEADER_SIZE = 4
PRIVATE_SESSION_TYPE = 1
PRIVATE_MEDIA_TYPE = 2
PRIVATE_MEDIA_PREFIX_SIZE = 4


class EzvizDirectMediaDeframer:
    """Incrementally remove the two private callback framing layers."""

    def __init__(self, *, expected_session_flag: str | None = None) -> None:
        self._buffer = bytearray()
        self._expected_session_flag = (
            expected_session_flag.encode("ascii")
            if expected_session_flag is not None
            else None
        )
        self.session_verified = False
        self.private_frames = 0
        self.media_frames = 0

    @property
    def pending_bytes(self) -> int:
        """Return bytes retained for the next incomplete private frame."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[bytes]:
        """Consume arbitrary TCP bytes and return ordered MPEG-PS fragments."""
        if data:
            self._buffer.extend(data)

        media: list[bytes] = []
        while len(self._buffer) >= PRIVATE_FRAME_HEADER_SIZE:
            if self._buffer[0] != PRIVATE_FRAME_MAGIC:
                raise ValueError("EZVIZ reverse media lost private frame alignment")

            message_type = self._buffer[1]
            payload_size = int.from_bytes(self._buffer[2:4], "big")
            frame_size = PRIVATE_FRAME_HEADER_SIZE + payload_size
            if len(self._buffer) < frame_size:
                break

            payload = bytes(self._buffer[4:frame_size])
            del self._buffer[:frame_size]
            self.private_frames += 1

            if message_type == PRIVATE_SESSION_TYPE:
                self._verify_session(payload)
                continue
            if message_type != PRIVATE_MEDIA_TYPE:
                raise ValueError(
                    f"Unsupported EZVIZ reverse media message type: {message_type}"
                )
            if len(payload) < PRIVATE_MEDIA_PREFIX_SIZE:
                raise ValueError("EZVIZ reverse media payload is missing its prefix")

            self.media_frames += 1
            fragment = payload[PRIVATE_MEDIA_PREFIX_SIZE:]
            if fragment:
                media.append(fragment)

        return media

    def finish(self) -> None:
        """Reject a connection that ended halfway through a private frame."""
        if self._buffer:
            raise ValueError(
                "EZVIZ reverse media connection ended with "
                f"{len(self._buffer)} incomplete bytes"
            )

    def _verify_session(self, payload: bytes) -> None:
        value = payload.rstrip(b"\0")
        if self._expected_session_flag is not None:
            # The final number is an SDK-internal stream enum. Some CB2
            # firmware reports MAIN (1) here even when the invite requested
            # NewStreamType=2, while the preceding client/session/device/channel
            # fields remain stable. Validate those identity-bearing fields and
            # require a numeric wire enum instead of rejecting valid media.
            expected_prefix, separator, _ = self._expected_session_flag.rpartition(
                b"-"
            )
            value_prefix, value_separator, value_stream = value.rpartition(b"-")
            alternate_wire_enum = (
                separator == b"-"
                and value_separator == b"-"
                and value_prefix == expected_prefix
                and value_stream.isdigit()
            )
            if value != self._expected_session_flag and not alternate_wire_enum:
                raise ValueError("EZVIZ reverse media session identifier mismatch")
        self.session_verified = True


class EzvizMediaOutput:
    """Write deframed MPEG-PS directly or remux it losslessly to MPEG-TS."""

    def __init__(
        self,
        output_format: str,
        *,
        ffmpeg_bin: str,
        output: BinaryIO | None = None,
    ) -> None:
        if output_format not in {"mpegps", "mpegts"}:
            raise ValueError(f"Unsupported media output format: {output_format}")
        self.output_format = output_format
        self.output = output or sys.stdout.buffer
        self._process: subprocess.Popen[bytes] | None = None
        self._input: BinaryIO = self.output

        if output_format == "mpegts":
            process = subprocess.Popen(
                [
                    ffmpeg_bin,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-fflags",
                    "nobuffer",
                    "-probesize",
                    "2M",
                    "-analyzeduration",
                    "3M",
                    "-f",
                    "mpeg",
                    "-i",
                    "pipe:0",
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-c",
                    "copy",
                    "-mpegts_flags",
                    "+resend_headers",
                    "-f",
                    "mpegts",
                    "pipe:1",
                ],
                stdin=subprocess.PIPE,
                stdout=self.output,
                stderr=sys.stderr,
                bufsize=0,
            )
            if process.stdin is None:
                process.kill()
                raise RuntimeError("Could not open FFmpeg input pipe")
            self._process = process
            self._input = process.stdin

    def write(self, fragments: Iterable[bytes]) -> int:
        """Write fragments and return their total MPEG-PS byte count."""
        total = 0
        for fragment in fragments:
            self._input.write(fragment)
            total += len(fragment)
        return total

    def close(self) -> int:
        """Finish output and return FFmpeg's exit status, if one was used."""
        if self._process is None:
            self.output.flush()
            return 0
        try:
            self._input.close()
        except BrokenPipeError:
            pass
        return self._process.wait(timeout=10)
