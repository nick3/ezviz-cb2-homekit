from __future__ import annotations

import argparse
import importlib.util
import json
import stat
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_DIR / "scripts" / "login-ezviz-cloud.py"
SPEC = importlib.util.spec_from_file_location("login_ezviz_cloud", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
login_ezviz_cloud = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(login_ezviz_cloud)


class FakeClient:
    def __init__(self, account: str, password: str, region: str) -> None:
        self.account = account
        self.password = password
        self.region = region
        self.closed = False

    def login(self, sms_code: int | None = None) -> None:
        assert sms_code is None

    def get_device_infos(self, serial: str) -> dict[str, object]:
        assert serial == "TESTCB2123456"
        return {"deviceSerial": serial}

    def export_token(self) -> dict[str, object]:
        return {"session_id": "private", "api_url": "https://api.ys7.com"}

    def close_session(self) -> None:
        self.closed = True


def test_cli_login_writes_a_bound_private_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "ezviz_token.json"
    client: FakeClient | None = None

    def client_factory(account: str, password: str, region: str) -> FakeClient:
        nonlocal client
        client = FakeClient(account, password, region)
        return client

    answers = iter(("owner", "secret"))
    monkeypatch.setattr(
        login_ezviz_cloud,
        "arguments",
        lambda: argparse.Namespace(
            serial="TESTCB2123456",
            token_file=token_file,
            region="api.ys7.com",
        ),
    )
    monkeypatch.setattr(
        login_ezviz_cloud, "prompt_nonempty", lambda *_a, **_k: next(answers)
    )
    monkeypatch.setattr(login_ezviz_cloud, "EzvizClient", client_factory)

    assert login_ezviz_cloud.main() == 0

    assert json.loads(token_file.read_text())["session_id"] == "private"
    assert json.loads(token_file.with_name("ezviz_auth.json").read_text()) == {
        "serial": "TESTCB2123456",
        "region": "api.ys7.com",
    }
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.with_name("ezviz_auth.json").stat().st_mode) == 0o600
    assert client is not None and client.closed is True
