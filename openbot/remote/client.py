"""RemoteBackend: drive a printer shared by an OpenBot server (PLAN.md §3.1).

It looks like any other PrinterBackend to the app, with two differences:
* start_print() uploads the file into the server's queue; it starts after an
  Operator confirms the plate is clear (confirm_plate);
* capabilities include "queue", plus whatever the server's printer supports.

The server's self-signed certificate is pinned exactly like a printer's
(TrustStore, PLAN.md §3.3), keyed by "server-<host>-<port>".
"""

import asyncio
import contextlib
import itertools
import json
import logging
import os
import re
import ssl

import aiohttp

from .. import backend as b
from ..printer.errors import CertificateChanged, NotTrusted, PrinterError
from ..printer.models import CameraFrame, PrinterInfo
from ..printer.trust import CertInfo, TrustStore, verified_context
from .codec import from_wire

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765
QUEUE = "queue"
# Positional parameter names for operations that take arguments.
PARAMS = {"wifi_scan": ["force_rescan"], "wifi_connect": ["path", "password", "name"],
          "wifi_disconnect": ["path"], "wifi_forget": ["path"],
          "get_static_ipv4": ["service_path"], "set_static_ipv4": ["service_path"],
          "use_dhcp": ["service_path"], "rename": ["name"], "load_filament": ["tool_index"],
          "unload_filament": ["tool_index"], "preheat": ["nozzle", "bed"],
          "sd_print": ["name"], "send_gcode": ["command"]}


def trust_key(host, port):
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"server-{host}-{port}")


def _unverified():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def fetch_server_certificate(host, port=DEFAULT_PORT, timeout=10):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=_unverified(), server_hostname=host),
            timeout)
    except (OSError, asyncio.TimeoutError) as e:
        raise PrinterError(f"can't reach the OpenBot server at {host}:{port}: {e!r}") from e
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        return CertInfo.from_der(der)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def server_context(host, port, trust: TrustStore):
    """A verified SSL context pinned to the trusted cert, or NotTrusted/CertificateChanged."""
    key = trust_key(host, port)
    presented = await fetch_server_certificate(host, port)
    trusted = trust.get(key)
    if trusted is None:
        raise NotTrusted(presented)
    if trusted.sha256 != presented.sha256:
        raise CertificateChanged(trusted, presented)
    return verified_context(trusted.pem)


def _session(ctx, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx), headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))


async def server_info(host, port, ctx):
    async with _session(ctx) as s:
        async with s.get(f"https://{host}:{port}/api/info") as r:
            r.raise_for_status()
            return await r.json()


async def request_pairing(host, port, ctx, name, setup_code=None):
    """Returns {"id", "code", "status"}; compare `code` with the server's screen."""
    async with _session(ctx) as s:
        async with s.post(f"https://{host}:{port}/api/pair",
                          json={"name": name, "setup_code": setup_code}) as r:
            if r.status == 403:
                raise PrinterError(await r.text())
            r.raise_for_status()
            return await r.json()


