import asyncio

import pytest

from openbot.marlin import MarlinPrinter, PrinterHalted
from openbot.marlin.connection import checksum, numbered, parse_temps
from openbot.printer.errors import PrinterError

from fake_marlin import FakeMarlin


@pytest.fixture
def marlin():
    fm = FakeMarlin().start()
    yield fm
    fm.stop()


@pytest.fixture
async def ender(marlin):
    p = MarlinPrinter(marlin.port, "ender3_pro")
    await p.connect()
    yield p
    await p.close()


async def wait_for(pred, timeout=10):
    for _ in range(int(timeout * 20)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


def test_checksum_and_numbering():
    # Reference value computed by Marlin's own algorithm (XOR of all bytes before '*').
    assert numbered(1, "G28") == f"N1 G28*{checksum('N1 G28')}"
    assert checksum("N1 G28") == 18


def test_parse_temps():
    t = parse_temps("ok T:201.3 /200.0 B:59.8 /60.0 @:127 B@:64")
    assert t == {"T": (201.3, 200.0), "B": (59.8, 60.0)}
    assert parse_temps(" T0:20.1 /0.0 B:20 /0 T1:0 /0")["T"] == (20.1, 0.0)
    assert parse_temps("echo:SD card ok") == {}


async def test_connect_reads_identity(ender):
    assert ender.info.name == "Ender-3 Pro"
    assert ender.info.firmware.startswith("Marlin 2.0.9.3")
    assert ender.capabilities["AUTOREPORT_TEMP"] and ender.capabilities["EMERGENCY_PARSER"]
    st = ender.status()
    assert st.bot_type == "ender3_pro" and st.bed is not None
    assert st.extruder.filament_present is None          # Marlin can't tell


async def test_preheat_and_autoreported_temps(ender):
    seen = []
    ender.on_status(seen.append)
    await ender.preheat(200, 60)
    await wait_for(lambda: seen and seen[-1].extruder.current_temperature > 190)
    assert seen[-1].extruder.target_temperature == 200 and seen[-1].bed[1] == 60
    with pytest.raises(PrinterError, match="limit"):
        await ender.preheat(260, 60)                      # PTFE-lined hotend: max 240


def _gcode(tmp_path, n=150):
    lines = ["; start", "M140 S60", "M190 S60", "M104 S205", "M109 S205", "G28", "G92 E0"]
    lines += [f"G1 X{10 + i % 50} Y{10 + i // 5} E{i * 0.05:.2f} F1500 ; move {i}"
              for i in range(n)]
    lines += ["M104 S0", "M140 S0", "M84", ""]
    p = tmp_path / "part.gcode"
    p.write_text("\n".join(lines))
    return p, [l.split(";")[0].strip() for l in lines if l.split(";")[0].strip()]


async def test_stream_print_with_resends(tmp_path):
    fm = FakeMarlin(corrupt_lines={12, 60, 61}).start()
    try:
        p = MarlinPrinter(fm.port, "ender3_pro")
        await p.connect()
        path, expected = _gcode(tmp_path)
        before = len(fm.received)
        await p.start_print(str(path))
        await wait_for(lambda: p.status().process.complete, 30)
        st = p.status()
        assert st.process.step == "completed" and st.process.progress == 100
        assert fm.received[before:] == expected           # every line, once, in order
        assert fm.resends >= 3
        await p.close()
    finally:
        fm.stop()


async def test_pause_resume_and_cancel(ender, marlin, tmp_path):
    path, expected = _gcode(tmp_path, 3000)
    await ender.start_print(str(path))
    await ender.pause()
    await asyncio.sleep(0.3)
    sent = len(marlin.received)
    await asyncio.sleep(0.3)
    assert len(marlin.received) == sent                   # paused: nothing more sent
    assert ender.status().process.step == "suspended"
    await ender.resume()
    await wait_for(lambda: len(marlin.received) > sent + 20)
    with pytest.raises(PrinterError, match="busy"):
        await ender.start_print(str(path))
    await ender.cancel()
    assert ender.status().process.cancelled
    assert marlin.received[-7:] == ["M104 S0", "M140 S0", "M107", "G91", "G1 Z10 F600",
                                    "G90", "M84"]
    await ender.acknowledge()
    assert ender.status().process is None


async def test_sd_card_list_and_print(ender, marlin):
    files = await ender.sd_list()
    assert files[0] == {"name": "CUBE~1.GCO", "size": 123456, "display": "cube_pla.gcode"}
    await ender.sd_print("CUBE~1.GCO")
    await wait_for(lambda: (ender.status().process.progress or 0) > 0)
    await wait_for(lambda: ender.status().process.complete, 20)
    assert ender.status().process.step == "completed"


async def test_sd_print_cancel_sends_abort(ender, marlin):
    await ender.sd_list()
    await ender.sd_print("BENCHY.GCO")
    await ender.cancel()
    assert "M524" in marlin.received and marlin.sd_printing is None


async def test_emergency_stop_halts(ender, marlin):
    await ender.emergency_stop()
    await wait_for(lambda: ender.conn.halted)
    with pytest.raises(PrinterHalted):
        await ender.home()


async def test_polling_when_firmware_lacks_autoreport():
    fm = FakeMarlin(autoreport=False).start()
    try:
        p = MarlinPrinter(fm.port, "ender3")
        await p.connect()
        assert p._poll_task is not None
        await p.preheat(190, 50)
        await wait_for(lambda: p.status().extruder.current_temperature > 150, 10)
        await p.close()
    finally:
        fm.stop()


async def test_console(ender, tmp_path):
    reply = await ender.send_gcode("M105")
    assert any("T:" in l for l in reply)
    path, _ = _gcode(tmp_path, 3000)
    await ender.start_print(str(path))
    with pytest.raises(PrinterError, match="streaming"):
        await ender.send_gcode("G28")
    await ender.cancel()


async def test_marlin_backend_capabilities_and_roles(ender):
    from openbot.backend import PermissionDenied, Role, Unsupported
    from openbot.marlin.backend import MarlinBackend
    admin = MarlinBackend(ender)
    assert admin.supports("sd_card") and not admin.supports("camera")
    assert (await admin.status()).bot_type == "ender3_pro"
    assert await admin.sd_list()
    with pytest.raises(Unsupported):
        await admin.wifi_scan()
    viewer = MarlinBackend(ender, role=Role.VIEWER)
    with pytest.raises(PermissionDenied):
        await viewer.send_gcode("G28")
