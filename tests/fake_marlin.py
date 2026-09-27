"""A simulated Marlin 2.x printer (Ender-3 Pro) on a pseudo-terminal, for tests.

Validates line numbers and checksums exactly like the firmware, answers the
commands OpenBot uses, reports temperatures, and has an SD card with files.
`corrupt_lines` makes chosen line numbers "arrive garbled" once, to exercise resends.
"""

import os
import re
import select
import threading
import time
import tty

from openbot.marlin.connection import checksum

LINE_RE = re.compile(r"^N(\d+)\s+(.*)\*(\d+)$")


class FakeMarlin:
    def __init__(self, corrupt_lines=(), autoreport=True, sd_files=None, speedup=20.0):
        self.master, self.slave = os.openpty()
        tty.setraw(self.master)
        self.port = os.ttyname(self.slave)
        self.last_line = 0
        self.received = []                 # commands accepted, in order
        self.resends = 0
        self.corrupt = set(corrupt_lines)
        self.autoreport = autoreport
        self.speedup = speedup
        self.temp = {"T": [25.0, 0.0], "B": [24.0, 0.0]}
        self.sd_files = sd_files if sd_files is not None else [
            ("CUBE~1.GCO", 123456, "cube_pla.gcode"), ("BENCHY.GCO", 999999, None)]
        self.sd_printing = None            # [name, done, total, paused]
        self.halted = False
        self._report_interval = 0
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ticker = threading.Thread(target=self._tick, daemon=True)

    def start(self):
        self._write("start\necho:Marlin 2.0.9.3\necho: Last Updated: 2026-01-01\n")
        self._thread.start()
        self._ticker.start()
        return self

    def stop(self):
        # Close only after the reader thread has exited: on macOS, closing a pty
        # master while another thread is blocked reading it can hang forever.
        self._stop.set()
        self._thread.join(2)
        self._ticker.join(2)
        for fd in (self.slave, self.master):
            try:
                os.close(fd)
            except OSError:
                pass

    # ------------------------------------------------------------ io

    def _write(self, text):
        with self._write_lock:
            try:
                os.write(self.master, text.encode())
            except OSError:
                pass

    def _run(self):
        buf = b""
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.master], [], [], 0.1)
                if not ready:
                    continue
                chunk = os.read(self.master, 1024)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._line(raw.decode("ascii", "replace").strip())

    def _tick(self):
        while not self._stop.is_set():
            time.sleep(0.05)
            for k in ("T", "B"):
                cur, target = self.temp[k]
                self.temp[k][0] = cur + (target - cur) * 0.5
            if self.autoreport and self._report_interval and not self.halted:
                self._report_due = getattr(self, "_report_due", 0) - 0.05
                if self._report_due <= 0:
                    self._report_due = self._report_interval / self.speedup
                    self._write(" " + self._temps() + "\n")
            if self.sd_printing and getattr(self, "_sd_report", 0):
                self._sd_due = getattr(self, "_sd_due", 0) - 0.05
                if self._sd_due <= 0:
                    self._sd_due = self._sd_report / self.speedup
                    self._write(f"SD printing byte {self.sd_printing[1]}/"
                                f"{self.sd_printing[2]}\n")
            if self.sd_printing and not self.sd_printing[3]:
                self.sd_printing[1] = min(self.sd_printing[2],
                                          self.sd_printing[1] + self.sd_printing[2] // 20)
                if self.sd_printing[1] >= self.sd_printing[2]:
                    self._write("Done printing file\n")
                    self.sd_printing = None

    def _temps(self):
        t, b = self.temp["T"], self.temp["B"]
        return f"T:{t[0]:.2f} /{t[1]:.2f} B:{b[0]:.2f} /{b[1]:.2f} @:0 B@:0"

    # ------------------------------------------------------------ protocol

    def _line(self, line):
        if not line:
            return
        if line == "M112":                                   # emergency parser, unnumbered
            self.halted = True
            self._write("Error:Printer halted. kill() called!\n")
            return
        if self.halted:
            return
        m = LINE_RE.match(line)
        if not m:
            self._write("Error:No Line Number with checksum, Last Line: "
                        f"{self.last_line}\nResend: {self.last_line + 1}\nok\n")
            return
        n, cmd, cs = int(m.group(1)), m.group(2), int(m.group(3))
        body = line[:line.rindex("*")]
        if n in self.corrupt:
            self.corrupt.discard(n)
            cs = -1                                          # pretend it arrived garbled
        if cs != checksum(body):
            self.resends += 1
            self._write(f"Error:checksum mismatch, Last Line: {self.last_line}\n"
                        f"Resend: {self.last_line + 1}\nok\n")
            return
        if cmd.startswith("M110"):
            self.last_line = n
            self._write("ok\n")
            return
        if n != self.last_line + 1:
            self.resends += 1
            self._write(f"Error:Line Number is not Last Line Number+1, Last Line: "
                        f"{self.last_line}\nResend: {self.last_line + 1}\nok\n")
            return
        self.last_line = n
        self.received.append(cmd)
        self._write(self._respond(cmd))

    def _respond(self, cmd):
        parts = cmd.split()
        code = parts[0].upper()
        args = {p[0].upper(): p[1:] for p in parts[1:] if p}
        if code == "M115":
            return ("FIRMWARE_NAME:Marlin 2.0.9.3 (Sep 23 2026) SOURCE_CODE_URL:github.com/"
                    "MarlinFirmware/Marlin PROTOCOL_VERSION:1.0 MACHINE_TYPE:Ender-3 Pro "
                    "EXTRUDER_COUNT:1 UUID:cede2a2f-41a2-4748-9b12-c55c62f367ff\n"
                    f"Cap:AUTOREPORT_TEMP:{1 if self.autoreport else 0}\n"
                    f"Cap:AUTOREPORT_SD_STATUS:{1 if self.autoreport else 0}\n"
                    "Cap:EMERGENCY_PARSER:1\nCap:LONG_FILENAME:1\nok\n")
        if code == "M105":
            return f"ok {self._temps()}\n"
        if code == "M155":
            self._report_interval = float(args.get("S", 0) or 0)
            return "ok\n"
        if code in ("M104", "M140"):
            self.temp["T" if code == "M104" else "B"][1] = float(args.get("S", 0))
            return "ok\n"
        if code in ("M109", "M190", "G28", "G29"):
            if code in ("M109", "M190"):
                k = "T" if code == "M109" else "B"
                self.temp[k][1] = float(args.get("S", args.get("R", 0)))
                self.temp[k][0] = self.temp[k][1]
            time.sleep(0.05)
            return "echo:busy: processing\nok\n"
        if code == "M21":
            return "echo:SD card ok\nok\n"
        if code == "M20":
            lines = ["Begin file list"]
            for short, size, long_name in self.sd_files:
                lines.append(f"{short} {size}" + (f" {long_name}" if "L" in args and
                                                   long_name else ""))
            lines.append("End file list")
            return "\n".join(lines) + "\nok\n"
        if code == "M23":
            name = cmd.split(None, 1)[1]
            match = [f for f in self.sd_files if f[0] == name]
            if not match:
                return f"echo:open failed, File: {name}.\nok\n"
            self._selected = match[0]
            return (f"echo:Now fresh file: {name}\nFile opened: {name} Size: {match[0][1]}\n"
                    "File selected\nok\n")
        if code == "M24":
            if self.sd_printing:
                self.sd_printing[3] = False
            elif getattr(self, "_selected", None):
                self.sd_printing = [self._selected[0], 0, self._selected[1], False]
            return "ok\n"
        if code == "M25":
            if self.sd_printing:
                self.sd_printing[3] = True
            return "ok\n"
        if code == "M524":
            self.sd_printing = None
            return "echo:Print aborted\nok\n"
        if code == "M27":
            if "S" in args:
                self._sd_report = float(args["S"] or 0)
                return "ok\n"
            if self.sd_printing:
                return f"SD printing byte {self.sd_printing[1]}/{self.sd_printing[2]}\nok\n"
            return "Not SD printing\nok\n"
        return "ok\n"
