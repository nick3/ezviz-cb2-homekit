#!/usr/bin/env python3
"""Small LAN-only Web configuration wizard for the container deployment."""

from __future__ import annotations

import ipaddress
import json
import secrets
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import config_tool
from ezviz_discovery import discover_ezviz_devices
from runtime_settings import (
    AUTH_STATE_FILE_NAME,
    SettingsError,
    SettingsStore,
    secure_write,
    settings_complete,
    token_matches_serial,
    usable_lan_ipv4,
)

MAX_REQUEST_BYTES = 64 * 1024
PENDING_LOGIN_SECONDS = 300
DEFAULT_SETUP_HOST = "0.0.0.0"  # noqa: S104 - HTTPS plus a LAN allowlist is intentional.
TRUSTED_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
)
TRUSTED_IPV6_NETWORK = ipaddress.ip_network("fc00::/7")


class WizardError(RuntimeError):
    """A safe error that can be shown in the Web UI."""


def _client_allowed(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_loopback or address.is_link_local:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in TRUSTED_IPV4_NETWORKS)
    return address in TRUSTED_IPV6_NETWORK


def _same_origin(origin: str | None, host: str | None) -> bool:
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc == host


def _default_client_dependencies() -> tuple[
    Callable[..., Any], type[BaseException], type[BaseException]
]:
    from pyezvizapi import EzvizClient
    from pyezvizapi.exceptions import EzvizAuthVerificationCode, PyEzvizError

    return EzvizClient, EzvizAuthVerificationCode, PyEzvizError


