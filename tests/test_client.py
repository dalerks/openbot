import asyncio
import zipfile

import pytest

from openbot.printer import (AuthError, CertificateChanged, NotTrusted, PrinterError,
                             SecureChannelRequired)
from openbot.printer.client import check_print_file
from openbot.printer.rpc import RpcConnection

from fake_printer import FAKE_JPEG, SERIAL, FakePrinter

# Made by OpenBot's own converter (MakerBot's sample files aren't redistributable).
FIXTURE = "tests/fixtures/openbot_box.makerbot"


async def test_hello_reads_identity(make_printer, fake):
    p = make_printer()
    info = await p.hello()
    assert info.serial == SERIAL
    assert info.is_replicator_plus
    assert info.firmware == "2.6.2.734"
    assert p.ssl_port == fake.ssl_port
    assert info.mac == "3c:70:59:aa:bb:cc"


async def test_untrusted_certificate_blocks_connection(make_printer, fake):
    p = make_printer()
    with pytest.raises(NotTrusted) as e:
        await p.pair("x")
    assert e.value.cert.subject.startswith("CN=MakerBot Replicator") or \
        "MakerBot Replicator" in e.value.cert.subject
    assert not [c for c in fake.calls if c[0] == "authorize"]   # nothing sent


async def test_pair_then_reconnect_without_knob(paired, make_printer, fake):
    assert paired.connected
    assert paired.credentials.get(SERIAL).username == "OpenBot@test"
    await paired.close()
    fake.knob = "reject"                       # a knob press would now fail
    p2 = make_printer()
    await p2.connect()                         # reauthorize, no knob
    assert (await p2.status()).name == "MakerBot Replicator+"
    await p2.close()


@pytest.mark.parametrize("knob,msg", [("reject", "rejected"), ("timeout", "in time")])
async def test_pair_rejected_or_timed_out(make_printer, fake, knob, msg):
    fake.knob = knob
    p = make_printer()
    await p.hello()
    p.trust.trust(p.info.serial, await p.fetch_certificate())
    with pytest.raises(AuthError, match=msg):
        await p.pair("x")
    assert p.credentials.get(SERIAL) is None


async def test_revoked_pairing_is_reported(paired, make_printer, fake):
    await paired.close()
    fake.local_auth.clear()                    # e.g. user removed us on the printer
    p = make_printer()
    with pytest.raises(AuthError, match="pair again"):
        await p.connect()


async def test_certificate_change_is_refused(paired, make_printer, fake):
    await paired.close()
    await fake.swap_certificate()
    p = make_printer()
    with pytest.raises(CertificateChanged) as e:
        await p.connect()
    assert e.value.trusted.sha256 != e.value.presented.sha256


async def test_status_parsing(paired):
    s = await paired.status()
    assert s.idle
    assert s.extruder.tool_id == 8
    assert s.extruder.tool_type == "mk13"
    assert s.extruder.filament_present


