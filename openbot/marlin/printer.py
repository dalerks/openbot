"""A Marlin printer (Creality Ender-3 family) on a USB serial port."""

import asyncio
import logging
import os
import re
import time

from .. import machines
from ..printer.errors import PrinterError
from ..printer.models import Extruder, PrinterInfo, PrinterStatus, Process
from .connection import MarlinConnection, MarlinError, PrinterHalted, parse_temps

log = logging.getLogger(__name__)

SD_PROGRESS_RE = re.compile(r"SD printing byte (\d+)/(\d+)", re.I)
FIRMWARE_RE = re.compile(r"FIRMWARE_NAME:(.*?)(?:\s+\w+:|$)")
MACHINE_TYPE_RE = re.compile(r"MACHINE_TYPE:(.*?)(?:\s+[A-Z_]+:|$)")
SD_FILE_RE = re.compile(r"^(\S+)\s+(\d+)(?:\s+(.*))?$")


class MarlinPrinter:
    def __init__(self, port, machine_id="ender3_pro", baud=None):
        self.port = port
        self.machine = machines.get(machine_id)
        self.baud = baud or self.machine.baud
        self.conn: MarlinConnection | None = None
        self.info: PrinterInfo | None = None
        self.capabilities = {}
        self.temps = {}
        self._status_cbs = []
        self._close_cbs = []
        self._job = None             # asyncio.Task streaming a file
        self._job_state = None       # dict describing the current process
        self._paused = asyncio.Event()
        self._paused.set()           # set = running
        self._poll_task = None
        self.sd_files = []

    # ------------------------------------------------------------ lifecycle

    async def connect(self, factory=None):
        try:
            self.conn = await MarlinConnection.open(self.port, self.baud, factory=factory)
            self.conn.on_line(self._on_line)
            self.conn.on_close(self._on_close)
            lines = await self.conn.send("M115")
        except MarlinError as e:
            if self.conn:
                await self.conn.close()
            raise PrinterError(f"{self.port}: {e}") from e
        text = " ".join(lines)
        fw = FIRMWARE_RE.search(text)
        mt = MACHINE_TYPE_RE.search(text)
        for line in lines:
            if line.startswith("Cap:"):
                key, _, val = line[4:].partition(":")
                self.capabilities[key] = val.strip() == "1"
        guessed = machines.for_marlin_machine_type(mt.group(1) if mt else "")
        if guessed and guessed != self.machine.id:
            log.info("printer reports %s; keeping user-selected %s",
                     mt.group(1), self.machine.name)
        self.info = PrinterInfo(
            serial=f"usb:{os.path.basename(self.port)}",
            name=(mt.group(1).strip() if mt else self.machine.name),
            ip=None, machine_type="marlin", bot_type=self.machine.id,
            firmware=(fw.group(1).strip() if fw else "Marlin"), source="usb", raw={"M115": lines})
        if self.capabilities.get("AUTOREPORT_TEMP"):
            await self.conn.send("M155 S2")
        else:
            self._poll_task = asyncio.ensure_future(self._poll_temps())
        await self._refresh_temps()
        return self

    @property
    def connected(self):
        return self.conn is not None and not self.conn.closed

    async def close(self):
        if self._poll_task:
            self._poll_task.cancel()
        if self._job and not self._job.done():
            self._job.cancel()
        if self.conn:
            try:
                if not self.conn.halted and not self.conn.closed:
                    await asyncio.wait_for(self.conn.send("M155 S0"), 2)
            except Exception:  # noqa: BLE001
                pass
            await self.conn.close()

    def on_status(self, callback):
        self._status_cbs.append(callback)
        return lambda: self._status_cbs.remove(callback)

    def on_disconnect(self, callback):
        self._close_cbs.append(callback)

    def _on_close(self, reason):
        for cb in list(self._close_cbs):
            cb(reason)

    # ------------------------------------------------------------ status

    def _on_line(self, line):
        changed = False
        temps = parse_temps(line)
        if temps:
            self.temps.update(temps)
            changed = True
        if m := SD_PROGRESS_RE.search(line):
            done, total = int(m.group(1)), int(m.group(2))
            if self._job_state and self._job_state["name"] == "SDPrint":
                self._job_state["progress"] = int(done * 100 / total) if total else 0
                self._job_state["step"] = "printing" if self._paused.is_set() else "suspended"
                changed = True
        elif "done printing file" in line.lower() and self._job_state:
            self._job_state.update(step="completed", progress=100, complete=True)
            changed = True
        elif "not sd printing" in line.lower() and self._job_state and \
                self._job_state["name"] == "SDPrint" and self._paused.is_set() and \
                self._job_state.get("started") and \
                time.monotonic() - self._job_state["started"] > 5:
            self._job_state.update(step="completed", complete=True)
            changed = True
        if self.conn and self.conn.halted and self._job_state:
            self._job_state.update(step="failed", error={"message": self.conn.halted},
                                   complete=True)
            changed = True
        if changed:
            self._emit()

    def status(self):
        t_cur, t_target = self.temps.get("T", (0.0, 0.0))
        bed = self.temps.get("B")
        ext = Extruder(index=0, tool_id=None, present=True,
                       current_temperature=t_cur, target_temperature=t_target or 0.0,
                       filament_present=None, preheating=False, error=0)
        proc = None
        if self._job_state:
            j = self._job_state
            elapsed = time.monotonic() - j["started"] if j.get("started") else 0
            proc = Process(name=j["name"], step=j["step"], progress=j.get("progress"),
                           cancellable=not j.get("complete"), complete=j.get("complete", False),
                           cancelled=j.get("cancelled", False), error=j.get("error"),
                           filename=j.get("filename"), elapsed_time=elapsed,
                           username=None, methods=[], raw=dict(j))
        return PrinterStatus(name=self.info.name if self.info else self.machine.name,
                             machine_type="marlin", bot_type=self.machine.id, ip=None,
                             firmware=self.info.firmware if self.info else "",
                             extruders=[ext], process=proc, sound=None,
                             raw={"temps": dict(self.temps)},
                             bed=(bed[0], bed[1] or 0.0) if bed else None)

    def _emit(self):
        st = self.status()
        for cb in list(self._status_cbs):
            try:
                cb(st)
            except Exception:  # noqa: BLE001
                log.exception("status listener failed")

    async def _refresh_temps(self):
        lines = await self.conn.send("M105")
        for line in lines:
            self._on_line(line)
        self._emit()

    async def _poll_temps(self):
        while self.connected:
            await asyncio.sleep(2)
            try:
                await self._refresh_temps()
            except MarlinError:
                return

    # ------------------------------------------------------------ USB streaming print

    def _busy(self):
        return self._job_state is not None and not self._job_state.get("complete")

    async def start_print(self, path, *, progress=None, plate_cleared=True):
        """Stream a G-code file over USB, one checksummed line at a time."""
        if self._busy():
            raise PrinterError(f"printer is busy ({self._job_state['step']})")
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [l.split(";", 1)[0].strip() for l in f]
        lines = [l for l in lines if l]
        if not lines:
            raise PrinterError("the file has no G-code commands")
        self._paused.set()
        self._job_state = {"name": "PrintProcess", "step": "printing", "progress": 0,
                           "filename": os.path.basename(path), "started": time.monotonic(),
                           "total": len(lines), "sent": 0}
        self._job = asyncio.ensure_future(self._stream(lines, progress))
        self._emit()

    async def _stream(self, lines, progress):
        j = self._job_state
        try:
            for i, line in enumerate(lines):
                if not self._paused.is_set():
                    j["step"] = "suspended"
                    self._emit()
                    await self._paused.wait()
                    j["step"] = "printing"
                cmd = line.split()[0].upper()
                if cmd in ("M109", "M190"):
                    j["step"] = "heating"
                    self._emit()
                await self.conn.send(line)
                if cmd in ("M109", "M190"):
                    j["step"] = "printing"
                j["sent"] = i + 1
                pct = int((i + 1) * 100 / len(lines))
                if pct != j["progress"]:
                    j["progress"] = pct
                    self._emit()
                if progress:
                    progress(i + 1, len(lines))
            j.update(step="completed", progress=100, complete=True)
        except asyncio.CancelledError:
            j.update(step="cancelled", cancelled=True, complete=True)
            raise
        except PrinterHalted as e:
            j.update(step="failed", error={"message": str(e)}, complete=True)
        except MarlinError as e:
            j.update(step="failed", error={"message": str(e)}, complete=True)
        finally:
            self._emit()

    async def pause(self):
        if self._job_state and self._job_state["name"] == "SDPrint":
            await self.conn.send("M25")
            self._paused.clear()
            self._job_state["step"] = "suspended"
            self._emit()
        else:
            self._paused.clear()

    async def resume(self):
        if self._job_state and self._job_state["name"] == "SDPrint":
            await self.conn.send("M24")
            self._job_state["step"] = "printing"
        self._paused.set()
        self._emit()

    async def cancel(self):
        """Stop the job, turn heaters off, lift the nozzle clear of the print."""
        j = self._job_state
        if j and j["name"] == "SDPrint":
            try:
                await self.conn.send("M524")       # abort SD print (Marlin 2.x)
            except MarlinError:
                await self.conn.send("M25")        # older firmware: at least pause it
        if self._job and not self._job.done():
            self._job.cancel()
            try:
                await self._job
            except asyncio.CancelledError:
                pass
        self._paused.set()
        for cmd in ("M104 S0", "M140 S0", "M107", "G91", "G1 Z10 F600", "G90", "M84"):
            await self.conn.send(cmd)
        if j:
            j.update(step="cancelled", cancelled=True, complete=True)
        self._emit()

    async def acknowledge(self):
        """Clear a finished/cancelled job from the status display."""
        if self._job_state and self._job_state.get("complete"):
            self._job_state = None
            self._emit()

    # ------------------------------------------------------------ printer's SD card

    async def sd_list(self):
        await self.conn.send("M21")
        lines = await self.conn.send("M20 L" if self.capabilities.get("LONG_FILENAME")
                                     else "M20")
        files, inside = [], False
        for line in lines:
            low = line.lower()
            if low.startswith("begin file list"):
                inside = True
            elif low.startswith("end file list"):
                inside = False
            elif inside and (m := SD_FILE_RE.match(line)):
                short, size, long_name = m.group(1), int(m.group(2)), m.group(3)
                files.append({"name": short, "size": size, "display": long_name or short})
        self.sd_files = files
        return files

    async def sd_print(self, name):
        if self._busy():
            raise PrinterError(f"printer is busy ({self._job_state['step']})")
        await self.conn.send(f"M23 {name}")
        await self.conn.send("M24")
        self._paused.set()
        self._job_state = {"name": "SDPrint", "step": "printing", "progress": 0,
                           "filename": name, "started": time.monotonic()}
        if self.capabilities.get("AUTOREPORT_SD_STATUS"):
            await self.conn.send("M27 S3")
        else:
            asyncio.ensure_future(self._poll_sd())
        self._emit()

    async def _poll_sd(self):
        while self.connected and self._busy() and self._job_state["name"] == "SDPrint":
            await asyncio.sleep(3)
            try:
                for line in await self.conn.send("M27"):
                    self._on_line(line)
            except MarlinError:
                return

    # ------------------------------------------------------------ manual control

    async def preheat(self, nozzle=None, bed=None):
        nozzle = nozzle if nozzle is not None else 200
        bed = bed if bed is not None else 60
        if nozzle > self.machine.max_nozzle_temp:
            raise PrinterError(f"{nozzle} °C is above the {self.machine.name}'s "
                               f"{self.machine.max_nozzle_temp} °C limit")
        await self.conn.send(f"M104 S{int(nozzle)}")
        await self.conn.send(f"M140 S{int(bed)}")
        await self._refresh_temps()

    async def cool(self):
        await self.conn.send("M104 S0")
        await self.conn.send("M140 S0")
        await self._refresh_temps()

    async def home(self):
        await self.conn.send("G28")

    async def motors_off(self):
        await self.conn.send("M84")

    async def send_gcode(self, command):
        """Console: send one command and return the printer's reply lines."""
        if self._busy() and self._job_state["name"] == "PrintProcess":
            raise PrinterError("a USB print is streaming; wait or pause it first")
        return await self.conn.send(command)

    async def emergency_stop(self):
        """M112: the firmware halts immediately; the printer must be reset afterwards."""
        await self.conn.send_raw("M112")
