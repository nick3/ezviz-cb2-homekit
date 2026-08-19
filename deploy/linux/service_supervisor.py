#!/usr/bin/env python3
"""Keep the setup wizard available and run the bridge once it is configured."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import config_tool
from ezviz_discovery import interface_ipv4_addresses
from runtime_settings import (
    AUTH_STATE_FILE_NAME,
    SettingsError,
    SettingsStore,
    bridge_environment,
    settings_complete,
    token_is_ready,
    token_matches_serial,
)
from setup_wizard import DEFAULT_SETUP_HOST, WizardApplication, create_server
from tls_config import TLSConfigError, ensure_tls_certificate

RESTART_DELAY_SECONDS = 5.0


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _runtime_signature(settings_path: Path, token_file: Path) -> tuple[int | None, ...]:
    """Track only wizard-owned state, not HomeKit's pairing writes."""

    return (
        _mtime_ns(settings_path),
        _mtime_ns(token_file),
        _mtime_ns(token_file.with_name(AUTH_STATE_FILE_NAME)),
    )


def bridge_command(
    settings: Mapping[str, Any],
    *,
    project_dir: Path,
    data_dir: Path,
    config_file: Path,
) -> list[str]:
    return [
        "/usr/local/bin/python3",
        str(project_dir / "scripts" / "ezviz_warm_controller.py"),
        "--serial",
        str(settings["serial"]),
        "--token-file",
        str(data_dir / "ezviz_token.json"),
        "--activity-file",
        str(data_dir / "ezviz-stream-active.json"),
        "--ffmpeg-bin",
        "/usr/bin/ffmpeg",
        "--warm-seconds",
        str(settings["warm_seconds"]),
        "--power-mode",
        str(settings["power_mode"]),
        "--homekit-transcode",
        str(settings["homekit_transcode"]),
        "--power-refresh-seconds",
        str(settings["power_refresh_seconds"]),
        "--pir-poll-seconds",
        str(settings["pir_poll_seconds"]),
        "--pir-preheat",
        str(settings["pir_preheat"]),
        "--",
        "/usr/local/bin/go2rtc",
        "-c",
        str(config_file),
    ]