async def test_print_upload_is_byte_exact(paired, fake):
    data = open(FIXTURE, "rb").read()
    seen = []
    await paired.start_print(FIXTURE, progress=lambda s, t: seen.append((s, t)))
    assert fake.files["/current_thing/openbot_box.makerbot"] == data
    assert seen[-1] == (len(data), len(data))
    assert len(seen) == -(-len(data) // 50_000)   # one progress call per 50 kB block
    assert getattr(fake, "plate_cleared", False)
    s = await paired.status()
    assert s.process.is_print and s.process.step == "initial_heating"


async def test_print_refused_while_busy(paired, fake):
    await paired.start_print(FIXTURE)
    with pytest.raises(PrinterError, match="busy"):
        await paired.start_print(FIXTURE)
    await paired.cancel()
    assert (await paired.status()).idle


async def test_wifi_scan_and_connect_over_tls(paired, fake):
    nets = await paired.wifi_scan()
    assert [n.name for n in nets] == ["HomeNet", "", "Cafe"]   # strongest first
    home = nets[0]
    assert home.secured and not home.saved
    state = await paired.wifi_connect(home.path, "hunter22")
    assert state.state == "wifi" and state.name == "HomeNet"
    assert (await paired.wifi_scan())[0].saved


async def test_hidden_network_connect(paired):
    hidden = [n for n in await paired.wifi_scan() if n.hidden][0]
    state = await paired.wifi_connect(hidden.path, "secret", name="Attic")
    assert state.name == "Attic"


async def test_wifi_connect_refused_on_plaintext(paired, fake):
    # Mirror the real printer: a one_time_token authenticates :9999, but
    # secure-only methods are still refused there.
    token = (await paired.call("reauthorize",
                               paired.credentials.get(SERIAL).reauthorize_params()))["one_time_token"]
    plain = await RpcConnection.open("127.0.0.1", fake.port)
    try:
        await plain.call("authenticate", {"access_token": token})
        with pytest.raises(SecureChannelRequired):
            await plain.call("wifi_connect", {"path": "/x"})
    finally:
        await plain.close()


async def test_one_time_token_is_single_use(paired, fake):
    token = (await paired.call("reauthorize",
                               paired.credentials.get(SERIAL).reauthorize_params()))["one_time_token"]
    for expect_ok in (True, False):
        c = await RpcConnection.open("127.0.0.1", fake.port)
        try:
            if expect_ok:
                await c.call("authenticate", {"access_token": token})
            else:
                with pytest.raises(Exception):
                    await c.call("authenticate", {"access_token": token})
        finally:
            await c.close()


async def test_static_ip_validation(paired):
    await paired.set_static_ipv4(ip="192.168.1.50", netmask="255.255.255.0",
                                 gateway="192.168.1.1")
    cfg = await paired.get_static_ipv4()
    assert cfg.use_static and cfg.ip == "192.168.1.50"
    with pytest.raises(PrinterError):
        await paired.set_static_ipv4(ip="999.1.1.1")
    await paired.use_dhcp()
    assert not (await paired.get_static_ipv4()).use_static


async def test_snapshot(paired):
    frame = await paired.snapshot()
    assert frame.is_jpeg and frame.width == 640 and frame.data == FAKE_JPEG


async def test_camera_stream_interleaved_with_calls(paired):
    frames = 0
    async for frame in paired.camera_stream():
        assert frame.data == FAKE_JPEG
        # JSON-RPC calls must still work while binary frames are flowing.
        assert (await paired.status()).name
        frames += 1
        if frames == 5:
            break
    await asyncio.sleep(0.1)
    assert (await paired.status()).idle


async def test_camera_frames_split_across_reads(stores):
    fp = await FakePrinter(chunked_frames=True).start()
    try:
        from openbot.printer import Printer
        trust, creds = stores
        p = Printer("127.0.0.1", port=fp.port, trust=trust, credentials=creds)
        await p.hello()
        trust.trust(p.info.serial, await p.fetch_certificate())
        await p.pair("x")
        for _ in range(3):
            frame = await p.snapshot()
            assert frame.data == FAKE_JPEG
        await p.close()
    finally:
        await fp.stop()


async def test_disconnect_fails_pending_calls(paired, fake):
    closed = asyncio.Event()
    paired.on_disconnect(lambda err: closed.set())
    await fake.stop()
    await asyncio.wait_for(closed.wait(), 5)
    with pytest.raises(PrinterError):
        await paired.status()


def test_check_print_file_real_fixture(tmp_path):
    from openbot.printer.models import PrinterStatus
    status = PrinterStatus.from_info(FakePrinter().system_info())
    assert check_print_file(FIXTURE, status) == []
    # Tough extruder attached -> mismatch warning
    info = FakePrinter().system_info()
    info["toolheads"]["extruder"][0]["tool_id"] = 999
    assert check_print_file(FIXTURE, PrinterStatus.from_info(info)) == []  # unknown id: no claim
    bad = tmp_path / "other.makerbot"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("meta.json", '{"bot_type": "mini_8", "tool_type": "mk14"}')
    assert any("not a Replicator+" in p for p in check_print_file(str(bad), status))
    junk = tmp_path / "junk.makerbot"
    junk.write_bytes(b"nope")
    assert "not a valid" in check_print_file(str(junk))[0]


async def test_new_printer_asks_for_trust_before_pairing(make_printer):
    p = make_printer()
    with pytest.raises(NotTrusted):
        await p.connect()          # not trusted AND not paired -> trust comes first
    p.trust.trust(p.info.serial, await p.fetch_certificate())
    with pytest.raises(AuthError):
        await p.connect()          # now trusted, still not paired
