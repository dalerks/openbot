"""Marlin serial protocol: numbered, checksummed G-code lines with ok/resend flow control.

One command is in flight at a time (the same "ping-pong" approach OctoPrint uses by
default): send `N<n> <cmd>*<checksum>`, wait for `ok`. The firmware may interject:

  busy: processing          still working (G28, M109, ...) - keep waiting
  Resend: N / rs N          line N arrived garbled - send it again
  Error:checksum mismatch   (always followed by Resend)
  Error:Printer halted ...  fatal (thermal runaway, MINTEMP, M112) - stop everything
  T:21.3 /0.0 B:20.9 /0.0   temperature report (reply to M105 or M155 auto-report)
  SD printing byte 12/345   SD progress (M27 / auto-report)

Serial I/O runs in a thread; results are delivered to the asyncio loop.
"""

import asyncio
import logging
import re
import threading
import time

log = logging.getLogger(__name__)

TEMP_RE = re.compile(r"(?:^|\s)(T\d?|B|C):\s*(-?[\d.]+)\s*(?:/\s*(-?[\d.]+))?")
RESEND_RE = re.compile(r"(?:Resend|rs)[:\s]+N?(\d+)", re.I)
LONG_COMMANDS = ("G28", "G29", "M109", "M190", "M191", "M303", "M400", "G4", "M600", "M1")


class MarlinError(RuntimeError):
    pass


class PrinterHalted(MarlinError):
    """The firmware stopped (thermal protection, M112, kill()). Needs a reset."""


def checksum(line):
    cs = 0
    for ch in line.encode("ascii", "replace"):
        cs ^= ch
    return cs


def numbered(n, cmd):
    body = f"N{n} {cmd}"
    return f"{body}*{checksum(body)}"


def parse_temps(line):
    """'ok T:200.0 /200.0 B:60.0 /60.0 @:127' -> {'T': (200.0, 200.0), 'B': (60.0, 60.0)}"""
    temps = {}
    for key, cur, target in TEMP_RE.findall(line):
        key = "T" if key in ("T", "T0") else key
        temps.setdefault(key, (float(cur), float(target) if target else None))
    return temps


