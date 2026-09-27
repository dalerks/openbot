"""The OpenBot server: shares one printer with OpenBot clients (PLAN.md §3.1).

HTTP API (TLS, default port 8765):

    GET  /api/info                      no auth: server + printer summary
    POST /api/pair                      no auth: {name, setup_code?} -> {id, code}
    GET  /api/pair/{id}                 no auth: poll -> {status, token?, role?}
    GET  /api/ws                        JSON-RPC over WebSocket + pushed events
    POST /api/jobs?name=FILE            Operator: upload a print file into the queue
    GET  /api/jobs                      Viewer: the queue
    GET  /api/jobs/{id}/snapshot.jpg    Viewer: photo taken when the job finished
    GET  /api/camera.mjpeg              Viewer: live camera (MJPEG, any browser works)
    GET  /api/camera/snapshot.jpg       Viewer: one frame

Auth: `Authorization: Bearer <token>` (or `?token=` for browsers).
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import socket
import ssl
import tempfile

from aiohttp import WSMsgType, web

from .. import __version__, machines, project
from ..backend import (PermissionDenied, REQUIRED_ROLE, Role, Unsupported, check_role)
from ..printer.errors import PrinterError
from ..remote.codec import to_wire
from .auth import ClientStore, ensure_certificate
from .camera_relay import CameraRelay
from .queue import JobQueue

log = logging.getLogger(__name__)

API_VERSION = 1
CLIENT = web.RequestKey("client", object)
DEFAULT_PORT = 8765
MAX_UPLOAD = 512 * 1024 * 1024
BINARY_OPS = {"start_print", "camera_stream", "snapshot"}     # not over JSON-RPC

QUEUE_OPS = {"queue.list": Role.VIEWER, "queue.confirm_plate": Role.OPERATOR,
             "queue.cancel": Role.OPERATOR, "queue.remove": Role.OPERATOR}
ADMIN_OPS = {"clients.list", "clients.revoke", "clients.set_role", "pairing.list",
             "pairing.approve", "pairing.deny"}


class RpcFailure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class OpenBotServer:
    def __init__(self, data_dir, host, name=None):
        """host: provides `.backend` (admin-level PrinterBackend or None), `.on_status(cb)`
        and `.on_connection(cb)`; see server.host.PrinterHost."""
        self.dir = os.path.join(data_dir, "server")
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self.host = host
        self.name = name or socket.gethostname().split(".")[0]
        self.clients = ClientStore(self.dir)
        self.queue = JobQueue(self.dir, lambda: self.host.backend)
        self.relay = CameraRelay(lambda: self.host.backend)
        self.cert_path, self.key_path = ensure_certificate(self.dir, self.name)
        with open(self.cert_path, "rb") as f:
            import ssl as _ssl
            der = _ssl.PEM_cert_to_DER_cert(f.read().decode())
        self.fingerprint = hashlib.sha256(der).hexdigest()
        self.server_id = self.fingerprint[:16]
        self._sockets: dict[web.WebSocketResponse, object] = {}
        self._runner = None
        self.port = None
        self.queue.on_change(lambda jobs: self._broadcast("queue", jobs))
        self.clients.on_change(self._clients_changed)
        host.on_status(self._on_status)
        host.on_connection(lambda state: self._broadcast("printer", self.printer_info()))

    # ------------------------------------------------------------ lifecycle

    def make_app(self):
        app = web.Application(client_max_size=MAX_UPLOAD, middlewares=[self._auth_mw])
        app.add_routes([
            web.get("/", self.h_index),
            web.get("/web/{name}", self.h_static),
            web.get("/favicon.ico", lambda r: web.HTTPFound("/web/favicon.svg")),
            web.get("/api/info", self.h_info),
            web.post("/api/pair", self.h_pair),
            web.get("/api/pair/{id}", self.h_pair_poll),
            web.get("/api/ws", self.h_ws),
            web.post("/api/jobs", self.h_upload),
            web.get("/api/jobs", self.h_jobs),
            web.get("/api/jobs/{id}/snapshot.jpg", self.h_job_snapshot),
            web.get("/api/camera.mjpeg", self.h_mjpeg),
            web.get("/api/camera/snapshot.jpg", self.h_snapshot),
        ])
        return app

    async def start(self, bind="0.0.0.0", port=DEFAULT_PORT):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cert_path, self.key_path)
        self._runner = web.AppRunner(self.make_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, bind, port, ssl_context=ctx)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        log.info("OpenBot server on port %s (fingerprint %s)", self.port, self.fingerprint)
        if self.clients.setup_code:
            log.warning("No admin yet. Setup code for the first client: %s",
                        self.clients.setup_code)
        return self.port

    async def stop(self):
        for ws in list(self._sockets):
            with contextlib.suppress(Exception):
                await ws.close()
        await self.relay.close()
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------ helpers

    def printer_info(self):
        b = self.host.backend
        if b is None:
            return {"connected": False}
        m = machines.MACHINES.get(b.machine_id)
        return {"connected": True, "machine": b.machine_id,
                "name": getattr(getattr(b, "printer", None), "info", None) and
                b.printer.info.name or (m.name if m else ""),
                "capabilities": sorted(b.capabilities)}

    def _scoped_backend(self, client):
        b = self.host.backend
        if b is None:
            raise RpcFailure(503, "the printer isn't connected to the server")
        return type(b)(b.printer, role=Role(client.role))

    @web.middleware
    async def _auth_mw(self, request, handler):
        token = None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        token = token or request.query.get("token")
        request[CLIENT] = self.clients.authenticate(token)
        return await handler(request)

    def _require(self, request, role):
        client = request[CLIENT]
        if client is None:
            raise web.HTTPUnauthorized(text="pair with this server first")
        if client.role < role:
            raise web.HTTPForbidden(text=f"needs {Role(role).name.lower()} access")
        return client

    # ------------------------------------------------------------ web page

    WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    WEB_FILES = {"app.js": "application/javascript", "style.css": "text/css",
                 "favicon.svg": "image/svg+xml"}
    SECURITY_HEADERS = {
        "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; "
                                   "connect-src 'self'; frame-ancestors 'none'; "
                                   "base-uri 'none'; form-action 'self'",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-cache"}

    async def h_index(self, request):
        with open(os.path.join(self.WEB_DIR, "index.html"), "rb") as f:
            body = f.read()
        return web.Response(body=body, content_type="text/html", charset="utf-8",
                            headers=self.SECURITY_HEADERS)

    async def h_static(self, request):
        name = request.match_info["name"]
        if name not in self.WEB_FILES:
            raise web.HTTPNotFound()
        with open(os.path.join(self.WEB_DIR, name), "rb") as f:
            body = f.read()
        ctype = self.WEB_FILES[name]
        return web.Response(body=body, content_type=ctype,
                            charset=None if ctype.startswith("image/") else "utf-8",
                            headers=self.SECURITY_HEADERS)

    def web_url(self, host=None):
        host = host or (socket.gethostname().split(".")[0] + ".local")
        return f"https://{host}:{self.port}/"

    # ------------------------------------------------------------ HTTP handlers

    async def h_info(self, request):
        return web.json_response({
            "server_name": self.name, "server_id": self.server_id,
            "version": __version__, "api_version": API_VERSION,
            "fingerprint": self.fingerprint, "needs_setup": self.clients.setup_code is not None,
            "printer": self.printer_info(), "project": project.public_info()})

    async def h_pair(self, request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = str(body.get("name") or "OpenBot client")
        try:
            req = self.clients.request(name, body.get("setup_code"))
        except PermissionError as e:
            raise web.HTTPForbidden(text=str(e)) from None
        return web.json_response({"id": req.id, "code": req.code, "status": req.status})

    async def h_pair_poll(self, request):
        return web.json_response(self.clients.poll(request.match_info["id"]))

    async def h_jobs(self, request):
        self._require(request, Role.VIEWER)
        return web.json_response(self.queue.public())

    async def h_job_snapshot(self, request):
        self._require(request, Role.VIEWER)
        try:
            job = self.queue.get(request.match_info["id"])
        except KeyError:
            raise web.HTTPNotFound() from None
        if not job.snapshot:
            raise web.HTTPNotFound(text="no photo for this job")
        return web.FileResponse(job.snapshot, headers={"Content-Type": "image/jpeg"})

    async def h_upload(self, request):
        client = self._require(request, Role.OPERATOR)
        backend = self.host.backend
        if backend is None:
            raise web.HTTPServiceUnavailable(text="the printer isn't connected")
        machine = machines.get(backend.machine_id)
        name = os.path.basename(request.query.get("name", "print"))
        ext = os.path.splitext(name)[1].lower()
        if ext != machine.file_extension:
            raise web.HTTPBadRequest(text=f"the {machine.name} needs a "
                                          f"{machine.file_extension} file")
        fd, tmp = tempfile.mkstemp(dir=os.path.join(self.dir, "jobs"), suffix=".upload")
        size = 0
        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.content.iter_chunked(1 << 16):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise web.HTTPRequestEntityTooLarge(MAX_UPLOAD, size)
                    f.write(chunk)
            problems = self._check_file(tmp, machine)
            if problems:
                raise web.HTTPBadRequest(text="; ".join(problems))
            job = self.queue.add(name, tmp, machine.id, client.name)
            final = self.queue.job_file(job.id, name)
            os.replace(tmp, final)
            job.path = final
            self.queue._changed()
            self.queue.status_changed(await backend.status())
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return web.json_response(job.public(), status=201)

    def _check_file(self, path, machine):
        if machine.output == "makerbot":
            from ..printer.client import check_print_file
            return [p for p in check_print_file(path) if "not a" in p or "made for" in p]
        from ..slicing.gcode_output import GcodeCheckError, analyse
        try:
            analyse(path, machine.bed, machine.max_nozzle_temp)
        except GcodeCheckError as e:
            return [str(e)]
        return []

    async def h_snapshot(self, request):
        client = self._require(request, Role.VIEWER)
        try:
            frame = await self._scoped_backend(client).snapshot()
        except (RpcFailure, Unsupported, PrinterError) as e:
            raise web.HTTPServiceUnavailable(text=str(getattr(e, "message", e))) from None
        return web.Response(body=frame.data, content_type="image/jpeg")

    async def h_mjpeg(self, request):
        self._require(request, Role.VIEWER)
        if self.host.backend is None or not self.host.backend.supports("camera"):
            raise web.HTTPNotFound(text="this printer has no camera")
        resp = web.StreamResponse(headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=frame",
            "Cache-Control": "no-store"})
        await resp.prepare(request)
        async with self.relay.subscribe() as q:
            try:
                while True:
                    data = await asyncio.wait_for(q.get(), 30)
                    await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     + f"Content-Length: {len(data)}\r\n\r\n".encode()
                                     + data + b"\r\n")
            except (ConnectionResetError, asyncio.TimeoutError, asyncio.CancelledError):
                pass
        return resp

    # ------------------------------------------------------------ WebSocket JSON-RPC

    async def h_ws(self, request):
        client = self._require(request, Role.VIEWER)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._sockets[ws] = client
        await ws.send_json({"event": "hello", "data": {
            "client": client.public(), "server": self.name, "printer": self.printer_info(),
            "queue": self.queue.public()}})
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    req = json.loads(msg.data)
                except ValueError:
                    continue
                asyncio.ensure_future(self._answer(ws, client, req))
        finally:
            self._sockets.pop(ws, None)
        return ws

    async def _answer(self, ws, client, req):
        rid, method, params = req.get("id"), req.get("method", ""), req.get("params") or {}
        try:
            result = await self._dispatch(client, method, params)
            reply = {"id": rid, "result": to_wire(result)}
        except RpcFailure as e:
            reply = {"id": rid, "error": {"code": e.code, "message": e.message}}
        except PermissionDenied as e:
            reply = {"id": rid, "error": {"code": 403, "message": str(e)}}
        except Unsupported as e:
            reply = {"id": rid, "error": {"code": 400, "message": str(e)}}
        except (PrinterError, PermissionError, KeyError, ValueError, TypeError) as e:
            reply = {"id": rid, "error": {"code": 409, "message": str(e) or repr(e)}}
        except Exception as e:  # noqa: BLE001
            log.exception("RPC %s failed", method)
            reply = {"id": rid, "error": {"code": 500, "message": repr(e)}}
        with contextlib.suppress(Exception):
            await ws.send_json(reply)

    async def _dispatch(self, client, method, params):
        if method in QUEUE_OPS:
            if client.role < QUEUE_OPS[method]:
                raise PermissionDenied(f"{method} needs "
                                       f"{Role(QUEUE_OPS[method]).name.lower()} access")
            if method == "queue.list":
                return self.queue.public()
            job_id = params.get("job_id")
            if method == "queue.confirm_plate":
                await self.queue.confirm_plate(job_id, client.name)
            elif method == "queue.cancel":
                await self.queue.cancel(job_id)
            elif method == "queue.remove":
                await self.queue.remove(job_id)
            return self.queue.public()
        if method in ADMIN_OPS:
            if client.role < Role.ADMIN:
                raise PermissionDenied(f"{method} needs admin access")
            return self._admin(method, params)
        if method == "server.info":
            return {"server": self.name, "printer": self.printer_info(),
                    "client": client.public()}
        if method not in REQUIRED_ROLE or method in BINARY_OPS:
            raise RpcFailure(404, f"unknown method {method}")
        check_role(Role(client.role), method)
        backend = self._scoped_backend(client)
        return await getattr(backend, method)(**params)

    def _admin(self, method, params):
        c = self.clients
        if method == "clients.list":
            return [x.public() for x in c.clients.values()]
        if method == "clients.revoke":
            return c.revoke(params["client_id"])
        if method == "clients.set_role":
            c.set_role(params["client_id"], Role(params["role"]))
            return True
        if method == "pairing.list":
            return c.pending_requests()
        if method == "pairing.approve":
            c.approve(params["request_id"], Role(params.get("role", Role.OPERATOR)))
            return True
        if method == "pairing.deny":
            c.deny(params["request_id"])
            return True
        raise RpcFailure(404, method)

    # ------------------------------------------------------------ events

    def _on_status(self, status):
        self.queue.status_changed(status)
        self._broadcast("status", to_wire(status))

    def _clients_changed(self):
        # Revoked clients lose their live connection immediately.
        for ws, client in list(self._sockets.items()):
            if client.id not in self.clients.clients:
                asyncio.ensure_future(ws.close(code=4001, message=b"revoked"))
        self._broadcast("pairing", self.clients.pending_requests(), min_role=Role.ADMIN)

    def _broadcast(self, event, data, min_role=Role.VIEWER):
        for ws, client in list(self._sockets.items()):
            if client.role >= min_role and not ws.closed:
                asyncio.ensure_future(self._send(ws, {"event": event, "data": data}))

    async def _send(self, ws, msg):
        with contextlib.suppress(Exception):
            await ws.send_json(msg)
