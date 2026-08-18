#!/usr/bin/env python3
"""Perform an explicit, wake-on-demand HomeKit media verification."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def main() -> int:
    command = [
        "/usr/bin/ffprobe",
        "-v",
        "error",
        "-rw_timeout",
        "60000000",
        "-rtsp_transport",
        "tcp",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,sample_rate,channels",
        "-of",
        "json",
        "rtsp://127.0.0.1:8554/ezviz?video=h264&audio=opus",
    ]
    timeout = float(os.environ.get("EZVIZ_VERIFY_TIMEOUT", "75"))
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print("验证超时：摄像头没有在预期时间内完成唤醒和回连。", file=sys.stderr)
        return 1

    if result.returncode != 0:
        diagnostic = result.stderr.strip() or "ffprobe 未返回诊断"
        print(f"媒体验证失败：{diagnostic}", file=sys.stderr)
        return 1

    try:
        streams = json.loads(result.stdout).get("streams", [])
    except (json.JSONDecodeError, AttributeError) as error:
        print(f"媒体验证结果无法解析：{error}", file=sys.stderr)
        return 1

    video = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and stream.get("codec_name") == "h264"
        ),
        None,
    )
    audio = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "audio"
            and stream.get("codec_name") == "opus"
        ),
        None,
    )
    if video is None or audio is None:
        print("未同时收到 HomeKit 所需的 H.264 视频和 Opus 音频。", file=sys.stderr)
        return 1

    summary = {
        "video": {
            key: video.get(key)
            for key in ("codec_name", "width", "height")
        },
        "audio": {
            key: audio.get(key)
            for key in ("codec_name", "sample_rate", "channels")
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("CB2 局域网直连以及 HomeKit 视频/音频轨道验证成功。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
