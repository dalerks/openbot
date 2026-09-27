"""Models against data captured from the real printer, plus backend role checks."""

import json

import pytest

from openbot.backend import PermissionDenied, Role
from openbot.local_backend import LocalBackend
from openbot.printer.models import (NetworkState, PrinterInfo, PrinterStatus, WifiNetwork,
                                    mac_from_serial)

REAL = json.load(open("tests/fixtures/replicator_plus_fw262_read.json"))


def test_real_system_information():
    s = PrinterStatus.from_info(REAL["get_system_information"])
    assert s.bot_type == "replicator_b"
    assert s.machine_type == "horseshoe"
    assert s.firmware == "2.6.2.734"
    assert s.extruder.tool_id == 8
    assert s.extruder.display_name == "Smart Extruder+"


def test_real_network_state():
    n = NetworkState.from_dict(REAL["network_state"])
    assert n.state == "ethernet" and not n.static and n.wifi_radio == "enabled"


def test_real_wifi_scan_has_hidden_network():
    nets = [WifiNetwork(**a) for a in REAL["wifi_scan"]]
    assert any(n.hidden for n in nets)


def test_mac_from_real_serial():
    # Verified on hardware: serial ...3C70590A1B2C <-> ARP 3c:70:59:0a:1b:2c
    assert mac_from_serial("23C1000B3C70590A1B2C") == "3c:70:59:0a:1b:2c"
    assert mac_from_serial("short") is None


def test_bonjour_txt_ip_none_is_treated_as_missing():
    info = PrinterInfo.from_handshake({"iserial": "X", "ip": "None"}, source="bonjour")
    assert info.ip is None


async def test_viewer_cannot_change_wifi(paired):
    viewer = LocalBackend(paired, role=Role.VIEWER)
    assert (await viewer.status()).name
    with pytest.raises(PermissionDenied):
        await viewer.wifi_connect("/net/connman/service/wifi_home", "x")
    with pytest.raises(PermissionDenied):
        await viewer.start_print("tests/fixtures/openbot_box.makerbot")


async def test_operator_can_print_but_not_rename(paired):
    op = LocalBackend(paired, role=Role.OPERATOR)
    await op.start_print("tests/fixtures/openbot_box.makerbot")
    with pytest.raises(PermissionDenied):
        await op.rename("x")
    admin = LocalBackend(paired, role=Role.ADMIN)
    await admin.rename("Shop Printer")
    assert (await admin.status()).name == "Shop Printer"


def test_parse_real_dnssd_txt_line():
    from openbot.printer.discovery import parse_dnssd_txt
    # Captured from the real printer with `dns-sd -L` on 2026-09-27.
    line = (r" machine_type=horseshoe vid=9153 ip=None pid=11 api_version=1.9.0 "
            r"iserial=23C1000B3C70590A1B2C firmware_version=2.6.2.734 ssl_port=12309 "
            r"machine_name=MakerBot\ Replicator+ motor_driver_version=4.6 "
            r"bot_type=replicator_b port=9999")
    txt = parse_dnssd_txt(line)
    info = PrinterInfo.from_handshake(txt, source="bonjour")
    assert info.name == "MakerBot Replicator+"
    assert info.ip is None
    assert info.serial == "23C1000B3C70590A1B2C"
    assert info.ssl_port == 12309 and info.is_replicator_plus