class BridgeSupervisor:
    def __init__(
        self,
        *,
        settings_store: SettingsStore,
        token_file: Path,
        config_file: Path,
        project_dir: Path,
        data_dir: Path,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        startup_error: str = "",
        startup_recovery: Callable[[], str] | None = None,
    ) -> None:
        self.settings_store = settings_store
        self.token_file = token_file
        self.config_file = config_file
        self.project_dir = project_dir
        self.data_dir = data_dir
        self.popen = popen
        self._startup_error = startup_error
        self._startup_recovery = startup_recovery
        self.reload_event = threading.Event()
        self.stop_event = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._status: dict[str, Any] = (
            {"state": "error", "message": startup_error}
            if startup_error
            else {"state": "waiting", "message": "等待完成 Web 配置"}
        )
        self._status_lock = threading.Lock()

    def _set_status(self, state: str, message: str, **extra: Any) -> None:
        with self._status_lock:
            self._status = {"state": state, "message": message, **extra}
        print(f"[桥接服务] {message}", flush=True)

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def request_reload(self) -> None:
        self.reload_event.set()

    def request_stop(self) -> None:
        self.stop_event.set()
        self.reload_event.set()

    def _stop_child(self) -> bool:
        process = self._process
        if process is None:
            return True
        if process.poll() is not None:
            self._process = None
            return True
        process.terminate()
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._set_status(
                    "error",
                    "桥接子进程停止超时；为避免端口冲突，不会启动新实例",
                    pid=process.pid,
                )
                return False
        self._process = None
        return True

    def _retry_startup_initialization(self) -> None:
        if not self._startup_error or self._startup_recovery is None:
            return
        self._startup_error = self._startup_recovery()
        if self._startup_error:
            self._set_status("error", self._startup_error)
        else:
            self._set_status("waiting", "持久化配置初始化已恢复")

    def _ready(self) -> tuple[dict[str, Any], bool, str]:
        if self._startup_error:
            return {}, False, self._startup_error
        settings = self.settings_store.load()
        if not settings_complete(settings):
            return settings, False, "等待填写完整摄像头配置"
        if not token_matches_serial(self.token_file, str(settings["serial"])):
            if token_is_ready(self.token_file):
                return (
                    settings,
                    False,
                    "萤石会话尚未绑定当前摄像头，请在 Web 向导中重新登录",
                )
            return settings, False, "等待在 Web 向导中登录萤石"
        return settings, True, "配置已就绪"

    def _start_child(self, settings: Mapping[str, Any]) -> None:
        activity_file = self.data_dir / "ezviz-stream-active.json"
        activity_file.unlink(missing_ok=True)
        environment = bridge_environment(settings, data_dir=self.data_dir)
        command = bridge_command(
            settings,
            project_dir=self.project_dir,
            data_dir=self.data_dir,
            config_file=self.config_file,
        )
        self._set_status("starting", "正在启动 go2rtc 与摄像头保温控制器")
        self._process = self.popen(command, env=environment)
        self._set_status("running", "桥接服务正在运行", pid=self._process.pid)

    def run(self) -> None:
        retry_at = 0.0
        last_signature: tuple[int | None, ...] | None = None
        try:
            while not self.stop_event.is_set():
                signature = _runtime_signature(
                    self.settings_store.path,
                    self.token_file,
                )
                if last_signature is not None and signature != last_signature:
                    self.reload_event.set()
                last_signature = signature

                if self.reload_event.is_set():
                    self.reload_event.clear()
                    if self._process is not None:
                        self._set_status("restarting", "配置发生变化，正在重启桥接服务")
                        if not self._stop_child():
                            self.reload_event.wait(1)
                            continue
                    self._retry_startup_initialization()
                    retry_at = 0.0

                try:
                    settings, ready, message = self._ready()
                except (OSError, SettingsError) as error:
                    if not self._stop_child():
                        self.reload_event.wait(1)
                        continue
                    self._set_status("error", f"读取持久化配置失败：{error}")
                    self.stop_event.wait(2)
                    continue

                if not ready:
                    if not self._stop_child():
                        self.reload_event.wait(1)
                        continue
                    if self.status().get("message") != message:
                        state = "error" if self._startup_error else "waiting"
                        self._set_status(state, message)
                    self.reload_event.wait(1)
                    continue

                if self._process is None and time.monotonic() >= retry_at:
                    try:
                        self._start_child(settings)
                    except (OSError, SettingsError) as error:
                        self._set_status("error", f"桥接服务启动失败：{error}")
                        retry_at = time.monotonic() + RESTART_DELAY_SECONDS

                process = self._process
                if process is not None:
                    returncode = process.poll()
                    if returncode is not None:
                        self._process = None
                        self._set_status(
                            "error",
                            f"桥接服务已退出（状态 {returncode}），稍后自动重试",
                            returncode=returncode,
                        )
                        retry_at = time.monotonic() + RESTART_DELAY_SECONDS
                self.reload_event.wait(0.5)
        finally:
            self._set_status("stopping", "正在停止桥接服务")
            self._stop_child()


def _positive_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise RuntimeError("EZVIZ_SETUP_PORT 必须是整数") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("EZVIZ_SETUP_PORT 必须在 1 到 65535 之间")
    return port


def _initialize_homekit_config(config_file: Path, template: Path) -> None:
    if not config_file.exists():
        config_tool.init_config(template, config_file)
        print("[桥接服务] 已生成新的 HomeKit 身份。", flush=True)
    config_tool.upgrade_config(template, config_file)
    config_file.chmod(0o600)


