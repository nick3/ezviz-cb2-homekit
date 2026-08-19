from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
MANAGE = PROJECT_DIR / "deploy" / "linux" / "manage.sh"
COMPOSE = PROJECT_DIR / "deploy" / "linux" / "compose.yaml"


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
    output = _run_login(tmp_path)
    assert "https://<Linux-IP>:9123" in output
    assert "TLS 证书 SHA-256" in output


def test_shell_setup_port_takes_precedence_over_dotenv(tmp_path: Path) -> None:
    assert "<Linux-IP>:9234" in _run_login(tmp_path, shell_port="9234")


def test_compose_preserves_the_old_bind_mount_for_automatic_migration() -> None:
    compose = COMPOSE.read_text()

    assert "EZVIZ_LEGACY_DATA_DIR: /legacy-data" in compose
    assert "- ezviz-data:/data" in compose
    assert "- ./data:/legacy-data:ro" in compose
