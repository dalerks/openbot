"""Several printers at once: the app's PrinterHub with two fake Klipper printers."""

import asyncio

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from openbot.app.hub import PrinterHub, summary  # noqa: E402

from fake_moonraker import FakeMoonraker  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    yield QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
async def two_pis():
    a, b = await FakeMoonraker().start(), await FakeMoonraker().start()
    yield a, b
    await a.stop()
    await b.stop()


async def until(pred, timeout=5):
    for _ in range(int(timeout * 50)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_two_printers_at_once(two_pis, tmp_path):
    a, b = two_pis
    hub = PrinterHub(remember=False)
    seen = []
    hub.status_changed.connect(lambda st: seen.append(st.extruder.target_temperature))
    assert await hub.connect_moonraker(a.address, "ender3_pro") is None
    first = hub.active
    assert await hub.connect_moonraker(b.address, "ender3") is None
    second = hub.active
    assert first is not second and hub.printers() == [first, second]
    assert first.connected and second.connected          # A stayed connected

    # Print on B while A is in the background, then on A while B prints.
    path = tmp_path / "part.gcode"
    path.write_text("G28\nG1 X10 E1\n")
    await hub.backend.start_print(str(path))
    assert ("start", "part.gcode") in b.actions and not a.actions
    hub.select(first)
    assert hub.machine_id == "ender3_pro"
    await hub.backend.start_print(str(path))
    assert ("start", "part.gcode") in a.actions
    await until(lambda: second.status.process is not None and
                first.status.process is not None)
    assert "printing" in summary(second) or "%" in summary(second)

    # Only the selected printer's updates reach the pages.
    seen.clear()
    b.update(extruder={"target": 111.0})
    a.update(extruder={"target": 222.0})
    await until(lambda: 222.0 in seen)
    await asyncio.sleep(0.1)
    assert 111.0 not in seen

    # Connecting to the same address again just selects it.
    assert await hub.connect_moonraker(b.address, "ender3") is None
    assert hub.active is second and len(hub.printers()) == 2

    # Disconnecting the selected printer leaves the other one connected and selected.
    await hub.disconnect()
    assert hub.active is first and first.connected and not second.connected
    assert hub.printers() == [first]
    await hub.disconnect_all()


async def test_background_errors_name_the_printer(two_pis):
    a, b = two_pis
    hub = PrinterHub(remember=False)
    await hub.connect_moonraker(a.address, "ender3_pro")
    first = hub.active
    await hub.connect_moonraker(b.address, "ender3_pro")
    errors = []
    hub.error.connect(errors.append)
    first.error.emit("camera: lost")
    assert errors == ["fakepi: camera: lost"]
    await hub.disconnect_all()
