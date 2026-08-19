#!/usr/bin/env python3
"""Discover owned EZVIZ/Hikvision devices with the read-only SADP probe."""

from __future__ import annotations

import ipaddress
import socket
import struct
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

SADP_GROUP = "239.255.255.250"
SADP_PORT = 37020
MAX_RESPONSE_BYTES = 65535
PROBE_TYPES = ("inquiry", "inquiry_v32")
SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")
SIOCGIFADDR = 0x8915  # Linux ioctl; failures use the hostname fallback below.
IPV6_INTERFACES_PATH = Path("/proc/net/if_inet6")
# Linux IFA_F_TEMPORARY, OPTIMISTIC, DADFAILED, DEPRECATED, and TENTATIVE.
UNSTABLE_IPV6_FLAGS = 0x01 | 0x04 | 0x08 | 0x20 | 0x40


@dataclass(frozen=True)
class DiscoveredDevice:
    serial: str
    ip: str
    model: str = ""
    description: str = ""
    mac: str = ""
    firmware: str = ""
    new_direct_reverse: str = ""
    source: str = "sadp"

    def as_dict(self, serial_hint: str = "") -> dict[str, object]:
        hint = serial_hint.strip().upper()
        serial = self.serial.upper()
        return {
            "serial": self.serial,
            "ip": self.ip,
            "model": self.model,
            "description": self.description,
            "mac": self.mac,
            "firmware": self.firmware,
            "new_direct_reverse": self.new_direct_reverse,
            "source": self.source,
            "matches_hint": bool(hint and (serial == hint or serial.endswith(hint))),
        }