class MarlinConnection:
    def __init__(self, ser):
        self._ser = ser
        self._loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        self._line_no = 0
        self._history = {}                 # line number -> numbered text, for resends
        self._pending = None               # {"n": int, "lines": [], "future": Future}
        self._listeners = []
        self._close_listeners = []
        self._last_activity = time.monotonic()
        self.halted = None
        self.closed = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reader, name="marlin-reader",
                                         daemon=True)
        self._thread.start()

    @classmethod
    async def open(cls, port, baud=115200, *, boot_wait=2.5, factory=None):
        """Open the port; many boards reset when opened, so wait for the boot chatter."""
        import serial
        factory = factory or (lambda: serial.serial_for_url(port, baud, timeout=0.1,
                                                             write_timeout=5))
        try:
            ser = await asyncio.to_thread(factory)
        except Exception as e:  # noqa: BLE001 - SerialException and OS errors
            raise MarlinError(f"can't open {port}: {e}") from e
        conn = cls(ser)
        await conn._settle(boot_wait)
        await conn.reset_line_numbers()
        return conn

    async def _settle(self, max_wait):
        """Wait until the board stops printing boot messages (or max_wait passes)."""
        start = time.monotonic()
        while time.monotonic() - start < max_wait:
            await asyncio.sleep(0.1)
            if time.monotonic() - self._last_activity > 0.8 and \
                    time.monotonic() - start > 0.5:
                break

    # ------------------------------------------------------------ listeners

    def on_line(self, callback):
        """callback(line) for every line the printer sends. Returns unsubscribe."""
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def on_close(self, callback):
        self._close_listeners.append(callback)

    # ------------------------------------------------------------ sending

    async def reset_line_numbers(self):
        self._line_no = 0
        self._history.clear()
        await self.send("M110 N0", _number=0)

    async def send(self, cmd, timeout=None, *, _number=None):
        """Send one command; returns the lines the printer sent before its `ok`."""
        cmd = cmd.split(";", 1)[0].strip()
        if not cmd:
            return []
        if self.halted:
            raise PrinterHalted(self.halted)
        if self.closed:
            raise MarlinError("connection is closed")
        if timeout is None:
            timeout = 900 if cmd.split()[0].upper() in LONG_COMMANDS else 15
        async with self._lock:
            if _number is None:
                self._line_no += 1
                n = self._line_no
            else:
                n = self._line_no = _number
            text = numbered(n, cmd)
            self._history[n] = text
            self._history.pop(n - 100, None)
            fut = self._loop.create_future()
            self._pending = {"n": n, "lines": [], "future": fut, "deadline":
                             time.monotonic() + timeout, "timeout": timeout}
            await self._write(text)
            try:
                while True:
                    remaining = self._pending["deadline"] - time.monotonic()
                    if remaining <= 0:
                        raise MarlinError(f"no reply to '{cmd}' within {timeout:.0f}s")
                    try:
                        return await asyncio.wait_for(asyncio.shield(fut),
                                                      min(remaining, 1.0))
                    except asyncio.TimeoutError:
                        continue          # re-check the deadline ("busy:" extends it)
            finally:
                self._pending = None

    async def send_raw(self, text):
        """Unnumbered, no wait. For M112 (the emergency parser handles it immediately)."""
        await self._write(text)

    async def _write(self, text):
        log.debug(">> %s", text)
        data = (text + "\n").encode("ascii", "replace")
        await asyncio.to_thread(self._ser.write, data)

    # ------------------------------------------------------------ reading

    def _reader(self):
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except Exception as e:  # noqa: BLE001 - unplugged cable etc.
                self._loop.call_soon_threadsafe(self._closed, f"serial error: {e}")
                return
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii", "replace").strip()
                if line:
                    self._loop.call_soon_threadsafe(self._handle, line)

    def _handle(self, line):
        self._last_activity = time.monotonic()
        log.debug("<< %s", line)
        p = self._pending
        low = line.lower()
        if "printer halted" in low or "kill() called" in low or \
                low.startswith("error:thermal runaway") or "!!" == line[:2]:
            self.halted = line
            if p and not p["future"].done():
                p["future"].set_exception(PrinterHalted(line))
        elif p and not p["future"].done():
            if m := RESEND_RE.search(line):
                # Marlin follows "Resend: N" with an "ok" that acknowledges the error,
                # not the command: swallow it, resend, and wait for the real "ok".
                n = int(m.group(1))
                p["skip_oks"] = p.get("skip_oks", 0) + 1
                p["resends"] = p.get("resends", 0) + 1
                if p["resends"] > 10:
                    p["future"].set_exception(MarlinError(
                        "line kept arriving corrupted (10 resends); check the USB cable"))
                elif n in self._history:
                    self._loop.create_task(self._write(self._history[n]))
            elif low.startswith("busy:") or low.startswith("echo:busy"):
                p["deadline"] = time.monotonic() + p["timeout"]
            elif low.startswith("ok"):
                if p.get("skip_oks"):
                    p["skip_oks"] -= 1
                else:
                    p["future"].set_result(p["lines"] + ([line] if len(line) > 2 else []))
            elif not low.startswith("error:"):
                p["lines"].append(line)
        for cb in list(self._listeners):
            try:
                cb(line)
            except Exception:  # noqa: BLE001
                log.exception("line listener failed")

    def _closed(self, reason):
        if self.closed:
            return
        self.closed = True
        p = self._pending
        if p and not p["future"].done():
            p["future"].set_exception(MarlinError(reason))
        for cb in list(self._close_listeners):
            try:
                cb(reason)
            except Exception:  # noqa: BLE001
                log.exception("close listener failed")

    async def close(self):
        self._stop.set()
        await asyncio.to_thread(self._thread.join, 2)
        try:
            await asyncio.to_thread(self._ser.close)
        except Exception:  # noqa: BLE001
            pass
        self._closed("closed")
