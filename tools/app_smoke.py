#!/usr/bin/env python3
"""Drive the OpenBot app headlessly against the fake printer and screenshot every page.

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/app_smoke.py OUT_DIR [MODEL.stl]

Uses a throwaway data directory, so it never touches the real Keychain,
trusted certificates or settings. Exits non-zero if any step fails.
"""

import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["OPENBOT_DATA"] = tempfile.mkdtemp(prefix="openbot-smoke-")
os.environ["OPENBOT_NO_KEYCHAIN"] = "1"

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

import qasync  # noqa: E402
from fake_marlin import FakeMarlin  # noqa: E402
from fake_printer import FakePrinter  # noqa: E402
from openbot import drives  # noqa: E402
from openbot.app import dialogs  # noqa: E402
from openbot.app.controller import PrinterController  # noqa: E402
from openbot.app.main import PAGES, MainWindow  # noqa: E402
from openbot.backend import Role  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
MODEL = sys.argv[2] if len(sys.argv) > 2 else None
NEXT_FILES = []   # what the patched "Add Models" file dialog returns


def patch_dialogs():
    def trust_exec(self):
        self.choice = self.TRUST
        return self.DialogCode.Accepted
    dialogs.TrustDialog.exec = trust_exec
    dialogs.PairDialog.exec = lambda self: self.DialogCode.Accepted
    # A new fake printer has a new certificate for the same serial: accept the change.
    dialogs.CertificateChangedDialog.exec = lambda self: self.DialogCode.Accepted

    def confirm_exec(self):
        self.clear.setChecked(True)
        return self.DialogCode.Accepted
    dialogs.ConfirmPrintDialog.exec = confirm_exec
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING DIALOG:", a[2]))
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: (NEXT_FILES, ""))


async def until(pred, what, timeout=60):
    for _ in range(int(timeout * 10)):
        if pred():
            return
        await asyncio.sleep(0.1)
    raise SystemExit(f"FAILED: timed out waiting for {what}")


async def scenario(win, ctrl):
    fake = await FakePrinter().start()
    errors = []
    ctrl.error.connect(errors.append)

    def shot(name, filename=None):
        # No processEvents() here: pumping Qt from inside a task makes qasync re-enter
        # other tasks. The page switch is painted by grab() itself.
        win.show_page(name)
        path = os.path.join(OUT, f"{(filename or name).lower()}.png")
        win.grab().save(path)
        print("screenshot", path)

    printers = win.pages["Printers"]
    printers.ip.setText(f"127.0.0.1:{fake.port}")
    printers._connect()
    await until(lambda: ctrl.connected, "trust + pairing + connection")
    print("connected:", ctrl.printer.info.name, "| paired users:", list(fake.local_auth))
    await asyncio.sleep(0.3)
    shot("Printers")

    prepare = win.pages["Prepare"]
    if MODEL:
        # Add three copies in one go, delete one, then add another: 3 objects on the plate.
        NEXT_FILES[:] = [MODEL, MODEL, MODEL]
        prepare._add_models()
        assert len(prepare.objects) == 3, prepare.objects
        prepare.objects_list.setCurrentRow(1)
        prepare._remove_selected()
        assert len(prepare.objects) == 2
        NEXT_FILES[:] = [MODEL]
        prepare._add_models()
        assert len(prepare.objects) == 3 and len(prepare.placed) == 3
        boxes = [m.bounds() for m in prepare.placed]
        for i, (lo, hi) in enumerate(boxes):
            for lo2, hi2 in boxes[i + 1:]:
                assert hi[0] <= lo2[0] or hi2[0] <= lo[0] or hi[1] <= lo2[1] or \
                    hi2[1] <= lo[1], "objects overlap"
        print("plate:", prepare.objects_list.count(), "objects, no overlaps")
        # Transform the first object: scale, rotate, drag it in the view, duplicate.
        prepare.objects_list.setCurrentRow(0)
        prepare.obj_scale.setValue(150)
        prepare.obj_rz.setValue(30)
        o = prepare.objects[0]
        assert o.scale == 1.5 and o.rz == 30, (o.scale, o.rz)
        prepare.view.object_moved.emit(0, -100.0, 60.0)
        assert prepare.plate.manual and (round(o.x), round(o.y)) == (-100, 60), (o.x, o.y)
        prepare._duplicate()
        assert len(prepare.objects) == 4 and not prepare.plate.problems(), \
            prepare.plate.problems()
        prepare.objects_list.setCurrentRow(1)
        prepare._remove_selected()
        print("transforms: scale/rotate/drag/duplicate ok; plate problems:",
              prepare.plate.problems() or "none")
        prepare._slice()
        await until(lambda: prepare.result_path, "slicing", timeout=300)
        print("sliced:", prepare.result.text()[:120])
    shot("Prepare")

    monitor = win.pages["Monitor"]
    monitor._toggle_camera()
    await until(lambda: monitor._last_image is not None, "camera frame")
    print("camera frame:", monitor._last_image.width(), "x", monitor._last_image.height())
    await ctrl.stop_camera()

    if MODEL:
        prepare._print()
        await until(lambda: fake.files, "upload")
        await until(lambda: ctrl.status and ctrl.status.process, "print status")
        print("printing:", ctrl.status.process.step, "| uploaded",
              sum(len(v) for v in fake.files.values()), "bytes")
    shot("Monitor")

    await until(lambda: win.pages["Network"].list.topLevelItemCount() > 0, "wifi scan")
    shot("Network")
    shot("Maintenance")

    await ctrl.disconnect()
    await fake.stop()
    if MODEL:
        await ender_scenario(win, ctrl, shot)
        await share_scenario(win, ctrl, shot)
    real_errors = [e for e in errors if "camera" not in e]
    if real_errors:
        raise SystemExit("FAILED with errors: " + "; ".join(real_errors))
    print("SMOKE TEST PASSED")