def build_probe(probe_type: str, probe_uuid: str | None = None) -> bytes:
    if probe_type not in PROBE_TYPES:
        raise ValueError(f"unsupported SADP probe type: {probe_type}")
    identifier = (probe_uuid or str(uuid.uuid4())).upper()
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<Probe><Uuid>{identifier}</Uuid><Types>{probe_type}</Types></Probe>"
    ).encode()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _fields(root: Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for element in root.iter():
        if element.text and element.text.strip():
            result[_local_name(element.tag)] = element.text.strip()
        for key, value in element.attrib.items():
            if value.strip():
                result[_local_name(key)] = value.strip()
    return result


def _first(fields: dict[str, str], names: Iterable[str]) -> str:
    for name in names:
        value = fields.get(name.lower())
        if value:
            return value
    return ""


def _usable_ipv4(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return ""
    if not isinstance(address, ipaddress.IPv4Address):
        return ""
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        return ""
    if not (
        address.is_private or address.is_link_local or address in SHARED_ADDRESS_SPACE
    ):
        return ""
    return str(address)


def parse_probe_response(payload: bytes, peer_ip: str = "") -> DiscoveredDevice | None:
    if not payload or len(payload) > MAX_RESPONSE_BYTES:
        return None
    try:
        root = DefusedET.fromstring(payload)
    except (DefusedET.ParseError, DefusedXmlException):
        return None
    fields = _fields(root)
    serial = _first(
        fields,
        ("subserial", "devicesn", "serialnumber", "serialno", "deviceserial"),
    ).strip()
    if "_" in serial:
        serial = serial.split("_", 1)[0]
    address = _usable_ipv4(peer_ip) or _usable_ipv4(
        _first(
            fields,
            ("ipv4address", "deviceip", "deviceipaddress", "ipaddress", "ip"),
        )
    )
    if not serial or not address:
        return None
    return DiscoveredDevice(
        serial=serial.upper(),
        ip=address,
        model=_first(fields, ("devicetype", "model", "productmodel")),
        description=_first(fields, ("devicedescription", "description", "devicename")),
        mac=_first(fields, ("mac", "macaddress")),
        firmware=_first(fields, ("softwareversion", "firmwareversion", "firmware")),
        new_direct_reverse=_first(fields, ("newdirectreverse",)),
    )


def interface_ipv4_addresses() -> list[str]:
    """Return non-loopback interface addresses without shelling out."""

    addresses: set[str] = set()
    try:
        import fcntl  # Linux/macOS only; the container is Linux.

        for _, name in socket.if_nameindex():
            control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                request = struct.pack("256s", name.encode("utf-8")[:15])
                response = fcntl.ioctl(control.fileno(), SIOCGIFADDR, request)
                address = _usable_ipv4(socket.inet_ntoa(response[20:24]))
                if address:
                    addresses.add(address)
            except OSError:
                continue
            finally:
                control.close()
    except (ImportError, OSError):
        pass

    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = _usable_ipv4(item[4][0])
            if address:
                addresses.add(address)
    except OSError:
        pass
    return sorted(addresses)


def _usable_ipv6(value: str, interface: str = "") -> str:
    text = value.strip().strip("[]")
    address_text, _, supplied_scope = text.partition("%")
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        return ""
    if not isinstance(address, ipaddress.IPv6Address) or address.ipv4_mapped:
        return ""
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        return ""
    if not (address.is_private or address.is_link_local):
        return ""
    if address.is_link_local:
        scope = supplied_scope.strip() or interface.strip()
        if scope:
            return f"{address.compressed}%{scope}"
    return address.compressed


def interface_ipv6_addresses() -> list[str]:
    """Return usable LAN IPv6 addresses, scoped when link-local."""

    addresses: set[str] = set()
    proc_read = False
    try:
        lines = IPV6_INTERFACES_PATH.read_text(encoding="ascii").splitlines()
        proc_read = True
        for line in lines:
            fields = line.split()
            if len(fields) != 6:
                continue
            packed, _, _, _, flags_text, interface = fields
            try:
                flags = int(flags_text, 16)
                value = socket.inet_ntop(socket.AF_INET6, bytes.fromhex(packed))
            except (OSError, ValueError):
                continue
            if flags & UNSTABLE_IPV6_FLAGS:
                continue
            address = _usable_ipv6(value, interface)
            if address:
                addresses.add(address)
    except OSError:
        pass

    if not proc_read:
        try:
            items = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6)
        except OSError:
            items = []
        for item in items:
            sockaddr = item[4]
            scope_id = sockaddr[3] if len(sockaddr) > 3 else 0
            interface = ""
            if scope_id:
                try:
                    interface = socket.if_indextoname(scope_id)
                except OSError:
                    interface = str(scope_id)
            address = _usable_ipv6(sockaddr[0], interface)
            if address:
                addresses.add(address)
    return sorted(addresses)


def interface_lan_addresses() -> list[str]:
    """Return stable IPv4 and IPv6 addresses for TLS SANs and setup URLs."""

    return [*interface_ipv4_addresses(), *interface_ipv6_addresses()]


def _resolved_serial_hint(serial_hint: str) -> DiscoveredDevice | None:
    serial = serial_hint.strip().upper()
    if len(serial) < 7 or not serial.replace("-", "").replace("_", "").isalnum():
        return None
    for suffix in (".lan", ".local"):
        try:
            address = _usable_ipv4(socket.gethostbyname(serial.lower() + suffix))
        except OSError:
            continue
        if address:
            return DiscoveredDevice(serial=serial, ip=address, source="hostname")
    return None


def discover_ezviz_devices(
    *,
    timeout: float = 3.0,
    serial_hint: str = "",
) -> list[dict[str, object]]:
    """Send SADP discovery on every LAN interface and collect unique devices."""

    if not 0.2 <= timeout <= 10:
        raise ValueError("discovery timeout must be between 0.2 and 10 seconds")
    interfaces = interface_ipv4_addresses()
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    receiver.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    receiver.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    receiver.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    try:
        try:
            receiver.bind(("", SADP_PORT))
        except OSError:
            # Some hosts already run SADP. Replies are usually unicast to the
            # source port, so an ephemeral listener remains a useful fallback.
            receiver.bind(("", 0))

        for address in interfaces:
            membership = socket.inet_aton(SADP_GROUP) + socket.inet_aton(address)
            try:
                receiver.setsockopt(
                    socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership
                )
            except OSError:
                continue

        send_interfaces = interfaces or ["0.0.0.0"]
        for address in send_interfaces:
            try:
                receiver.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(address),
                )
            except OSError:
                continue
            for probe_type in PROBE_TYPES:
                probe = build_probe(probe_type)
                try:
                    receiver.sendto(probe, (SADP_GROUP, SADP_PORT))
                    receiver.sendto(probe, ("255.255.255.255", SADP_PORT))
                except OSError:
                    continue

        deadline = time.monotonic() + timeout
        devices: dict[tuple[str, str], DiscoveredDevice] = {}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            receiver.settimeout(remaining)
            try:
                payload, peer = receiver.recvfrom(MAX_RESPONSE_BYTES)
            except TimeoutError:
                break
            except OSError:
                break
            device = parse_probe_response(payload, peer[0])
            if device is not None:
                devices[(device.serial, device.ip)] = device
    finally:
        receiver.close()

    fallback = _resolved_serial_hint(serial_hint)
    if fallback is not None:
        devices.setdefault((fallback.serial, fallback.ip), fallback)
    hint = serial_hint.strip().upper()
    ordered = sorted(
        devices.values(),
        key=lambda device: (
            not bool(hint and (device.serial == hint or device.serial.endswith(hint))),
            device.serial,
            ipaddress.ip_address(device.ip),
        ),
    )
    return [device.as_dict(serial_hint) for device in ordered]
