from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
MANAGE = PROJECT_DIR / "deploy" / "linux" / "manage.sh"
COMPOSE = PROJECT_DIR / "deploy" / "linux" / "compose.yaml"
ENTRYPOINT = PROJECT_DIR / "deploy" / "linux" / "entrypoint.sh"


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

    assert "migrate:" in compose
    assert 'command: ["migrate-legacy"]' in compose
    assert 'network_mode: "none"' in compose
    assert "condition: service_completed_successfully" in compose
    assert "- ezviz-data:/data" in compose
    assert "- ./data:/legacy-data:ro" in compose
    assert "- DAC_OVERRIDE" in compose


def test_private_state_import_uses_a_restricted_root_helper() -> None:
    compose = COMPOSE.read_text()
    importer = compose.split("  state-import:\n", 1)[1].split("\n  bridge:\n", 1)[0]
    manage = MANAGE.read_text()
    entrypoint = ENTRYPOINT.read_text()

    assert 'profiles: ["tools"]' in importer
    assert 'command: ["import-state"]' in importer
    assert 'network_mode: "none"' in importer
    assert 'user: "0:0"' in importer
    assert "- ezviz-data:/data" in importer
    assert "read_only: true" in importer
    assert "cap_drop:\n      - ALL" in importer
    assert "- CHOWN" in importer
    assert "- DAC_OVERRIDE" in importer
    assert "- FOWNER" in importer
    assert "no-new-privileges:true" in importer
    assert 'EZVIZ_RUNTIME_UID: "${PUID:-1000}"' in importer
    assert 'EZVIZ_RUNTIME_GID: "${PGID:-1000}"' in importer
    assert "state-import import-state" in manage
    assert '--uid "${EZVIZ_RUNTIME_UID:-1000}"' in entrypoint
    assert '--gid "${EZVIZ_RUNTIME_GID:-1000}"' in entrypoint