async def wait_for_approval(host, port, ctx, request_id, timeout=300, interval=1.0):
    """Poll until approved (returns {"token", "role"}) or denied/expired (raises)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with _session(ctx) as s:
        while loop.time() < deadline:
            async with s.get(f"https://{host}:{port}/api/pair/{request_id}") as r:
                data = await r.json()
            if data.get("status") == "approved" and data.get("token"):
                return data
            if data.get("status") in ("denied", "expired", "unknown"):
                raise PrinterError(f"pairing {data.get('status')}")
            await asyncio.sleep(interval)
    raise PrinterError("pairing timed out; nobody approved it on the server")


class RemoteBackend(b.PrinterBackend):
    def __init__(self, host, port, token, ctx):
        self.host, self.port, self._token, self._ctx = host, port, token, ctx
        self.base = f"https://{host}:{port}"
        self._session = None
        self._ws = None
        self._reader = None
        self._ids = itertools.count(1)
        self._pending = {}
        self._status_cbs, self._close_cbs = [], []
        self._queue_cbs, self._pairing_cbs, self._printer_cbs = [], [], []
        self.queue = []
        self.client = {}
        self.server_name = ""
        self.printer_state = {}
        self.capabilities = frozenset()
        self.machine_id = ""
        self.role = b.Role.VIEWER
        self.info = None
        self.closed = False

    # ------------------------------------------------------------ connection

    @classmethod
    async def connect(cls, host, port, token, ctx):
        self = cls(host, port, token, ctx)
        self._session = _session(ctx, token)
        try:
            self._ws = await self._session.ws_connect(f"{self.base}/api/ws", heartbeat=30)
            hello = await self._ws.receive_json(timeout=10)
        except aiohttp.WSServerHandshakeError as e:
            await self._session.close()
            if e.status == 401:
                raise PrinterError("the server no longer accepts this client; pair "
                                   "again") from e
            raise PrinterError(f"server refused the connection ({e.status})") from e
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            await self._session.close()
            raise PrinterError(f"can't connect to {host}:{port}: {e!r}") from e
        self._apply_hello(hello["data"])
        self._reader = asyncio.ensure_future(self._read())
        return self

    def _apply_hello(self, data):
        self.client = data["client"]
        self.role = b.Role(self.client["role"])
        self.server_name = data["server"]
        self.queue = data["queue"]
        self._apply_printer(data["printer"])

    def _apply_printer(self, p):
        self.printer_state = p
        # Certificate tools manage the server's own trust of its printer: not remote.
        self.capabilities = (frozenset(p.get("capabilities", [])) - {"certificate"}) | {QUEUE}
        self.machine_id = p.get("machine", "")
        self.info = PrinterInfo(serial=f"server:{self.host}:{self.port}",
                                name=p.get("name") or "Printer", ip=self.host,
                                machine_type="remote", bot_type=self.machine_id,
                                firmware=f"via {self.server_name}", source="server")

    @property
    def connected(self):
        return not self.closed and self._ws is not None and not self._ws.closed

    @property
    def printer_connected(self):
        return bool(self.printer_state.get("connected"))

    async def _read(self):
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if "id" in data and data["id"] in self._pending:
                    fut = self._pending.pop(data["id"])
                    if not fut.done():
                        if "error" in data:
                            fut.set_exception(_error(data["error"]))
                        else:
                            fut.set_result(from_wire(data.get("result")))
                    continue
                self._event(data.get("event"), data.get("data"))
        except Exception as e:  # noqa: BLE001
            log.info("server connection ended: %r", e)
        finally:
            self._closed("server connection closed")

    def _event(self, event, data):
        if event == "status":
            st = from_wire(data)
            for cb in list(self._status_cbs):
                _safe(cb, st)
        elif event == "queue":
            self.queue = data
            for cb in list(self._queue_cbs):
                _safe(cb, data)
        elif event == "printer":
            self._apply_printer(data)
            for cb in list(self._printer_cbs):
                _safe(cb, data)
        elif event == "pairing":
            for cb in list(self._pairing_cbs):
                _safe(cb, data)

    def _closed(self, reason):
        if self.closed:
            return
        self.closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(PrinterError(reason))
        self._pending.clear()
        for cb in list(self._close_cbs):
            _safe(cb, reason)

    async def close(self):
        self.closed = True
        if self._ws is not None:
            await self._ws.close()
        if self._reader:
            self._reader.cancel()
        if self._session is not None:
            await self._session.close()

    # ------------------------------------------------------------ listeners

    def on_status(self, callback):
        self._status_cbs.append(callback)
        return lambda: self._status_cbs.remove(callback)

    def on_disconnect(self, callback):
        self._close_cbs.append(callback)

    def on_queue(self, callback):
        self._queue_cbs.append(callback)

    def on_pairing(self, callback):
        self._pairing_cbs.append(callback)

    def on_printer(self, callback):
        self._printer_cbs.append(callback)

    # ------------------------------------------------------------ RPC

    async def call(self, method, timeout=180, **params):
        if not self.connected:
            raise PrinterError("not connected to the server")
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._ws.send_json({"id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise PrinterError(f"{method}: the server didn't answer") from None

    async def status(self):
        return await self.call("status")

    # queue
    async def queue_list(self):
        self.queue = await self.call("queue.list")
        return self.queue

    async def confirm_plate(self, job_id):
        return await self.call("queue.confirm_plate", job_id=job_id)

    async def cancel_job(self, job_id):
        return await self.call("queue.cancel", job_id=job_id)

    async def remove_job(self, job_id):
        return await self.call("queue.remove", job_id=job_id)

    # admin
    async def pending_pairings(self):
        return await self.call("pairing.list")

    async def approve_pairing(self, request_id, role=b.Role.OPERATOR):
        return await self.call("pairing.approve", request_id=request_id, role=int(role))

    async def deny_pairing(self, request_id):
        return await self.call("pairing.deny", request_id=request_id)

    async def list_clients(self):
        return await self.call("clients.list")

    async def revoke_client(self, client_id):
        return await self.call("clients.revoke", client_id=client_id)

    # ------------------------------------------------------------ HTTP: files and camera

    async def start_print(self, path, *, progress=None, plate_cleared=True):
        """Upload into the server's queue. Returns the job (it waits for plate confirmation)."""
        b.check_role(self.role, "start_print")
        total = os.path.getsize(path)

        async def body():
            sent = 0
            with open(path, "rb") as f:
                while chunk := f.read(1 << 16):
                    sent += len(chunk)
                    if progress:
                        progress(sent, total)
                    yield chunk
        async with self._session.post(f"{self.base}/api/jobs",
                                      params={"name": os.path.basename(path)},
                                      data=body()) as r:
            if r.status != 201:
                raise PrinterError(f"the server refused the file: {await r.text()}")
            return await r.json()

    async def snapshot(self):
        async with self._session.get(f"{self.base}/api/camera/snapshot.jpg") as r:
            if r.status != 200:
                raise PrinterError(await r.text())
            return CameraFrame(0, 0, 2, await r.read())

    async def job_snapshot(self, job_id):
        async with self._session.get(f"{self.base}/api/jobs/{job_id}/snapshot.jpg") as r:
            if r.status != 200:
                raise PrinterError(await r.text())
            return await r.read()

    async def camera_stream(self):
        """Parse the server's MJPEG relay into CameraFrames."""
        async with self._session.get(f"{self.base}/api/camera.mjpeg") as r:
            if r.status != 200:
                raise PrinterError(await r.text())
            while True:
                line = await r.content.readline()
                if not line:
                    return
                if not line.startswith(b"--frame"):
                    continue
                length = None
                while (h := await r.content.readline()) not in (b"\r\n", b""):
                    if h.lower().startswith(b"content-length:"):
                        length = int(h.split(b":", 1)[1])
                if length is None:
                    continue
                data = await r.content.readexactly(length)
                yield CameraFrame(0, 0, 2, data)


def _error(err):
    code, message = err.get("code"), err.get("message", "error")
    if code == 403:
        return b.PermissionDenied(message)
    if code == 400:
        return b.Unsupported(message)
    return PrinterError(message)


def _safe(cb, *args):
    try:
        cb(*args)
    except Exception:  # noqa: BLE001
        log.exception("listener failed")


def _make_op(name):
    names = PARAMS.get(name, [])

    async def op(self, *args, **kwargs):
        params = dict(zip(names, args))
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self.call(name, **params)
    op.__name__ = name
    return op


for _name in b.REQUIRED_ROLE:
    if _name not in ("status", "start_print", "snapshot", "camera_stream"):
        setattr(RemoteBackend, _name, _make_op(_name))