class LoginCoordinator:
    """Keep an MFA login in memory; never persist account credentials."""

    def __init__(
        self,
        token_file: Path,
        *,
        reload_callback: Callable[[], None],
        dependencies: tuple[
            Callable[..., Any], type[BaseException], type[BaseException]
        ]
        | None = None,
        discover: Callable[..., list[dict[str, object]]] = discover_ezviz_devices,
    ) -> None:
        self.token_file = token_file
        self.reload_callback = reload_callback
        self._dependencies_override = dependencies
        self.discover = discover
        self._pending: tuple[Any, dict[str, str], float] | None = None
        self._pending_timer: threading.Timer | None = None
        self._lock = threading.RLock()

    def _dependencies(
        self,
    ) -> tuple[Callable[..., Any], type[BaseException], type[BaseException]]:
        return self._dependencies_override or _default_client_dependencies()

    def _close_pending(self) -> None:
        timer = self._pending_timer
        self._pending_timer = None
        if timer is not None:
            timer.cancel()
        if self._pending is None:
            return
        client, _, _ = self._pending
        self._pending = None
        try:
            client.close_session()
        except BaseException:  # noqa: S110 - session cleanup is best effort
            pass

    def _expire_pending(self, client: Any, expires_at: float) -> None:
        with self._lock:
            if self._pending is None:
                return
            pending_client, _, pending_expiry = self._pending
            if pending_client is client and pending_expiry == expires_at:
                self._close_pending()

    def _schedule_pending_expiry(self, client: Any, expires_at: float) -> None:
        timer = threading.Timer(
            max(0.0, expires_at - time.monotonic()),
            self._expire_pending,
            args=(client, expires_at),
        )
        timer.daemon = True
        self._pending_timer = timer
        timer.start()

    def _save_token(self, client: Any, serial: str) -> None:
        token = client.export_token()
        if not isinstance(token, dict) or not token.get("session_id"):
            raise WizardError("萤石登录成功，但返回的会话令牌无效")
        auth_state = self.token_file.with_name(AUTH_STATE_FILE_NAME)
        secure_write(
            auth_state,
            b'{"state":"updating","serial":""}\n',
        )
        secure_write(
            self.token_file,
            json.dumps(token, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        )
        secure_write(
            auth_state,
            json.dumps({"serial": serial.upper()}, ensure_ascii=False, indent=2).encode(
                "utf-8"
            )
            + b"\n",
        )
        self.reload_callback()

    def _finish_configured(self, client: Any, serial: str) -> dict[str, Any]:
        device = client.get_device_infos(serial)
        if not device:
            raise WizardError(f"当前萤石账号中没有找到摄像头 {serial}")
        self._save_token(client, serial)
        return {"state": "authenticated", "message": "萤石账号验证成功"}

    def _finish_identify(self, client: Any, serial_hint: str) -> dict[str, Any]:
        devices = client.get_device_infos()
        if not isinstance(devices, dict):
            raise WizardError("萤石账号没有返回可识别的摄像头列表")
        hint = serial_hint.strip().upper()
        if len(hint) < 4 or not hint.replace("-", "").replace("_", "").isalnum():
            raise WizardError("请至少输入序列号末 4 位，以便匹配账号中的设备")
        matches = [
            (str(serial).upper(), device)
            for serial, device in devices.items()
            if str(serial).upper() == hint or str(serial).upper().endswith(hint)
        ]
        if not matches:
            raise WizardError("当前萤石账号中没有找到与序列号提示匹配的设备")
        if len(matches) > 1:
            raise WizardError("序列号提示匹配到多台设备，请再多输入几位")
        serial, device = matches[0]
        if not isinstance(device, dict):
            raise WizardError("萤石设备信息格式不正确")

        wifi = device.get("WIFI") if isinstance(device.get("WIFI"), dict) else {}
        connection = (
            device.get("CONNECTION")
            if isinstance(device.get("CONNECTION"), dict)
            else {}
        )
        camera_ip = usable_lan_ipv4(wifi.get("address")) or usable_lan_ipv4(
            connection.get("localIp")
        )
        source = "cloud_metadata" if camera_ip else ""
        if not camera_ip:
            try:
                client.delay_battery_device_sleep(serial, 1, 1, max_retries=0)
            except BaseException:  # noqa: S110 - wake-up is an optional hint
                pass
            try:
                local_devices = self.discover(timeout=4.0, serial_hint=serial)
            except (OSError, ValueError):
                local_devices = []
            local_match = next(
                (
                    item
                    for item in local_devices
                    if str(item.get("serial") or "").upper() == serial
                ),
                None,
            )
            if local_match is not None:
                camera_ip = usable_lan_ipv4(local_match.get("ip"))
                source = str(local_match.get("source") or "sadp")

        info = (
            device.get("deviceInfos")
            if isinstance(device.get("deviceInfos"), dict)
            else {}
        )
        self._save_token(client, serial)
        result: dict[str, Any] = {
            "state": "identified" if camera_ip else "identified_no_ip",
            "message": (
                "已从萤石设备信息中识别摄像头和局域网 IP"
                if source == "cloud_metadata"
                else "已识别摄像头，并通过局域网发现取得 IP"
                if camera_ip
                else "已识别完整序列号，但摄像头尚未回应局域网地址；请唤醒后重新搜索"
            ),
            "device": {
                "serial": serial,
                "ip": camera_ip,
                "model": str(
                    info.get("deviceType")
                    or info.get("deviceName")
                    or info.get("model")
                    or "EZVIZ 摄像头"
                ),
                "source": source,
                "matches_hint": True,
            },
        }
        return result

    def _complete(self, client: Any, context: Mapping[str, str]) -> dict[str, Any]:
        mode = context.get("mode")
        serial = context.get("serial", "")
        if mode == "identify":
            return self._finish_identify(client, serial)
        return self._finish_configured(client, serial)

    def begin(
        self,
        *,
        account: str,
        password: str,
        serial: str,
        region: str,
        mode: str = "configured",
    ) -> dict[str, Any]:
        account = account.strip()
        if not account or not password:
            raise WizardError("请输入萤石账号和密码")
        with self._lock:
            self._close_pending()
            client_factory, verification_error, api_error = self._dependencies()
            client = client_factory(account, password, region)
            try:
                client.login()
            except verification_error:
                expires_at = time.monotonic() + PENDING_LOGIN_SECONDS
                self._pending = (
                    client,
                    {"mode": mode, "serial": serial},
                    expires_at,
                )
                self._schedule_pending_expiry(client, expires_at)
                return {"state": "sms_required", "message": "请输入短信验证码"}
            except api_error as error:
                try:
                    client.close_session()
                finally:
                    raise WizardError(f"萤石登录失败：{error}") from error
            except BaseException:
                client.close_session()
                raise
            try:
                return self._complete(client, {"mode": mode, "serial": serial})
            except api_error as error:
                raise WizardError(f"萤石设备验证失败：{error}") from error
            finally:
                client.close_session()

    def finish_sms(self, code: str, *, expected_mode: str) -> dict[str, Any]:
        code = code.strip()
        if not code.isdigit() or not 4 <= len(code) <= 8:
            raise WizardError("短信验证码格式不正确")
        with self._lock:
            if self._pending is None:
                raise WizardError("验证码会话已失效，请重新输入账号和密码")
            client, context, expires_at = self._pending
            if time.monotonic() > expires_at:
                self._close_pending()
                raise WizardError("验证码会话已超时，请重新登录")
            if context.get("mode") != expected_mode:
                raise WizardError("验证码与当前操作不匹配，请返回原步骤继续")
            _, verification_error, api_error = self._dependencies()
            try:
                client.login(sms_code=int(code))
            except verification_error as error:
                raise WizardError("短信验证码未通过验证") from error
            except api_error as error:
                self._close_pending()
                raise WizardError(f"萤石登录失败：{error}") from error
            except BaseException:
                self._close_pending()
                raise
            try:
                return self._complete(client, context)
            except api_error as error:
                raise WizardError(f"萤石设备验证失败：{error}") from error
            finally:
                self._close_pending()

    def close(self) -> None:
        with self._lock:
            self._close_pending()


class WizardApplication:
    def __init__(
        self,
        *,
        settings_store: SettingsStore,
        token_file: Path,
        config_file: Path,
        html_file: Path,
        reload_callback: Callable[[], None],
        bridge_status: Callable[[], Mapping[str, Any]],
        discover: Callable[..., list[dict[str, object]]] = discover_ezviz_devices,
        login_dependencies: tuple[
            Callable[..., Any], type[BaseException], type[BaseException]
        ]
        | None = None,
    ) -> None:
        self.settings_store = settings_store
        self.token_file = token_file
        self.config_file = config_file
        self.html_file = html_file
        self.reload_callback = reload_callback
        self.bridge_status = bridge_status
        self.discover = discover
        self.csrf_token = secrets.token_urlsafe(32)
        self.login = LoginCoordinator(
            token_file,
            reload_callback=reload_callback,
            dependencies=login_dependencies,
            discover=discover,
        )

    def html(self) -> bytes:
        text = self.html_file.read_text(encoding="utf-8")
        return text.replace("__CSRF_TOKEN__", self.csrf_token).encode("utf-8")

    def pin(self) -> str:
        try:
            return config_tool.read_homekit_pin(self.config_file)
        except (OSError, RuntimeError):
            return ""

    def status(self) -> dict[str, Any]:
        settings = self.settings_store.load()
        return {
            "settings": settings,
            "configured": settings_complete(settings),
            "authenticated": token_matches_serial(
                self.token_file, str(settings.get("serial") or "")
            ),
            "homekit_pin": self.pin(),
            "bridge": dict(self.bridge_status()),
        }

    def save_settings(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        settings = payload.get("settings")
        if not isinstance(settings, dict):
            raise WizardError("缺少配置内容")
        normalized = self.settings_store.save(settings)
        self.reload_callback()
        return {"settings": normalized, "message": "配置已保存并正在应用"}

    def run_discovery(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        hint = str(payload.get("serial_hint") or "").strip()
        try:
            devices = self.discover(timeout=3.0, serial_hint=hint)
        except (OSError, ValueError) as error:
            raise WizardError(f"局域网搜索失败：{error}") from error
        return {
            "devices": devices,
            "message": (
                f"找到 {len(devices)} 台设备"
                if devices
                else "没有收到设备回应，请确认摄像头与服务器位于同一 VLAN"
            ),
        }

    def run_login(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        settings = self.settings_store.load()
        if not settings_complete(settings):
            raise WizardError("请先保存摄像头和桥接配置")
        sms_code = str(payload.get("sms_code") or "")
        if sms_code:
            return self.login.finish_sms(sms_code, expected_mode="configured")
        return self.login.begin(
            account=str(payload.get("account") or ""),
            password=str(payload.get("password") or ""),
            serial=str(settings["serial"]),
            region=str(settings["region"]),
        )

    def run_identify(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        settings = self.settings_store.load()
        sms_code = str(payload.get("sms_code") or "")
        if sms_code:
            return self.login.finish_sms(sms_code, expected_mode="identify")
        return self.login.begin(
            account=str(payload.get("account") or ""),
            password=str(payload.get("password") or ""),
            serial=str(payload.get("serial_hint") or ""),
            region=str(settings["region"]),
            mode="identify",
        )

    def healthy(self) -> tuple[bool, dict[str, Any]]:
        status = self.status()
        bridge = status["bridge"]
        ready = status["configured"] and status["authenticated"]
        healthy = not ready or bridge.get("state") in {
            "starting",
            "running",
            "restarting",
        }
        return healthy, status

    def close(self) -> None:
        self.login.close()


def _handler(application: WizardApplication) -> type[BaseHTTPRequestHandler]:
    class WizardHandler(BaseHTTPRequestHandler):
        server_version = "EZVIZSetup/1"

        def log_message(self, format: str, *args: object) -> None:
            # Do not put form bodies, account names or query strings in logs.
            print(
                f"[Web 向导] {self.client_address[0]} {format % args}",
                flush=True,
            )

        def _headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            self.end_headers()

        def _send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _send_error(self, message: str, status: HTTPStatus) -> None:
            self._send_json({"error": message}, status)

        def _same_origin(self) -> bool:
            return _same_origin(self.headers.get("Origin"), self.headers.get("Host"))

        def _read_json(self) -> dict[str, Any]:
            if (
                self.headers.get("Content-Type", "").split(";", 1)[0]
                != "application/json"
            ):
                raise WizardError("请求必须使用 JSON 格式")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise WizardError("请求长度无效") from error
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise WizardError("请求内容为空或过大")
            try:
                value = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as error:
                raise WizardError("请求 JSON 无法解析") from error
            if not isinstance(value, dict):
                raise WizardError("请求内容必须是 JSON 对象")
            return value

        def do_GET(self) -> None:  # noqa: N802
            if not _client_allowed(self.client_address[0]):
                self._send_error("仅允许从可信局域网访问配置向导", HTTPStatus.FORBIDDEN)
                return
            path = urlsplit(self.path).path
            if path == "/":
                try:
                    body = application.html()
                except OSError as error:
                    self._send_error(
                        f"向导页面无法读取：{error}", HTTPStatus.INTERNAL_SERVER_ERROR
                    )
                    return
                self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(body))
                self.wfile.write(body)
                return
            if path == "/api/status":
                try:
                    self._send_json(application.status())
                except (OSError, SettingsError) as error:
                    self._send_error(str(error), HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if path == "/api/health":
                try:
                    healthy, status = application.healthy()
                    self._send_json(
                        {"healthy": healthy, "bridge": status["bridge"]},
                        HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                except (OSError, SettingsError) as error:
                    self._send_error(str(error), HTTPStatus.SERVICE_UNAVAILABLE)
                return
            self._send_error("页面不存在", HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if not _client_allowed(self.client_address[0]):
                self._send_error("仅允许从可信局域网访问配置向导", HTTPStatus.FORBIDDEN)
                return
            if not self._same_origin():
                self._send_error("请求来源不受信任", HTTPStatus.FORBIDDEN)
                return
            if not secrets.compare_digest(
                self.headers.get("X-CSRF-Token", ""), application.csrf_token
            ):
                self._send_error("页面会话已失效，请刷新后重试", HTTPStatus.FORBIDDEN)
                return
            try:
                payload = self._read_json()
                path = urlsplit(self.path).path
                if path == "/api/settings":
                    result = application.save_settings(payload)
                elif path == "/api/discover":
                    result = application.run_discovery(payload)
                elif path == "/api/login":
                    result = application.run_login(payload)
                elif path == "/api/identify":
                    result = application.run_identify(payload)
                else:
                    self._send_error("接口不存在", HTTPStatus.NOT_FOUND)
                    return
                self._send_json(result)
            except (WizardError, SettingsError) as error:
                self._send_error(str(error), HTTPStatus.BAD_REQUEST)
            except OSError as error:
                self._send_error(
                    f"写入配置失败：{error}", HTTPStatus.INTERNAL_SERVER_ERROR
                )
            except Exception as error:
                print(
                    f"[Web 向导] 未处理错误：{type(error).__name__}: {error}",
                    flush=True,
                )
                self._send_error("服务器处理请求失败", HTTPStatus.INTERNAL_SERVER_ERROR)

    return WizardHandler


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server(
    application: WizardApplication,
    host: str,
    port: int,
    *,
    certificate: Path,
    private_key: Path,
) -> ThreadingHTTPServer:
    server = ReusableThreadingHTTPServer((host, port), _handler(application))
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(certificate), str(private_key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    except BaseException:
        server.server_close()
        raise
    return server


def serve(
    application: WizardApplication,
    *,
    certificate: Path,
    private_key: Path,
    host: str = DEFAULT_SETUP_HOST,
    port: int = 8099,
) -> None:
    # The handler still enforces a local/private source-address allowlist when
    # listening on every interface for convenient first-run LAN access.
    server = create_server(
        application,
        host,
        port,
        certificate=certificate,
        private_key=private_key,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        application.close()
