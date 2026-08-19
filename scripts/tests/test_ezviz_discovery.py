from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
LINUX_DIR = PROJECT_DIR / "deploy" / "linux"
sys.path.insert(0, str(LINUX_DIR))

import ezviz_discovery  # noqa: E402

SAMPLE_RESPONSE = b"""<?xml version="1.0" encoding="utf-8"?>
<ProbeMatch>
  <Uuid>private-probe-id</Uuid>
  <DeviceType>CS-CB2</DeviceType>
  <DeviceDescription>EZVIZ Battery Camera</DeviceDescription>
  <DeviceSN>TESTCB2123456</DeviceSN>
  <IPv4Address>192.168.89.99</IPv4Address>
  <MAC>AA-BB-CC-DD-EE-FF</MAC>
  <SoftwareVersion>V1.0.0</SoftwareVersion>
  <NewDirectReverse>1</NewDirectReverse>
</ProbeMatch>
"""


def test_probe_shape_matches_sadp_inquiry() -> None:
    payload = ezviz_discovery.build_probe(
        "inquiry_v32", "12345678-1234-1234-1234-123456789ABC"
    )
    root = ET.fromstring(payload)  # noqa: S314 - locally generated probe XML

    assert root.tag == "Probe"
    assert root.findtext("Types") == "inquiry_v32"
    assert root.findtext("Uuid") == "12345678-1234-1234-1234-123456789ABC"


def test_parse_sadp_response_uses_packet_source_as_trusted_ip() -> None:
    device = ezviz_discovery.parse_probe_response(SAMPLE_RESPONSE, "192.168.50.21")

    assert device is not None
    assert device.serial == "TESTCB2123456"
    assert device.ip == "192.168.50.21"
    assert device.model == "CS-CB2"
    assert device.new_direct_reverse == "1"
    assert device.as_dict("123456")["matches_hint"] is True


def test_parse_falls_back_to_xml_ip_and_strips_channel_suffix() -> None:
    payload = SAMPLE_RESPONSE.replace(b"TESTCB2123456", b"TESTCB2123456_1")
    device = ezviz_discovery.parse_probe_response(payload)

    assert device is not None
    assert device.serial == "TESTCB2123456"
    assert device.ip == "192.168.89.99"


def test_parse_ignores_malformed_or_unidentified_responses() -> None:
    assert ezviz_discovery.parse_probe_response(b"not xml", "192.168.1.2") is None
    assert ezviz_discovery.parse_probe_response(b"<ProbeMatch/>", "192.168.1.2") is None


def test_parse_rejects_public_oversized_and_entity_expanding_payloads() -> None:
    public_address = SAMPLE_RESPONSE.replace(b"192.168.89.99", b"8.8.8.8")
    assert ezviz_discovery.parse_probe_response(public_address) is None
    assert (
        ezviz_discovery.parse_probe_response(
            b"x" * (ezviz_discovery.MAX_RESPONSE_BYTES + 1), "192.168.1.2"
        )
        is None
    )

    entity_payload = b"""<?xml version="1.0"?>
<!DOCTYPE ProbeMatch [<!ENTITY serial "TESTCB2123456">]>
<ProbeMatch>
  <DeviceSN>&serial;</DeviceSN>
  <IPv4Address>192.168.89.99</IPv4Address>
</ProbeMatch>
"""
    assert ezviz_discovery.parse_probe_response(entity_payload) is None
