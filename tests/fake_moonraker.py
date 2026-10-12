"""A small Moonraker stand-in: the HTTP endpoints and websocket OpenBot uses, plus a
camera. Shaped after a real Moonraker (v0.9) with Klipper v0.13 on a Raspberry Pi."""

import asyncio
import json
import os

from aiohttp import web

# A 2x1 baseline JPEG is enough for jpeg_size(); the bytes in between are arbitrary.
JPEG = (b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x01\x00\x02\x03\x01\x22\x00\x02\x11\x01\x03\x11"
        b"\x01\xff\xda\x00\x02\x12\x34\xff\xd9")


with open(os.path.join(os.path.dirname(__file__), "fixtures", "test_camera_frame.jpg"),
          "rb") as _f:
    CAMERA_JPEG = _f.read()     # a real picture, for the app's camera view


class FakeMoonraker:
    def __init__(self, *, login=None, webcam=True, jpeg=JPEG):
        self.login = login                     # (username, password) if login is required
        self.token = "tok-1"
        self.webcam = webcam
        self.jpeg = jpeg
        self.objects = {
            "print_stats": {"filename": "", "total_duration": 0.0, "print_duration": 0.0,
                            "state": "standby", "message": ""},
            "virtual_sdcard": {"progress": 0.0, "is_active": False},
            "display_status": {"progress": 0.0, "message": None},
            "webhooks": {"state": "ready", "state_message": "Printer is ready"},
            "extruder": {"temperature": 22.0, "target": 0.0},
            "heater_bed": {"temperature": 21.5, "target": 0.0},
        }
        self.files = {"old.gcode": (b"G28\n", 100.0)}
        self.gcode = []                        # scripts received
        self.actions = []                      # print start/pause/... received
        self.sockets = []
        self.port = None

    async def start(self):
        app = web.Application(middlewares=[self._auth], client_max_size=1024 ** 3)
        r = app.router
        r.add_get("/access/info", self.h_access_info)
        r.add_post("/access/login", self.h_login)
        r.add_get("/access/oneshot_token", self._const("one-shot"))
        r.add_get("/server/info", self._const({"klippy_state": "ready"}))
        r.add_get("/printer/info", self._const({
            "hostname": "fakepi", "software_version": "v0.13.0-642-g77d5d942e"}))
        r.add_get("/server/webcams/list", self.h_webcams)
        r.add_get("/server/files/list", self.h_files)
        r.add_post("/server/files/upload", self.h_upload)
        r.add_post("/printer/gcode/script", self.h_script)
        r.add_get("/printer/objects/query", self.h_query)
        for action in ("start", "pause", "resume", "cancel"):
            r.add_post(f"/printer/print/{action}", self._action(action))
        r.add_post("/printer/emergency_stop", self.h_estop)
        r.add_post("/printer/firmware_restart", self.h_restart)
        r.add_get("/webcam/", self.h_webcam)
        r.add_get("/websocket", self.h_ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        for ws in list(self.sockets):
            await ws.close()
        await self._runner.cleanup()

    @property
    def address(self):
        return f"http://127.0.0.1:{self.port}/viewer"

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _ok(result):
        return web.json_response({"result": result})

    def _const(self, result):
        async def handler(request):
            return self._ok(result)
        return handler

    @staticmethod
    def _err(code, message):
        return web.json_response({"error": {"code": code, "message": message}}, status=code)

    @web.middleware
    async def _auth(self, request, handler):
        open_paths = ("/access/info", "/access/login", "/webcam/")
        if self.login and request.path not in open_paths:
            ok = (request.headers.get("Authorization") == f"Bearer {self.token}" or
                  request.query.get("token") == "one-shot")
            if not ok:
                return self._err(401, "Unauthorized")
        return await handler(request)

    def update(self, **objects):
        """Change Klipper objects and push notify_status_update to every client."""
        for name, fields in objects.items():
            self.objects.setdefault(name, {}).update(fields)
        self.notify("notify_status_update", [objects, 123.0])

    def notify(self, method, params):
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        for ws in list(self.sockets):
            asyncio.ensure_future(ws.send_str(msg))

    # ------------------------------------------------------------ handlers

    async def h_access_info(self, request):
        return self._ok({"login_required": bool(self.login), "trusted": not self.login})

    async def h_login(self, request):
        body = await request.json()
        if (body.get("username"), body.get("password")) != self.login:
            return self._err(401, "Invalid Password")
        return self._ok({"username": body["username"], "token": self.token})

    async def h_webcams(self, request):
        cams = [{"name": "cam", "enabled": True, "service": "mjpegstreamer",
                 "stream_url": "/webcam/?action=stream",
                 "snapshot_url": "/webcam/?action=snapshot", "flip_horizontal": True,
                 "flip_vertical": False, "rotation": 0}] if self.webcam else []
        return self._ok({"webcams": cams})

    async def h_files(self, request):
        return self._ok([{"path": k, "size": len(v[0]), "modified": v[1]}
                         for k, v in self.files.items()])

    async def h_upload(self, request):
        reader = await request.multipart()
        fields, data, name = {}, b"", None
        async for part in reader:
            if part.name == "file":
                name, data = part.filename, await part.read()
            else:
                fields[part.name] = await part.text()
        self.files[name] = (data, 200.0)
        started = fields.get("print") == "true"
        if started:
            self._start(name)
        return web.json_response({"item": {"path": name, "root": fields.get("root")},
                                  "print_started": started, "action": "create_file"},
                                 status=201)

    def _start(self, name):
        self.actions.append(("start", name))
        self.update(print_stats={"state": "printing", "filename": name,
                                 "print_duration": 0.0, "total_duration": 0.0})

    def _action(self, action):
        async def handler(request):
            if action == "start":
                name = request.query["filename"]
                if name not in self.files:
                    return self._err(400, f"File {name} does not exist")
                self._start(name)
                return self._ok("ok")
            self.actions.append((action, None))
            state = {"pause": "paused", "resume": "printing", "cancel": "cancelled"}[action]
            self.update(print_stats={"state": state})
            return self._ok("ok")
        return handler

    async def h_script(self, request):
        script = request.query["script"]
        self.gcode.append(script)
        if script == "BAD":
            self.notify("notify_gcode_response", ["!! Unknown command: BAD"])
            return self._err(400, "Unknown command: BAD")
        self.notify("notify_gcode_response", [f"// ran {script}"])
        await asyncio.sleep(0.01)
        return self._ok("ok")

    async def h_query(self, request):
        return self._ok({"status": {k: self.objects.get(k, {}) for k in request.query}})

    async def h_estop(self, request):
        self.actions.append(("estop", None))
        self.objects["webhooks"] = {"state": "shutdown",
                                    "state_message": "Shutdown due to M112 command"}
        self.notify("notify_klippy_shutdown", [])
        return self._ok("ok")

    async def h_restart(self, request):
        self.actions.append(("restart", None))
        self.objects["webhooks"] = {"state": "ready", "state_message": "Printer is ready"}
        self.objects["print_stats"].update(state="standby", filename="")   # as Klipper does
        self.notify("notify_klippy_ready", [])
        return self._ok("ok")

    async def h_webcam(self, request):
        if request.query.get("action") == "snapshot":
            return web.Response(body=self.jpeg, content_type="image/jpeg")
        resp = web.StreamResponse(headers={
            "Content-Type": "multipart/x-mixed-replace;boundary=boundarydonotcross"})
        await resp.prepare(request)
        try:
            for _ in range(50):
                await resp.write(b"--boundarydonotcross\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: %d\r\n\r\n" % len(self.jpeg) + self.jpeg +
                                 b"\r\n")
                await asyncio.sleep(0.02)
        except ConnectionError:
            pass
        return resp

    async def h_ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        try:
            async for msg in ws:
                req = json.loads(msg.data)
                method, params = req["method"], req.get("params") or {}
                if method == "server.connection.identify":
                    result = {"connection_id": 1}
                elif method == "printer.objects.subscribe":
                    result = {"eventtime": 1.0, "status": {
                        k: dict(self.objects.get(k, {})) for k in params["objects"]}}
                else:
                    await ws.send_json({"jsonrpc": "2.0", "id": req["id"], "error": {
                        "code": -32601, "message": f"Method not found: {method}"}})
                    continue
                await ws.send_json({"jsonrpc": "2.0", "id": req["id"], "result": result})
        finally:
            self.sockets.remove(ws)
        return ws