async def ender_scenario(win, ctrl, shot):
    """USB Ender-3 Pro: connect, slice G-code, save to 'SD card', print over USB, console."""
    marlin = FakeMarlin().start()
    card = tempfile.mkdtemp(prefix="SDCARD-")
    fake_card = drives.Drive(card, "SDCARD", "MS-DOS FAT32", 8_000_000_000, 7_000_000_000)
    drives.list_removable = lambda: [fake_card]
    drives.eject = lambda d: None

    def save_exec(self):
        self.drive = fake_card
        return self.DialogCode.Accepted
    dialogs.SaveToDriveDialog.exec = save_exec
    try:
        printers = win.pages["Printers"]
        printers.port.setEditText(marlin.port)
        printers.usb_machine.setCurrentIndex(printers.usb_machine.findData("ender3_pro"))
        printers._connect_usb()
        await until(lambda: ctrl.connected and ctrl.machine_id == "ender3_pro", "USB connect")
        visible = [win.nav.item(i).text() for i in range(win.nav.count())
                   if not win.nav.item(i).isHidden()]
        print("USB connected:", ctrl.printer.info.name, "|", ctrl.printer.info.firmware,
              "| pages:", visible)
        assert "Network" not in visible and "Console" in visible, visible

        prepare = win.pages["Prepare"]
        assert prepare.machine.currentData() == "ender3_pro"
        NEXT_FILES[:] = [MODEL, MODEL]
        prepare._add_models()
        prepare._slice()
        await until(lambda: prepare.result_path, "Ender slicing", timeout=300)
        assert prepare.result_path.endswith(".gcode"), prepare.result_path
        print("Ender slice:", prepare.result.text()[:110])
        shot("Prepare", "prepare_ender")
        prepare._save_to_drive()
        await until(lambda: any(f.endswith(".gcode") for f in os.listdir(card)), "SD save")
        print("saved to card:", os.listdir(card))

        before = len(marlin.received)
        prepare._print()
        await until(lambda: len(marlin.received) > before + 50, "USB streaming")
        await until(lambda: ctrl.status and ctrl.status.process, "print status")
        print("USB printing:", ctrl.status.process.step, ctrl.status.process.progress, "% |",
              len(marlin.received) - before, "lines sent so far")
        monitor = win.pages["Monitor"]
        await until(lambda: monitor.sd_list.count() > 0, "SD card list")
        shot("Monitor", "monitor_ender")
        await ctrl.backend.cancel()
        console = win.pages["Console"]
        console.cmd.setText("M115")
        console._send()
        await until(lambda: "FIRMWARE_NAME" in console.log.toPlainText(), "console reply")
        shot("Console", "console_ender")
        shot("Maintenance", "maintenance_ender")
        await ctrl.disconnect()
    finally:
        marlin.stop()


