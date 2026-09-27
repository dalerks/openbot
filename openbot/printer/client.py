"""High-level client for one MakerBot Replicator+.

    printer = Printer("192.168.1.154")
    await printer.connect()          # trusted TLS + reauthorize (no knob press)
    status = await printer.status()
    await printer.start_print("cube.makerbot")

All control traffic goes over the TLS port, which kaiten treats as a
"secure" channel, so secure-only methods like wifi_connect work.
"""

import asyncio
import json
import logging
import os
import ssl
import zipfile
import zlib

from . import auth as auth_mod
from .errors import AuthError, CertificateChanged, INVALID_CREDENTIALS, NotTrusted, \
    PrinterError, RpcError, AUTH_REJECTED, AUTH_TIMED_OUT
from .models import (NetworkState, PrinterInfo, PrinterStatus, StaticIpConfig,
                     WifiNetwork, EXTRUDERS)
from .rpc import RpcConnection
from .trust import TrustStore, fetch_certificate

log = logging.getLogger(__name__)

PLAIN_PORT = 9999
TLS_PORT = 12309
UPLOAD_BLOCK = 50_000          # what MakerBot's own clients and queue3d use
STATUS_METHODS = ("state_notification", "system_notification")


class Printer:
    def __init__(self, host, *, trust=None, credentials=None,
                 port=PLAIN_PORT, ssl_port=TLS_PORT):
        self.host = host
        self.port = port
        self.ssl_port = ssl_port
        self.trust = trust or TrustStore()
        self.credentials = credentials or auth_mod.default_credential_store()
        self.info: PrinterInfo | None = None
        self.tls_info = None
        self._conn: RpcConnection | None = None
        self._last_status: PrinterStatus | None = None

    # ------------------------------------------------------------ lifecycle

    async def hello(self):
        """Unauthenticated handshake on :9999 to learn who this printer is."""
        conn = await RpcConnection.open(self.host, self.port)
        try:
            result = await conn.call("handshake")
        finally:
            await conn.close()
        self.info = PrinterInfo.from_handshake(result, ip=self.host)
        if result.get("ssl_port"):
            self.ssl_port = int(result["ssl_port"])
        return self.info

    async def fetch_certificate(self):
        return await fetch_certificate(self.host, self.ssl_port)

    async def _open_tls(self):
        if self.info is None:
            await self.hello()
        serial = self.info.serial
        trusted = self.trust.get(serial)
        if trusted is None:
            raise NotTrusted(await self.fetch_certificate())
        try:
            conn = await RpcConnection.open(self.host, self.ssl_port,
                                            ssl=self.trust.context_for(serial))
        except PrinterError as e:
            if isinstance(e.__cause__, ssl.SSLCertVerificationError):
                presented = await self.fetch_certificate()
                if presented.sha256 != trusted.sha256:
                    raise CertificateChanged(trusted, presented) from None
            raise
        self.tls_info = conn.tls_info
        await conn.call("handshake")
        return conn

    async def connect(self):
        """Open a verified TLS session and authenticate with stored pairing credentials."""
        if self.info is None:
            await self.hello()
        # Trust is checked before pairing: a new printer must first be trusted
        # (NotTrusted), and only then paired (AuthError).
        conn = await self._open_tls()
        creds = self.credentials.get(self.info.serial)
        if creds is None:
            await conn.close()
            raise AuthError("not paired with this printer yet; run pair()")
        try:
            await conn.call("reauthorize", creds.reauthorize_params())
        except RpcError as e:
            await conn.close()
            if e.code == INVALID_CREDENTIALS:
                raise AuthError("the printer no longer accepts our pairing; pair again") from e
            raise
        self._attach(conn)
        return self

    async def pair(self, username=None, *, timeout=180.0, blink_chamber=False):
        """First-time pairing. Someone must press the knob on the printer.

        On success the session is authenticated and credentials are stored.
        """
        if self.info is None:
            await self.hello()
        username = username or auth_mod.default_username()
        secret = auth_mod.new_local_secret()
        conn = await self._open_tls()
        params = {"username": username, "local_secret": secret}
        if blink_chamber:
            params["chamber_blink"] = True
        try:
            result = await conn.call("authorize", params, timeout=timeout)
        except RpcError as e:
            await conn.close()
            if e.code == AUTH_REJECTED:
                raise AuthError("pairing was rejected on the printer") from e
            if e.code == AUTH_TIMED_OUT:
                raise AuthError("nobody pressed the knob in time") from e
            raise
        except BaseException:
            await conn.close()
            raise
        code = (result or {}).get("local_code")
        if not code:
            await conn.close()
            raise AuthError(f"unexpected authorize reply: {sorted((result or {}).keys())}")
        self.credentials.put(self.info.serial,
                             auth_mod.Credentials(username, secret, code))
        self._attach(conn)
        return self

    async def unpair(self):
        creds = self.credentials.get(self.info.serial)
        if creds:
            try:
                await self._rpc().call("deauthorize", {"username": creds.username})
            finally:
                self.credentials.delete(self.info.serial)

    def _attach(self, conn):
        self._conn = conn
        conn.on_notification(self._track_status)

    def _track_status(self, method, params):
        if method in STATUS_METHODS and isinstance(params, dict) and "info" in params:
            self._last_status = PrinterStatus.from_info(params["info"])

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    @property
    def connected(self):
        return self._conn is not None and not self._conn.closed

    def _rpc(self):
        if not self.connected:
            raise PrinterError("not connected")
        return self._conn

    async def call(self, method, params=None, timeout=15.0):
        """Escape hatch for methods without a wrapper yet."""
        return await self._rpc().call(method, params, timeout)

    # ------------------------------------------------------------ status

    async def status(self):
        info = await self._rpc().call("get_system_information")
        self._last_status = PrinterStatus.from_info(info)
        return self._last_status

    @property
    def last_status(self):
        return self._last_status

    def on_status(self, callback):
        """callback(PrinterStatus) on every state/system notification. Returns unsubscribe."""
        def handler(method, params):
            if method in STATUS_METHODS and isinstance(params, dict) and "info" in params:
                callback(PrinterStatus.from_info(params["info"]))
        return self._rpc().on_notification(handler)

    def on_disconnect(self, callback):
        self._rpc().on_close(callback)

    # ------------------------------------------------------------ printing

    async def start_print(self, path, *, progress=None, plate_cleared=True):
        """Upload a .makerbot file and start it.

        `plate_cleared=True` tells the printer the build plate has been cleared.
        Callers must only pass True after a person has confirmed it.
        progress(sent_bytes, total_bytes) is called during upload.
        """
        status = await self.status()
        if status.process is not None:
            raise PrinterError(
                f"printer is busy ({status.process.name}: {status.process.step})")
        with open(path, "rb") as f:
            data = f.read()
        name = os.path.basename(path)
        total = len(data)
        crc = zlib.crc32(data) & 0xFFFFFFFF
        rpc = self._rpc()
        await rpc.call("print", {"filepath": name, "transfer_wait": True})
        if plate_cleared:
            try:
                await rpc.call("process_method", {"method": "build_plate_cleared"})
            except RpcError as e:
                log.info("build_plate_cleared not needed/accepted: %s", e)
        await rpc.call("put_init", {"block_size": UPLOAD_BLOCK, "file_id": "1",
                                    "file_path": f"/current_thing/{name}",
                                    "length": total})
        sent = 0
        for off in range(0, total, UPLOAD_BLOCK):
            chunk = data[off:off + UPLOAD_BLOCK]
            await rpc.call("put_raw", {"file_id": "1", "length": len(chunk)})
            await rpc.send_raw(chunk)
            sent += len(chunk)
            if progress:
                progress(sent, total)
        await rpc.call("put_term", {"crc": crc, "file_id": "1", "length": total},
                       timeout=60)

    async def confirm_build_plate_cleared(self):
        await self._rpc().call("process_method", {"method": "build_plate_cleared"})

    async def cancel(self):
        await self._rpc().call("cancel")

    async def pause(self):
        await self._rpc().call("process_method", {"method": "suspend"})

    async def resume(self):
        await self._rpc().call("process_method", {"method": "resume"})

    async def acknowledge(self):
        """Dismiss a finished/failed process ("print complete" screen)."""
        await self._rpc().call("acknowledged")

    # ------------------------------------------------------------ filament / heat

    async def load_filament(self, tool_index=0):
        await self._rpc().call("load_filament", {"tool_index": tool_index})

    async def unload_filament(self, tool_index=0):
        await self._rpc().call("unload_filament", {"tool_index": tool_index})

    async def stop_filament(self):
        """Finish an in-progress load/unload."""
        await self._rpc().call("process_method", {"method": "stop_filament"})

    async def preheat(self):
        await self._rpc().call("preheat")

    async def cool(self):
        await self._rpc().call("cool")

    # ------------------------------------------------------------ camera

    async def snapshot(self, timeout=10.0):
        """One camera frame. Uses request_camera_frame, falling back to a brief stream."""
        rpc = self._rpc()
        loop = asyncio.get_running_loop()
        got = loop.create_future()

        def on_frame(frame):
            if not got.done():
                got.set_result(frame)

        unsubscribe = rpc.on_camera_frame(on_frame)
        streaming = False
        try:
            try:
                await rpc.call("request_camera_frame")
            except RpcError:
                await rpc.call("request_camera_stream")
                streaming = True
            return await asyncio.wait_for(got, timeout)
        except asyncio.TimeoutError:
            raise PrinterError("no camera frame received") from None
        finally:
            unsubscribe()
            if streaming:
                await rpc.notify("end_camera_stream")

    async def camera_stream(self):
        """Async iterator of CameraFrames until the consumer stops iterating."""
        rpc = self._rpc()
        queue = asyncio.Queue(maxsize=4)

        def on_frame(frame):
            if queue.full():           # drop the oldest; live view wants the newest
                queue.get_nowait()
            queue.put_nowait(frame)

        unsubscribe = rpc.on_camera_frame(on_frame)
        await rpc.call("request_camera_stream")
        try:
            while True:
                yield await queue.get()
        finally:
            unsubscribe()
            if not rpc.closed:
                await rpc.notify("end_camera_stream")

    # ------------------------------------------------------------ network

    async def network_state(self):
        return NetworkState.from_dict(await self._rpc().call("network_state"))

    async def wifi_scan(self, force_rescan=False):
        aps = await self._rpc().call("wifi_scan", {"force_rescan": force_rescan},
                                     timeout=45)
        nets = [WifiNetwork(a.get("path", ""), a.get("name", ""),
                            int(a.get("strength", 0)), a.get("password", "required"))
                for a in aps or []]
        return sorted(nets, key=lambda n: -n.strength)

    async def wifi_connect(self, path, password=None, name=None, timeout=150.0):
        """Join a network from wifi_scan (`path`). `name` is the SSID for hidden networks.

        If this Mac reaches the printer over its current Wi-Fi, the connection
        will drop; rediscover the printer by serial afterwards.
        """
        params = {"path": path}
        if password is not None:
            params["password"] = password
        if name is not None:
            params["name"] = name
        return NetworkState.from_dict(
            await self._rpc().call("wifi_connect", params, timeout=timeout))

    async def wifi_disconnect(self, path=None):
        await self._rpc().call("wifi_disconnect", {"path": path} if path else {})

    async def wifi_forget(self, path=None):
        await self._rpc().call("wifi_forget", {"path": path} if path else {})

    async def wifi_enable(self):
        await self._rpc().call("wifi_enable")

    async def wifi_disable(self):
        await self._rpc().call("wifi_disable")

    async def get_static_ipv4(self, service_path="ethernet"):
        return StaticIpConfig.from_dict(
            await self._rpc().call("get_static_ipv4", {"service_path": service_path}))

    async def set_static_ipv4(self, service_path="ethernet", *, ip=None, netmask=None,
                              gateway=None, dns=None):
        params = {"service_path": service_path, "use_static": True}
        for k, v in (("ip", ip), ("netmask", netmask), ("gateway", gateway), ("dns", dns)):
            if v is not None:
                params[k] = v
        await self._rpc().call("set_static_ipv4", params, timeout=30)

    async def use_dhcp(self, service_path="ethernet"):
        await self._rpc().call("set_static_ipv4",
                               {"service_path": service_path, "use_static": False},
                               timeout=30)

    async def rename(self, name):
        await self._rpc().call("change_machine_name", {"machine_name": name})


# ---------------------------------------------------------------- print files

def inspect_print_file(path):
    """Read meta.json from a .makerbot file."""
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read("meta.json"))


def check_print_file(path, status=None):
    """Return a list of human-readable problems that should block or warn before printing."""
    problems = []
    try:
        meta = inspect_print_file(path)
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        return [f"not a valid .makerbot file ({e})"]
    if meta.get("bot_type") != "replicator_b":
        problems.append(f"file was made for '{meta.get('bot_type')}', not a Replicator+")
    tool = meta.get("tool_type")
    if status and status.extruder:
        attached = status.extruder.tool_type
        if attached and tool and attached != tool:
            problems.append(
                f"file is for {EXTRUDERS.get(tool, tool)} but the printer has "
                f"{EXTRUDERS.get(attached, attached)} attached")
        if status.extruder.filament_present is False:
            problems.append("no filament detected in the extruder")
    return problems