def _initialize_persistent_state(
    settings_store: SettingsStore,
    config_file: Path,
    template: Path,
) -> str:
    """Initialize recoverable state and return a user-facing startup error."""

    errors: list[str] = []
    try:
        if settings_store.bootstrap_from_environment():
            print("[桥接服务] 已将旧版环境变量迁移到 Web 配置。", flush=True)
    except (OSError, SettingsError) as error:
        errors.append(f"旧版环境变量迁移失败：{error}")
    try:
        _initialize_homekit_config(config_file, template)
    except (OSError, RuntimeError) as error:
        errors.append(f"HomeKit 配置初始化失败：{error}")
    message = "；".join(errors)
    if message:
        print(f"[桥接服务] {message}", file=sys.stderr, flush=True)
    return message


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent.parent
    data_dir = Path(os.environ.get("EZVIZ_DATA_DIR", "/data"))
    config_file = Path(
        os.environ.get("GO2RTC_CONFIG_FILE", str(data_dir / "go2rtc.yaml"))
    )
    token_file = Path(
        os.environ.get("EZVIZ_TOKEN_FILE", str(data_dir / "ezviz_token.json"))
    )
    template = script_dir / "go2rtc.yaml.tmpl"
    html_file = script_dir / "wizard.html"
    legacy_data_value = os.environ.get("EZVIZ_LEGACY_DATA_DIR", "").strip()
    legacy_data_dir = Path(legacy_data_value) if legacy_data_value else None
    legacy_serial = os.environ.get("EZVIZ_SERIAL", "")
    host = os.environ.get("EZVIZ_SETUP_HOST", DEFAULT_SETUP_HOST).strip()
    host = host or DEFAULT_SETUP_HOST
    port = _positive_port(os.environ.get("EZVIZ_SETUP_PORT", "8099"))

    settings_store = SettingsStore(data_dir)
    try:
        settings_store.prepare()
    except OSError as error:
        print(f"[桥接服务] 无法启动：{error}", file=sys.stderr, flush=True)
        return 1
    if legacy_data_dir is not None:
        try:
            if config_tool.migrate_legacy_bind_state(
                legacy_data_dir,
                settings_store.data_dir,
                legacy_serial,
            ):
                print(
                    "[桥接服务] 已从旧版 ./data 目录迁移 HomeKit 身份和萤石会话。",
                    flush=True,
                )
        except (OSError, ValueError, RuntimeError) as error:
            print(
                f"[桥接服务] 旧版绑定目录迁移失败：{error}",
                file=sys.stderr,
                flush=True,
            )
            return 1
    startup_error = _initialize_persistent_state(settings_store, config_file, template)
    addresses = interface_ipv4_addresses()
    try:
        tls = ensure_tls_certificate(data_dir, host=host, addresses=addresses)
    except (OSError, TLSConfigError) as error:
        print(
            f"[Web 向导] 无法安全启动 HTTPS 服务：{error}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    supervisor = BridgeSupervisor(
        settings_store=settings_store,
        token_file=token_file,
        config_file=config_file,
        project_dir=project_dir,
        data_dir=data_dir,
        startup_error=startup_error,
        startup_recovery=lambda: _initialize_persistent_state(
            settings_store, config_file, template
        ),
    )
    application = WizardApplication(
        settings_store=settings_store,
        token_file=token_file,
        config_file=config_file,
        html_file=html_file,
        reload_callback=supervisor.request_reload,
        bridge_status=supervisor.status,
    )
    try:
        server = create_server(
            application,
            host,
            port,
            certificate=tls.certificate,
            private_key=tls.private_key,
        )
    except OSError as error:
        print(
            f"[Web 向导] 无法启动 HTTPS 服务：{error}",
            file=sys.stderr,
            flush=True,
        )
        application.close()
        return 1
    server_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="ezviz-setup-web",
        daemon=True,
    )

    def handle_signal(_signum: int, _frame: object) -> None:
        supervisor.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    server_thread.start()
    created = "（首次生成）" if tls.created else ""
    print(
        f"[Web 向导] TLS 证书 SHA-256{created}：{tls.fingerprint}",
        flush=True,
    )
    display_addresses = addresses if host in {DEFAULT_SETUP_HOST, "::"} else [host]
    if display_addresses:
        for address in display_addresses:
            url_host = f"[{address}]" if ":" in address else address
            print(f"[Web 向导] https://{url_host}:{port}", flush=True)
    else:
        print(f"[Web 向导] HTTPS 已在端口 {port} 启动。", flush=True)

    try:
        supervisor.run()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)
        application.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