async def share_scenario(win, ctrl, shot):
    """Window A shares the fake MakerBot; window B joins as a client and prints via the queue."""
    os.environ.update(OPENBOT_SERVER_BIND="127.0.0.1", OPENBOT_SERVER_PORT="0",
                      OPENBOT_NO_BONJOUR="1")
    fake = await FakePrinter().start()
    printers = win.pages["Printers"]
    printers.ip.setText(f"127.0.0.1:{fake.port}")
    printers._connect()
    await until(lambda: ctrl.connected and not ctrl.remote, "A: printer connection")
    share = win.pages["Share"]
    share.share_box.setChecked(True)
    await until(lambda: share.server is not None and share.server.port, "A: sharing")
    port = share.server.port
    assert not win.nav.item(PAGES.index("Queue")).isHidden()

    ctrl_b = PrinterController()
    win_b = MainWindow(ctrl_b)
    win_b.show()
    pb = win_b.pages["Printers"]
    pb.server_addr.setText(f"127.0.0.1:{port}")
    pb._connect_server()
    await until(lambda: share.server.clients.pending_requests(), "A: sees B's request")
    req = share.server.clients.pending_requests()[0]
    share.refresh()
    share.pending.selectRow(0)
    share.role.setCurrentIndex(share.role.findData(int(Role.OPERATOR)))
    share._decide(True)
    await until(lambda: ctrl_b.connected and ctrl_b.remote, "B: connected via server")
    print("B joined", ctrl_b.backend.server_name, "as", ctrl_b.backend.role.name,
          "| code was", req["code"], "| B's pages:",
          [win_b.nav.item(i).text() for i in range(win_b.nav.count())
           if not win_b.nav.item(i).isHidden()])
    assert ctrl_b.machine_id == "replicator_plus"
    shot_b = lambda name, fn: (win_b.show_page(name), win_b.grab().save(
        os.path.join(OUT, fn + ".png")))
    prep_b = win_b.pages["Prepare"]
    NEXT_FILES[:] = [MODEL]
    prep_b._add_models()
    prep_b._slice()
    await until(lambda: prep_b.result_path, "B: slicing", timeout=300)
    prep_b._print()                                    # remote -> sends to the queue
    queue_b = win_b.pages["Queue"]
    await until(lambda: any(j["state"] == "waiting_for_plate" for j in queue_b.jobs),
                "B: job waiting for plate")
    shot_b("Queue", "queue_client")
    queue_b.table.selectRow(0)
    queue_b._confirm()
    await until(lambda: fake.files, "printer received the job")
    await until(lambda: any(j["state"] == "printing" for j in queue_b.jobs), "B: printing")
    shot("Share", "share_server")
    print("shared print:", [(j["name"], j["state"]) for j in queue_b.jobs], "| printer got",
          list(fake.files))
    await ctrl_b.disconnect()
    share.share_box.setChecked(False)
    await until(lambda: share.server is None, "A: stop sharing")
    await ctrl.disconnect()
    await fake.stop()


def main():
    os.makedirs(OUT, exist_ok=True)
    patch_dialogs()
    app = QApplication(sys.argv[:1])
    app.setOrganizationName("OpenBot-smoke")
    app.setApplicationName("OpenBot-smoke")
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    ctrl = PrinterController()
    win = MainWindow(ctrl)
    win.show()
    with loop:
        loop.run_until_complete(scenario(win, ctrl))


if __name__ == "__main__":
    main()
