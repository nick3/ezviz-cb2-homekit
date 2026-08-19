from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
MANAGE = PROJECT_DIR / "deploy" / "linux" / "manage.sh"


def _run_login(tmp_path: Path, *, shell_port: str = "") -> str:
    script = tmp_path / "manage.sh"
    shutil.copy2(MANAGE, script)
    (tmp_path / ".env").write_text('EZVIZ_SETUP_PORT="9123" # test override\n')
    environment = dict(os.environ)
    environment.pop("EZVIZ_SETUP_PORT", None)
    if shell_port:
        environment["EZVIZ_SETUP_PORT"] = shell_port
    result = subprocess.run(
        ["sh", str(script), "login"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout


def test_login_url_reads_setup_port_from_dotenv(tmp_path: Path) -> None:
    assert "<Linux-IP>:9123" in _run_login(tmp_path)


def test_shell_setup_port_takes_precedence_over_dotenv(tmp_path: Path) -> None:
    assert "<Linux-IP>:9234" in _run_login(tmp_path, shell_port="9234")
