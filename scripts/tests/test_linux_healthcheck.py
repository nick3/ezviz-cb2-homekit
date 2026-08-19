from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
HEALTHCHECK = PROJECT_DIR / "deploy" / "linux" / "healthcheck.py"


def test_invalid_setup_port_exits_cleanly_without_a_traceback() -> None:
    environment = dict(os.environ)
    environment["EZVIZ_SETUP_PORT"] = "not-a-port"
    result = subprocess.run(
        [sys.executable, str(HEALTHCHECK)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
