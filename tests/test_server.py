"""End-to-end: OpenBot server (TLS on localhost) + fake printers + remote clients."""

import asyncio
import os

import pytest

from openbot.backend import PermissionDenied, Role
from openbot.local_backend import LocalBackend
from openbot.printer.errors import CertificateChanged, PrinterError
from openbot.printer.trust import TrustStore
from openbot.remote.client import (RemoteBackend, fetch_server_certificate, request_pairing,
                                   server_context, server_info, trust_key,
                                   wait_for_approval)
from openbot.server.app import OpenBotServer
from openbot.server.host import PrinterHost

from fake_printer import FAKE_JPEG

MAKERBOT = "tests/fixtures/openbot_box.makerbot"


async def until(pred, timeout=10):
    for _ in range(int(timeout * 20)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


@pytest.fixture
async def served(paired, tmp_path):
    """A server sharing the paired fake Replicator+; yields (server, host, fake)."""
    host = PrinterHost()
    await host.attach(LocalBackend(paired))
    server = OpenBotServer(str(tmp_path / "srv"), host, name="test-server")
    await server.start("127.0.0.1", 0)
    yield server
    await server.stop()


@pytest.fixture
def client_trust(tmp_path):
    return TrustStore(str(tmp_path / "client"))


async def trusted_ctx(server, trust):
    cert = await fetch_server_certificate("127.0.0.1", server.port)
    trust.trust(trust_key("127.0.0.1", server.port), cert)
    return await server_context("127.0.0.1", server.port, trust)


async def join(server, ctx, name, role=None, admin=None):
    """Pair a client: setup code when there's no admin yet, else approval by `admin`."""
    if admin is None:
        req = await request_pairing("127.0.0.1", server.port, ctx, name,
                                    setup_code=server.clients.setup_code)
    else:
        req = await request_pairing("127.0.0.1", server.port, ctx, name)
        assert len(req["code"]) == 6
        pending = await admin.pending_pairings()
        assert [p["code"] for p in pending] == [req["code"]]      # admin sees the same code
        await admin.approve_pairing(req["id"], role)
    got = await wait_for_approval("127.0.0.1", server.port, ctx, req["id"], interval=0.05)
    return await RemoteBackend.connect("127.0.0.1", server.port, got["token"], ctx)


async def test_info_pairing_and_roles(served, client_trust, fake):
    ctx = await trusted_ctx(served, client_trust)
    info = await server_info("127.0.0.1", served.port, ctx)
    assert info["needs_setup"] and info["printer"]["machine"] == "replicator_plus"
    admin = await join(served, ctx, "Admin Mac")
    assert admin.role == Role.ADMIN and not served.clients.setup_code
    viewer = await join(served, ctx, "Kid's laptop", Role.VIEWER, admin)
    operator = await join(served, ctx, "Shop Mac", Role.OPERATOR, admin)
    try:
        assert (await viewer.status()).name == "MakerBot Replicator+"
        assert "camera" in viewer.capabilities and "queue" in viewer.capabilities
        with pytest.raises(PermissionDenied):
            await viewer.wifi_scan()
        with pytest.raises(PermissionDenied):
            await viewer.start_print(MAKERBOT)
        with pytest.raises(PermissionDenied):
            await operator.wifi_connect("/net/connman/service/wifi_home", "x")
        nets = await admin.wifi_scan()                      # admin reaches the printer
        assert nets[0].name == "HomeNet"
        state = await admin.wifi_connect(nets[0].path, "hunter22")
        assert state.state == "wifi"
    finally:
        for c in (viewer, operator, admin):
            await c.close()


async def test_queue_confirm_print_and_finish(served, client_trust, fake):
    ctx = await trusted_ctx(served, client_trust)
    admin = await join(served, ctx, "Admin")
    operator = await join(served, ctx, "Operator", Role.OPERATOR, admin)
    seen = []
    admin.on_queue(seen.append)
    try:
        progress = []
        job = await operator.start_print(MAKERBOT, progress=lambda s, t: progress.append(s))
        assert progress[-1] == os.path.getsize(MAKERBOT)
        await until(lambda: served.queue.get(job["id"]).state == "waiting_for_plate")
        assert not fake.files                               # nothing starts unconfirmed
        await operator.confirm_plate(job["id"])
        assert fake.files["/current_thing/openbot_box.makerbot"] == \
            open(MAKERBOT, "rb").read()
        await until(lambda: served.queue.get(job["id"]).state == "printing")
        fake.process.update(step="completed", progress=100, complete=True)
        await fake.broadcast_state()
        await until(lambda: served.queue.get(job["id"]).state == "done")
        await until(lambda: served.queue.get(job["id"]).snapshot)
        assert await operator.job_snapshot(job["id"]) == FAKE_JPEG
        assert any(j and j[0]["state"] == "done" for j in seen)   # admins got live updates
        with pytest.raises(PrinterError, match="needs a .makerbot"):
            await operator.start_print("tests/fixtures/test_camera_frame.jpg")
    finally:
        await operator.close()
        await admin.close()


async def test_camera_relay_and_snapshot(served, client_trust):
    ctx = await trusted_ctx(served, client_trust)
    viewer = await join(served, ctx, "Viewer")
    try:
        assert (await viewer.snapshot()).data == FAKE_JPEG
        frames = 0
        async for frame in viewer.camera_stream():
            assert frame.data == FAKE_JPEG
            frames += 1
            if frames == 3:
                break
    finally:
        await viewer.close()


async def test_revoked_client_is_cut_off(served, client_trust):
    ctx = await trusted_ctx(served, client_trust)
    admin = await join(served, ctx, "Admin")
    guest = await join(served, ctx, "Guest", Role.VIEWER, admin)
    token_client = [c for c in served.clients.clients.values() if c.name == "Guest"][0]
    closed = asyncio.Event()
    guest.on_disconnect(lambda *_: closed.set())
    await admin.revoke_client(token_client.id)
    await asyncio.wait_for(closed.wait(), 5)
    with pytest.raises(PrinterError, match="pair again"):
        await RemoteBackend.connect("127.0.0.1", served.port, guest._token, ctx)
    await admin.close()


async def test_denied_pairing_and_wrong_setup_code(served, client_trust):
    ctx = await trusted_ctx(served, client_trust)
    with pytest.raises(PrinterError, match="setup code"):
        await request_pairing("127.0.0.1", served.port, ctx, "x", setup_code="AAAA-AAAA")
    admin = await join(served, ctx, "Admin")
    req = await request_pairing("127.0.0.1", served.port, ctx, "Stranger")
    await admin.deny_pairing(req["id"])
    with pytest.raises(PrinterError, match="denied"):
        await wait_for_approval("127.0.0.1", served.port, ctx, req["id"], interval=0.05)
    await admin.close()


async def test_server_certificate_is_pinned(served, tmp_path):
    trust = TrustStore(str(tmp_path / "pin"))
    from fake_printer import make_cert
    from openbot.printer.trust import CertInfo
    other = CertInfo.from_pem(make_cert()[0].decode())
    trust.trust(trust_key("127.0.0.1", served.port), other)
    with pytest.raises(CertificateChanged):
        await server_context("127.0.0.1", served.port, trust)


async def test_usb_ender_behind_the_server(tmp_path, client_trust):
    from fake_marlin import FakeMarlin
    from test_marlin import _gcode
    fm = FakeMarlin().start()
    host = PrinterHost(usb_port=fm.port, machine_id="ender3_pro")
    from openbot.marlin import MarlinPrinter
    from openbot.marlin.backend import MarlinBackend
    p = MarlinPrinter(fm.port, "ender3_pro")
    await p.connect()
    await host.attach(MarlinBackend(p))
    server = OpenBotServer(str(tmp_path / "srv2"), host, name="garage-pi")
    await server.start("127.0.0.1", 0)
    try:
        ctx = await trusted_ctx(server, client_trust)
        admin = await join(server, ctx, "Admin")
        assert admin.machine_id == "ender3_pro" and "sd_card" in admin.capabilities
        assert [f["display"] for f in await admin.sd_list()][0] == "cube_pla.gcode"
        path, expected = _gcode(tmp_path, 40)
        job = await admin.start_print(str(path))
        await until(lambda: server.queue.get(job["id"]).state == "waiting_for_plate")
        await admin.confirm_plate(job["id"])
        await until(lambda: server.queue.get(job["id"]).state == "done", 20)
        assert fm.received[-len(expected):] == expected
        with pytest.raises(PrinterError):
            await admin.snapshot()                         # no camera on an Ender
        await admin.close()
    finally:
        await server.stop()
        await p.close()
        fm.stop()


def test_console_admin_keeps_setup_code(tmp_path):
    from openbot.server.auth import ClientStore
    store = ClientStore(str(tmp_path))
    code = store.setup_code
    store.add_local_admin("server console")
    assert store.setup_code == code and not store.has_admin()
    assert ClientStore(str(tmp_path)).setup_code        # still offered after a restart
    req = store.request("First Mac", setup_code=code.lower())
    assert req.status == "approved" and store.has_admin() and store.setup_code is None


def test_server_config_file(tmp_path):
    import argparse
    from openbot.server.service import load_config
    cfg = tmp_path / "server.toml"
    cfg.write_text('[server]\nname = "garage-pi"\nport = 9000\nbonjour = false\n'
                   '[printer]\ntype = "ender3_pro"\nusb_port = "/dev/ttyUSB0"\n')
    args = argparse.Namespace(name=None, port=8765, bind="0.0.0.0", no_bonjour=False,
                              data=None, printer=None, usb=None, printer_type="ender3")
    load_config(str(cfg), args)
    assert (args.name, args.port, args.no_bonjour, args.usb, args.printer_type) == \
        ("garage-pi", 9000, True, "/dev/ttyUSB0", "ender3_pro")
    cfg.write_text('[printer]\ntype = "replicator_plus"\naddress = "192.168.1.154"\n')
    args = argparse.Namespace(name=None, port=8765, bind="0.0.0.0", no_bonjour=False,
                              data=None, printer=None, usb=None, printer_type="ender3")
    assert load_config(str(cfg), args).printer == "192.168.1.154"


async def test_web_page_and_project_info(served, client_trust):
    import aiohttp
    ctx = await trusted_ctx(served, client_trust)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx)) as s:
        base = f"https://127.0.0.1:{served.port}"
        async with s.get(base + "/") as r:
            html = await r.text()
            assert r.status == 200 and "Lighthouse Consulting" in html and "suggested $20" in html
            assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        async with s.get(base + "/web/app.js") as r:
            assert r.status == 200 and r.content_type == "application/javascript"
        async with s.get(base + "/web/../app.py") as r:
            assert r.status == 404
        async with s.get(base + "/api/info") as r:
            info = await r.json()
        assert info["project"]["publisher_url"] == "https://www.lighthouseconsult.com/case-studies/openbot/"
        assert info["project"]["homepage_url"] == "https://www.josephrounds.dev/openbot/"
        assert info["project"]["suggested_donation_usd"] == 20
