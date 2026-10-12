"""A Klipper printer behind Moonraker (the API Mainsail and Fluidd use), e.g. an Ender-3
driven by a Raspberry Pi.

HTTP for commands and uploads, one websocket for live status and console output.
Files are uploaded to the Pi and printed from there, so this Mac can sleep mid-print.
"""

import asyncio
import itertools
import json
import logging
import os
import struct
from urllib.parse import urlsplit

import aiohttp

from .. import machines
from ..printer.errors import PrinterError
from ..printer.models import (CAMERA_FORMAT_JPEG, CameraFrame, Extruder, PrinterInfo,
                              PrinterStatus, Process)

log = logging.getLogger(__name__)

# Klipper objects whose fields make up the status.
SUBSCRIBE = {"print_stats": None, "virtual_sdcard": ["progress", "is_active"],
             "display_status": ["progress", "message"], "webhooks": None,
             "extruder": ["temperature", "target"], "heater_bed": ["temperature", "target"]}
BUSY = ("printing", "paused")


def base_url(address):
    """'192.168.2.222', 'pi.local:7125' or 'http://pi/viewer' -> 'http://host[:port]'."""
    address = address.strip()
    if "://" not in address:
        address = "http://" + address
    u = urlsplit(address)
    if not u.hostname:
        raise PrinterError(f"not a printer address: {address!r}")
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    return f"{u.scheme}://{host}{f':{u.port}' if u.port else ''}"


