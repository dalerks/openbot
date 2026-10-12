import asyncio

import pytest

from openbot import backend as b
from openbot.moonraker import MoonrakerPrinter, base_url
from openbot.moonraker.backend import MoonrakerBackend
from openbot.moonraker.printer import jpeg_size
from openbot.printer.errors import PrinterError

from fake_moonraker import JPEG, FakeMoonraker


@pytest.fixture
async def pi():
    fm = await FakeMoonraker().start()
    yield fm
    await fm.stop()


@pytest.fixture
async def klipper(pi):
    p = await MoonrakerPrinter(pi.address, "ender3_pro").connect()
    yield p
    await p.close()


async def wait_for(pred, timeout=5):
    for _ in range(int(timeout * 50)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


def test_base_url():
    assert base_url("192.168.2.222") == "http://192.168.2.222"
    assert base_url("http://192.168.2.222/viewer") == "http://192.168.2.222"
    assert base_url("pi.local:7125") == "http://pi.local:7125"
    with pytest.raises(PrinterError):
        base_url("http://")


def test_jpeg_size():
    assert jpeg_size(JPEG) == (2, 1)
    assert jpeg_size(b"not a jpeg") == (0, 0)


async def test_connect_and_status(klipper):
    assert klipper.info.name == "fakepi"
    assert klipper.info.firmware == "Klipper v0.13.0-642"
    s = klipper.status()
    assert s.extruder.current_temperature == 22.0
    assert s.bed == (21.5, 0.0)
    assert s.process is None
    backend = MoonrakerBackend(klipper)
    assert {b.CAMERA, b.SD_CARD, b.CONSOLE, b.FIRMWARE_RESTART} <= backend.capabilities
    assert backend.camera_transform == {"flip_h": True, "flip_v": False, "rotation": 0}


async def test_live_temperature_updates(pi, klipper):
    seen = []
    klipper.on_status(seen.append)
    pi.update(extruder={"temperature": 150.0, "target": 200.0})
    await wait_for(lambda: seen and seen[-1].extruder.target_temperature == 200.0)
    assert seen[-1].extruder.current_temperature == 150.0


async def test_upload_and_print(pi, klipper, tmp_path):
    path = tmp_path / "cube.gcode"
    path.write_bytes(b"G28\nG1 X10 E1\n" * 50000)
    progress = []
    await klipper.start_print(str(path), progress=lambda s, t: progress.append((s, t)))
    assert pi.files["cube.gcode"][0] == path.read_bytes()
    assert ("start", "cube.gcode") in pi.actions
    assert progress[-1][0] == progress[-1][1] == path.stat().st_size
    await wait_for(lambda: klipper.status().process is not None)
    p = klipper.status().process
    assert p.is_print and p.step == "heating" and p.filename == "cube.gcode"
    pi.update(print_stats={"print_duration": 12.0, "total_duration": 60.0},
              virtual_sdcard={"progress": 0.25})
    await wait_for(lambda: klipper.status().process.step == "printing")
    assert klipper.status().process.progress == 25.0
    with pytest.raises(PrinterError, match="already printing"):
        await klipper.start_print(str(path))


async def test_pause_resume_cancel_acknowledge(pi, klipper):
    await klipper.sd_print("old.gcode")
    await wait_for(lambda: klipper.status().process is not None)
    await klipper.pause()
    await wait_for(lambda: klipper.status().process.step == "suspended")
    await klipper.resume()
    await klipper.cancel()
    await wait_for(lambda: klipper.status().process.cancelled)
    assert klipper.status().process.complete
    await klipper.acknowledge()
    assert klipper.status().process is None
    assert [a for a, _ in pi.actions] == ["start", "pause", "resume", "cancel"]


async def test_files_on_the_pi(pi, klipper):
    pi.files["new.gcode"] = (b"G28\n", 300.0)
    pi.files["notes.txt"] = (b"hi", 400.0)
    names = [f["name"] for f in await klipper.sd_list()]
    assert names == ["new.gcode", "old.gcode"]
    with pytest.raises(PrinterError, match="does not exist"):
        await klipper.sd_print("missing.gcode")


async def test_console(pi, klipper):
    lines = []
    klipper.on_line(lines.append)
    assert await klipper.send_gcode("M115") == ["// ran M115"]
    assert lines == ["// ran M115"]
    with pytest.raises(PrinterError, match="Unknown command"):
        await klipper.send_gcode("BAD")


async def test_preheat_limits(pi, klipper):
    await klipper.preheat(205, 60)
    assert pi.gcode[-1] == "M104 S205\nM140 S60"
    with pytest.raises(PrinterError, match="limit"):
        await klipper.preheat(260, 60)


async def test_emergency_stop_and_restart(pi, klipper):
    await klipper.emergency_stop()
    await wait_for(lambda: klipper.status().process is not None)
    p = klipper.status().process
    assert p.step == "failed" and "M112" in p.error["message"]
    with pytest.raises(PrinterError, match="M112"):
        await klipper.home()
    await klipper.restart_firmware()
    await wait_for(lambda: klipper.status().process is None)


async def test_camera(klipper):
    snap = await klipper.snapshot()
    assert snap.is_jpeg and (snap.width, snap.height) == (2, 1)
    n = 0
    async for frame in klipper.camera_stream():
        assert frame.data == JPEG
        n += 1
        if n == 3:
            break


async def test_no_camera():
    fm = await FakeMoonraker(webcam=False).start()
    try:
        p = await MoonrakerPrinter(fm.address).connect()
        assert b.CAMERA not in MoonrakerBackend(p).capabilities
        with pytest.raises(PrinterError, match="no camera"):
            await p.snapshot()
        await p.close()
    finally:
        await fm.stop()


async def test_login_required():
    fm = await FakeMoonraker(login=("me", "secret")).start()
    try:
        with pytest.raises(PrinterError, match="username and password"):
            await MoonrakerPrinter(fm.address).connect()
        with pytest.raises(PrinterError, match="Invalid Password"):
            await MoonrakerPrinter(fm.address, username="me", password="nope").connect()
        p = await MoonrakerPrinter(fm.address, username="me", password="secret").connect()
        assert p.status().extruder is not None
        assert await p.send_gcode("G28") == ["// ran G28"]
        await p.close()
    finally:
        await fm.stop()


async def test_disconnect_is_reported(pi, klipper):
    lost = asyncio.Event()
    klipper.on_disconnect(lambda reason: lost.set())
    for ws in list(pi.sockets):
        await ws.close()
    await asyncio.wait_for(lost.wait(), 5)
    assert not klipper.connected


async def test_roles(klipper):
    viewer = MoonrakerBackend(klipper, role=b.Role.VIEWER)
    await viewer.status()
    with pytest.raises(b.PermissionDenied):
        await viewer.cancel()
    with pytest.raises(b.PermissionDenied):
        await MoonrakerBackend(klipper, role=b.Role.OPERATOR).send_gcode("G28")


async def test_klipper_and_probe_capabilities(klipper):
    caps = MoonrakerBackend(klipper).capabilities
    assert b.KLIPPER in caps and b.BED_PROBE in caps


def test_bed_mesh_goes_after_bed_heating():
    from openbot.slicing.profiles import BED_MESH_GCODE, add_bed_mesh
    start = "G90\nM104 S205\nM190 S60\nM109 S205\nG1 Z0.28"
    lines = add_bed_mesh(start).split("\n")
    assert lines[lines.index("M190 S60") + 2] == BED_MESH_GCODE
    assert lines[lines.index("M190 S60") + 1].startswith("M104 S150")
    assert lines.index(BED_MESH_GCODE) < lines.index("M109 S205")
    # No later M109: never leave the nozzle cold.
    lines = add_bed_mesh("G28\nM190 S60").split("\n")
    assert lines == ["G28", "M190 S60", BED_MESH_GCODE]
    assert add_bed_mesh("G28\nG1 Z5").split("\n")[1] == BED_MESH_GCODE


def test_klipper_settings():
    from openbot.slicing.profiles import PrintSettings, SettingsError, _klipper_overrides
    with pytest.raises(SettingsError, match="Klipper"):
        PrintSettings(machine="ender3_pro", speed="fast").validate()
    with pytest.raises(SettingsError, match="Klipper"):
        PrintSettings(machine="replicator_plus", klipper=True, bed_mesh=True).validate()
    s = PrintSettings(machine="ender3_pro", klipper=True, speed="fast",
                      bed_mesh=True).validate()
    cfg = {"machine_start_gcode": "G28\nM190 S60\nM109 S200", "gcode_flavor": "marlin"}
    _klipper_overrides(cfg, s)
    assert cfg["gcode_flavor"] == "klipper" and cfg["default_acceleration"] == "2000"
    assert "BED_MESH_CALIBRATE" in cfg["machine_start_gcode"]
    cfg = {"gcode_flavor": "marlin"}
    _klipper_overrides(cfg, PrintSettings(machine="ender3_pro", klipper=True).validate())
    assert cfg["gcode_flavor"] == "marlin" and "default_acceleration" not in cfg