def jpeg_size(data):
    """(width, height) from a JPEG's SOF marker, or (0, 0)."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    return 0, 0


class MoonrakerPrinter:
    def __init__(self, address, machine_id="ender3_pro", *, username=None, password=None):
        self.base = base_url(address)
        self.host = urlsplit(self.base).hostname
        self.machine = machines.get(machine_id)
        self._username, self._password = username, password
        self._token = None
        self._session: aiohttp.ClientSession | None = None
        self._ws = None
        self._reader = None
        self._ids = itertools.count(1)
        self._pending = {}
        self._objects = {}          # merged Klipper object state
        self._status_cbs, self._close_cbs, self._line_cbs = [], [], []
        self._captures = []         # lists collecting console lines for send_gcode
        self._acknowledged = None   # (filename, state) the user dismissed
        self.info: PrinterInfo | None = None
        self.webcam = None
        self.has_probe = False
        self.closed = False

    # ------------------------------------------------------------ lifecycle

    async def connect(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None,
                                                                            sock_connect=8))
        try:
            access = await self._get("/access/info")
            if access.get("login_required") and not access.get("trusted"):
                await self._login()
            server = await self._get("/server/info")
            try:
                pinfo = await self._get("/printer/info")
            except PrinterError:          # Klipper not running yet; Moonraker still answers
                pinfo = {}
            cams = (await self._get("/server/webcams/list")).get("webcams", [])
            self.webcam = next((c for c in cams if c.get("enabled", True)
                                and c.get("snapshot_url")), None)
            await self._open_ws()
        except BaseException:
            await self._session.close()
            raise
        hostname = pinfo.get("hostname") or self.host
        self.info = PrinterInfo(
            serial=f"moonraker:{self.host}", name=hostname, ip=self.host,
            machine_type="klipper", bot_type=self.machine.id,
            firmware=f"Klipper {pinfo.get('software_version', '?')}".split("-g")[0],
            source="moonraker", raw={"server": server, "printer": pinfo})
        if server.get("klippy_state") == "ready":
            await self._subscribe()
            objects = (await self._get("/printer/objects/list")).get("objects", [])
            self.has_probe = any(o in ("probe", "bltouch") or o.startswith("probe_eddy")
                                 for o in objects)
        return self

    async def _login(self):
        if not (self._username and self._password):
            raise PrinterError("this Moonraker needs a username and password")
        r = await self._post("/access/login", json={
            "username": self._username, "password": self._password, "source": "moonraker"})
        self._token = r["token"]

    async def _open_ws(self):
        url = self.base.replace("http", "ws", 1) + "/websocket"
        if self._token:
            url += "?token=" + (await self._get("/access/oneshot_token"))
        try:
            self._ws = await self._session.ws_connect(url, heartbeat=30)
        except aiohttp.ClientError as e:
            raise PrinterError(f"{self.host}: live connection refused: {e}") from e
        self._reader = asyncio.ensure_future(self._read())
        from .. import __version__
        await self._call("server.connection.identify", {
            "client_name": "OpenBot", "version": __version__, "type": "desktop",
            "url": "https://github.com/dalerks/openbot"})

    async def _subscribe(self):
        r = await self._call("printer.objects.subscribe", {"objects": SUBSCRIBE})
        self._objects = r.get("status", {})
        self._emit()

    @property
    def connected(self):
        return self._ws is not None and not self._ws.closed and not self.closed

    async def close(self):
        self.closed = True
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    def on_status(self, callback):
        self._status_cbs.append(callback)
        return lambda: self._status_cbs.remove(callback)

    def on_disconnect(self, callback):
        self._close_cbs.append(callback)

    def on_line(self, callback):
        """Every console line Klipper prints (the Mainsail console)."""
        self._line_cbs.append(callback)

    # ------------------------------------------------------------ transport

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def _request(self, method, path, **kwargs):
        try:
            async with self._session.request(method, self.base + path,
                                             headers=self._headers(), **kwargs) as resp:
                text = await resp.text()
                if resp.status == 401 and self._password and "/access/" not in path:
                    await self._login()             # token expired: log in once more
                    return await self._request(method, path, **kwargs)
                try:
                    data = json.loads(text)
                except ValueError:
                    data = {}
                if resp.status >= 400:
                    msg = (data.get("error") or {}).get("message") or text[:200]
                    raise PrinterError(f"{self.host}: {msg.strip()}")
                return data.get("result", data)
        except aiohttp.ClientError as e:
            raise PrinterError(f"can't reach {self.host}: {e}") from e
        except asyncio.TimeoutError as e:
            raise PrinterError(f"{self.host} didn't answer") from e

    async def _get(self, path, **params):
        return await self._request("GET", path, params=params or None)

    async def _post(self, path, **kwargs):
        return await self._request("POST", path, **kwargs)

    async def _call(self, method, params=None, timeout=15):
        if not self.connected:
            raise PrinterError("not connected")
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._ws.send_json({"jsonrpc": "2.0", "method": method,
                                  "params": params or {}, "id": rid})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            raise PrinterError(f"{method}: no answer") from e
        finally:
            self._pending.pop(rid, None)

    async def _read(self):
        reason = "closed"
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle(json.loads(msg.data))
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    reason = str(self._ws.exception())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("moonraker reader failed")
            reason = repr(e)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(PrinterError(f"connection lost ({reason})"))
        if not self.closed:
            for cb in list(self._close_cbs):
                cb(reason)

    def _handle(self, data):
        rid = data.get("id")
        if rid is not None:
            fut = self._pending.get(rid)
            if fut and not fut.done():
                if "error" in data:
                    fut.set_exception(PrinterError(data["error"].get("message", "error")))
                else:
                    fut.set_result(data.get("result"))
            return
        method, params = data.get("method"), data.get("params") or []
        if method == "notify_status_update" and params:
            for obj, fields in params[0].items():
                self._objects.setdefault(obj, {}).update(fields)
            self._emit()
        elif method == "notify_gcode_response":
            for line in params:
                for cap in self._captures:
                    cap.append(line)
                for cb in list(self._line_cbs):
                    cb(line)
        elif method == "notify_klippy_ready":
            asyncio.ensure_future(self._resubscribe())
        elif method in ("notify_klippy_shutdown", "notify_klippy_disconnected"):
            self._objects.setdefault("webhooks", {}).update(
                state="shutdown" if method.endswith("shutdown") else "startup",
                state_message="Klipper shut down" if method.endswith("shutdown")
                else "Klipper is restarting")
            self._emit()
            if method.endswith("shutdown"):
                asyncio.ensure_future(self._refresh_webhooks())

    async def _resubscribe(self):
        try:
            await self._subscribe()
        except PrinterError as e:
            log.warning("resubscribe failed: %s", e)

    async def _refresh_webhooks(self):
        try:
            r = await self._get("/printer/objects/query", webhooks="")
            self._objects["webhooks"] = r["status"]["webhooks"]
            self._emit()
        except (PrinterError, KeyError):
            pass

    # ------------------------------------------------------------ status

    def _obj(self, name):
        return self._objects.get(name) or {}

    @property
    def klippy_state(self):
        return self._obj("webhooks").get("state", "startup")

    def _process(self):
        hooks = self._obj("webhooks")
        if hooks.get("state") in ("shutdown", "error"):
            return Process(name="Klipper", step="failed", progress=None, cancellable=False,
                           complete=False, cancelled=False,
                           error={"message": hooks.get("state_message", "")},
                           filename=None, elapsed_time=None, username=None, methods=[],
                           raw=dict(hooks))
        ps = self._obj("print_stats")
        state, filename = ps.get("state", "standby"), ps.get("filename") or None
        if state == "standby" or self._acknowledged == (filename, state):
            return None
        sd = self._obj("virtual_sdcard")
        progress = sd.get("progress")
        if progress is None:
            progress = self._obj("display_status").get("progress")
        step = {"printing": "printing", "paused": "suspended", "complete": "completed",
                "cancelled": "cancelled", "error": "failed"}.get(state, state)
        if state == "printing" and not ps.get("print_duration"):
            step = "heating"               # Klipper counts print time from first extrusion
        return Process(
            name="PrintProcess", step=step,
            progress=None if progress is None else round(progress * 100, 1),
            cancellable=state in BUSY, complete=state not in BUSY,
            cancelled=state == "cancelled",
            error={"message": ps.get("message")} if state == "error" else None,
            filename=filename, elapsed_time=ps.get("total_duration"), username=None,
            methods=[], raw=dict(ps))

    def status(self):
        ext, bed = self._obj("extruder"), self._obj("heater_bed")
        extruder = Extruder(index=0, tool_id=None, present=True,
                            current_temperature=ext.get("temperature", 0.0),
                            target_temperature=ext.get("target", 0.0),
                            filament_present=None, preheating=False, error=0)
        return PrinterStatus(
            name=self.info.name if self.info else self.host, machine_type="klipper",
            bot_type=self.machine.id, ip=self.host,
            firmware=self.info.firmware if self.info else "Klipper",
            extruders=[extruder], process=self._process(), sound=None,
            raw={k: dict(v) for k, v in self._objects.items()},
            bed=(bed.get("temperature", 0.0), bed.get("target", 0.0)) if bed else None)

    def _emit(self):
        st = self.status()
        for cb in list(self._status_cbs):
            try:
                cb(st)
            except Exception:  # noqa: BLE001
                log.exception("status listener failed")

    def _check_ready(self):
        if self.klippy_state != "ready":
            msg = self._obj("webhooks").get("state_message") or "Klipper isn't ready"
            raise PrinterError(msg.strip().splitlines()[0])

    def _busy(self):
        return self._obj("print_stats").get("state") in BUSY

    # ------------------------------------------------------------ printing

    async def start_print(self, path, *, progress=None, plate_cleared=True):
        """Upload a G-code file to the Pi and start it."""
        self._check_ready()
        if self._busy():
            raise PrinterError("the printer is already printing")
        total = os.path.getsize(path)
        name = os.path.basename(path)

        async def chunks():
            sent = 0
            with open(path, "rb") as f:
                while chunk := f.read(256 * 1024):
                    sent += len(chunk)
                    if progress:
                        progress(sent, total)
                    yield chunk

        form = aiohttp.FormData()
        form.add_field("file", chunks(), filename=name, content_type="text/x-gcode")
        form.add_field("root", "gcodes")
        form.add_field("print", "true")
        self._acknowledged = None
        r = await self._post("/server/files/upload", data=form)
        if not r.get("print_started", True):
            raise PrinterError(f"{name} was uploaded but the print didn't start")

    async def pause(self):
        await self._post("/printer/print/pause")

    async def resume(self):
        await self._post("/printer/print/resume")

    async def cancel(self):
        await self._post("/printer/print/cancel")

    async def acknowledge(self):
        """Dismiss a finished print from the status (Klipper keeps showing it until the
        next one starts)."""
        ps = self._obj("print_stats")
        if ps.get("state") not in BUSY:
            self._acknowledged = (ps.get("filename") or None, ps.get("state"))
            self._emit()

    async def sd_list(self):
        """G-code files stored on the Pi, newest first."""
        files = await self._get("/server/files/list", root="gcodes")
        files.sort(key=lambda f: f.get("modified", 0), reverse=True)
        return [{"name": f["path"], "size": f.get("size", 0), "display": f["path"]}
                for f in files if f["path"].lower().endswith((".gcode", ".g", ".gco"))]

    async def sd_print(self, name):
        self._check_ready()
        if self._busy():
            raise PrinterError("the printer is already printing")
        self._acknowledged = None
        await self._post("/printer/print/start", params={"filename": name})

    # ------------------------------------------------------------ manual control

    async def send_gcode(self, command, timeout=None):
        """Run G-code (or a Klipper macro) and return what Klipper printed meanwhile."""
        lines = []
        self._captures.append(lines)
        try:
            await self._post("/printer/gcode/script", params={"script": command},
                             timeout=aiohttp.ClientTimeout(total=timeout))
        finally:
            await asyncio.sleep(0.05)       # responses arrive just before the reply
            self._captures.remove(lines)
        return lines

    async def preheat(self, nozzle=None, bed=None):
        nozzle = nozzle if nozzle is not None else 200
        bed = bed if bed is not None else 60
        if nozzle > self.machine.max_nozzle_temp:
            raise PrinterError(f"{nozzle} °C is above the {self.machine.name}'s "
                               f"{self.machine.max_nozzle_temp} °C limit")
        if self.machine.max_bed_temp and bed > self.machine.max_bed_temp:
            raise PrinterError(f"{bed} °C is above the {self.machine.name}'s bed limit")
        self._check_ready()
        await self.send_gcode(f"M104 S{int(nozzle)}\nM140 S{int(bed)}")

    async def cool(self):
        await self.send_gcode("M104 S0\nM140 S0")

    async def home(self):
        self._check_ready()
        await self.send_gcode("G28", timeout=180)

    async def motors_off(self):
        await self.send_gcode("M84")

    async def emergency_stop(self):
        await self._post("/printer/emergency_stop")

    async def restart_firmware(self):
        """FIRMWARE_RESTART: brings Klipper back after an emergency stop or error."""
        await self._post("/printer/firmware_restart")

    # ------------------------------------------------------------ camera

    @property
    def camera_transform(self):
        c = self.webcam or {}
        return {"flip_h": bool(c.get("flip_horizontal")),
                "flip_v": bool(c.get("flip_vertical")), "rotation": int(c.get("rotation") or 0)}

    def _cam_url(self, key):
        if not self.webcam:
            raise PrinterError("this printer has no camera set up in Mainsail")
        url = self.webcam[key]
        return url if "://" in url else self.base + "/" + url.lstrip("/")

    async def snapshot(self, timeout=10.0):
        try:
            async with self._session.get(self._cam_url("snapshot_url"),
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status != 200:
                    raise PrinterError(f"camera answered {r.status}")
                data = await r.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise PrinterError(f"camera unavailable: {e!r}") from e
        w, h = jpeg_size(data)
        return CameraFrame(w, h, CAMERA_FORMAT_JPEG, data)

    async def camera_stream(self):
        """Async iterator of JPEG CameraFrames from the MJPEG stream."""
        url = self._cam_url("stream_url")
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=8, sock_read=20)) as r:
                if r.status != 200:
                    raise PrinterError(f"camera answered {r.status}")
                async for data in _mjpeg_frames(r.content):
                    w, h = jpeg_size(data)
                    yield CameraFrame(w, h, CAMERA_FORMAT_JPEG, data)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise PrinterError(f"camera stream lost: {e!r}") from e


async def _mjpeg_frames(stream, max_frame=8 * 1024 * 1024):
    """Split a multipart/x-mixed-replace stream into JPEGs (start/end markers, so it
    doesn't depend on each server's part headers)."""
    buf = bytearray()
    while True:
        chunk = await stream.readany()
        if not chunk:
            return
        buf += chunk
        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                del buf[:-1]
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start:
                    del buf[:start]
                if len(buf) > max_frame:
                    raise PrinterError("camera frame too large")
                break
            yield bytes(buf[start:end + 2])
            del buf[:end + 2]
