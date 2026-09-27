"""Bridges the async printer library to Qt: one controller per connected printer."""

import asyncio
import logging
import traceback

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from ..local_backend import LocalBackend
from ..printer import (AuthError, CertificateChanged, NotTrusted, Printer, PrinterError)
from ..printer.trust import TrustStore

log = logging.getLogger(__name__)


class SessionTrustStore(TrustStore):
    """Disk trust store plus certificates trusted 'for this session only'."""

    def __init__(self, directory=None):
        super().__init__(directory)
        self._session = {}

    def get(self, serial):
        return self._session.get(serial) or super().get(serial)

    def trust_session(self, serial, cert):
        self._session[serial] = cert


class PrinterController(QObject):
    # state: "disconnected" | "connecting" | "connected" | "reconnecting"
    connection_changed = Signal(str, str)
    status_changed = Signal(object)          # PrinterStatus
    camera_frame = Signal(QImage)
    trust_required = Signal(object, object)  # CertInfo, PrinterInfo
    certificate_changed = Signal(object, object)  # trusted CertInfo, presented CertInfo
    pairing_required = Signal(object)        # PrinterInfo
    console_line = Signal(str)               # every line a USB printer sends
    server_trust_required = Signal(object, str, int)   # CertInfo, host, port
    server_pairing = Signal(str, int, str)   # host, port, code to compare on the server
    queue_changed = Signal(list)             # jobs on the connected OpenBot server
    error = Signal(str)

    def __init__(self, trust=None, parent=None):
        super().__init__(parent)
        self.trust = trust or SessionTrustStore()
        self.printer: Printer | None = None
        self.backend: LocalBackend | None = None
        self.host = None
        self.usb_port = None
        self.status = None
        self._camera_task = None
        self._reconnect_task = None
        self._closing = False

    # ------------------------------------------------------------ helpers

    def run(self, coro, *, what="operation"):
        """Schedule a coroutine; errors are reported through the `error` signal."""
        async def wrapper():
            try:
                return await coro
            except asyncio.CancelledError:
                raise
            except PrinterError as e:
                self.error.emit(f"{what} failed: {e}")
            except Exception as e:  # noqa: BLE001
                log.error("%s crashed:\n%s", what, traceback.format_exc())
                self.error.emit(f"{what} failed: {e!r}")
        return asyncio.ensure_future(wrapper())

    @property
    def connected(self):
        return self.printer is not None and self.printer.connected

    @property
    def capabilities(self):
        return self.backend.capabilities if self.backend else frozenset()

    def supports(self, capability):
        return capability in self.capabilities

    @property
    def remote(self):
        """Connected through an OpenBot server (rather than to a printer directly)."""
        return bool(getattr(self, "_server_addr", None)) and self.backend is not None

    @property
    def machine_id(self):
        return self.backend.machine_id if self.backend else None

    def _new_printer(self, host):
        """Accept 'ip' or 'ip:port' (the port is the plaintext JSON-RPC port, 9999 by default)."""
        name, _, port = host.rpartition(":") if host.count(":") == 1 else (host, "", "")
        if name and port.isdigit():
            return Printer(name, port=int(port), trust=self.trust)
        return Printer(host, trust=self.trust)

    # ------------------------------------------------------------ connecting

    async def connect_to(self, host):
        await self.disconnect()
        self._closing = False
        self.host = host
        self.connection_changed.emit("connecting", f"Connecting to {host}…")
        printer = self._new_printer(host)
        try:
            await printer.connect()
        except NotTrusted as e:
            self.connection_changed.emit("disconnected", "Certificate not trusted yet")
            self.printer = printer
            self.trust_required.emit(e.cert, printer.info)
            return
        except CertificateChanged as e:
            self.connection_changed.emit("disconnected", "Printer identity changed")
            self.printer = printer
            self.certificate_changed.emit(e.trusted, e.presented)
            return
        except AuthError:
            self.connection_changed.emit("disconnected", "Not paired yet")
            self.printer = printer
            self.pairing_required.emit(printer.info)
            return
        except PrinterError as e:
            self.connection_changed.emit("disconnected", str(e))
            self.error.emit(str(e))
            return
        await self._attached(printer)

    async def trust_and_connect(self, cert, *, session_only=False, keychain=False):
        info = self.printer.info
        if session_only:
            self.trust.trust_session(info.serial, cert)
        else:
            self.trust.trust(info.serial, cert)
            if keychain:
                from ..printer.trust import add_to_keychain
                add_to_keychain(cert, self.host)
                self.trust.mark_keychain(info.serial, self.host)
        await self.connect_to(self.host)

    async def pair(self):
        printer = self.printer or self._new_printer(self.host)
        self.connection_changed.emit("connecting", "Waiting for the knob press on the printer…")
        try:
            await printer.pair()
        except PrinterError as e:
            self.connection_changed.emit("disconnected", str(e))
            raise
        await self._attached(printer)

    async def connect_usb(self, port, machine_id):
        """Connect a Marlin printer (Ender-3 family) on a USB serial port."""
        from ..marlin import MarlinPrinter
        from ..marlin.backend import MarlinBackend
        await self.disconnect()
        self._closing = False
        self.host, self.usb_port = None, port
        self.connection_changed.emit("connecting", f"Opening {port}…")
        printer = MarlinPrinter(port, machine_id)
        try:
            await printer.connect()
        except PrinterError as e:
            self.connection_changed.emit("disconnected", str(e))
            self.error.emit(str(e))
            return
        printer.conn.on_line(self.console_line.emit)
        await self._attached(printer, MarlinBackend(printer),
                             f"{printer.info.name} · USB · {printer.info.firmware}")

    # ------------------------------------------------------------ OpenBot servers

    async def connect_server(self, host, port=8765):
        """Connect to an OpenBot server. Trust its certificate and pair on first use."""
        from ..printer.auth import Credentials, default_credential_store
        from ..remote import client as rc
        await self.disconnect()
        self._closing = False
        self.host, self.usb_port = f"{host}:{port}", None
        self._server_addr = (host, port)
        self.connection_changed.emit("connecting", f"Connecting to OpenBot server {host}…")
        key = rc.trust_key(host, port)
        try:
            ctx = await rc.server_context(host, port, self.trust)
        except NotTrusted as e:
            self.connection_changed.emit("disconnected", "Server certificate not trusted yet")
            self.server_trust_required.emit(e.cert, host, port)
            return
        except CertificateChanged as e:
            self.connection_changed.emit("disconnected", "Server identity changed")
            self.certificate_changed.emit(e.trusted, e.presented)
            return
        store = default_credential_store()
        creds = store.get(key)
        if creds is None:
            import socket
            name = socket.gethostname().split(".")[0]
            info = await rc.server_info(host, port, ctx)
            req = await rc.request_pairing(host, port, ctx, name)
            self.server_pairing.emit(host, port, req["code"])
            self.connection_changed.emit(
                "connecting", f"Waiting for approval on {info['server_name']} "
                              f"(code {req['code']})…")
            try:
                got = await rc.wait_for_approval(host, port, ctx, req["id"])
            except PrinterError as e:
                self.connection_changed.emit("disconnected", str(e))
                raise
            store.put(key, Credentials(name, got["token"], str(got["role"])))
            creds = store.get(key)
        try:
            backend = await rc.RemoteBackend.connect(host, port, creds.local_secret, ctx)
        except PrinterError as e:
            if "pair again" in str(e):
                store.delete(key)
            self.connection_changed.emit("disconnected", str(e))
            raise
        backend.on_queue(self.queue_changed.emit)
        self.queue_changed.emit(backend.queue)
        await self._attached(backend, backend,
                             f"{backend.info.name} · via {backend.server_name} "
                             f"({backend.role.name.lower()})")

    async def pair_server_with_setup_code(self, host, port, code):
        """First Admin of a headless server: the setup code it printed at start-up."""
        from ..printer.auth import Credentials, default_credential_store
        from ..remote import client as rc
        import socket
        try:
            ctx = await rc.server_context(host, port, self.trust)
        except NotTrusted as e:
            self._pending_setup_code = code          # used once the cert is trusted
            self.server_trust_required.emit(e.cert, host, port)
            return
        name = socket.gethostname().split(".")[0]
        req = await rc.request_pairing(host, port, ctx, name, setup_code=code)
        got = await rc.wait_for_approval(host, port, ctx, req["id"])
        default_credential_store().put(rc.trust_key(host, port),
                                       Credentials(name, got["token"], str(got["role"])))
        await self.connect_server(host, port)

    async def trust_server_and_connect(self, cert, host, port):
        from ..remote.client import trust_key
        self.trust.trust(trust_key(host, port), cert)
        code, self._pending_setup_code = getattr(self, "_pending_setup_code", None), None
        if code:
            await self.pair_server_with_setup_code(host, port, code)
        else:
            await self.connect_server(host, port)

    async def _attached(self, printer, backend=None, label=None):
        self.printer = printer
        self.backend = backend or LocalBackend(printer)
        printer.on_status(self._on_status)
        printer.on_disconnect(self._on_disconnect)
        self.status = await self.backend.status()
        self.status_changed.emit(self.status)
        self.connection_changed.emit(
            "connected", label or
            f"{printer.info.name} · {self.host} · fw {printer.info.firmware}")

    async def refresh_status(self):
        self.status = await self.backend.status()
        self.status_changed.emit(self.status)
        return self.status

    def _on_status(self, status):
        self.status = status
        self.status_changed.emit(status)

    def _on_disconnect(self, err):
        self._camera_task = None
        if self._closing:
            return
        if getattr(self, "_server_addr", None) and self.host == "%s:%s" % self._server_addr:
            self.printer = self.backend = None
            self.connection_changed.emit("disconnected", f"Server connection lost ({err})")
            return
        if self.usb_port:
            # Reopening a serial port can reset the printer's board (and kill an SD
            # print), so USB printers are never reconnected automatically.
            self.printer = self.backend = None
            self.connection_changed.emit("disconnected", f"USB connection lost ({err})")
            return
        self.connection_changed.emit("reconnecting", "Connection lost, reconnecting…")
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.ensure_future(self._reconnect_loop())

    async def _reconnect_loop(self):
        delay = 2
        while not self._closing:
            await asyncio.sleep(delay)
            printer = self._new_printer(self.host)
            try:
                await printer.connect()
            except (NotTrusted, CertificateChanged, AuthError) as e:
                self.connection_changed.emit("disconnected", str(e))
                return
            except PrinterError:
                delay = min(delay * 2, 30)
                continue
            await self._attached(printer)
            return

    async def disconnect(self):
        self._closing = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
        await self.stop_camera()
        if self.printer:
            await self.printer.close()
        self.printer = None
        self.backend = None
        self.usb_port = None
        self._server_addr = None
        self.connection_changed.emit("disconnected", "Not connected")

    # ------------------------------------------------------------ camera

    def camera_running(self):
        return self._camera_task is not None and not self._camera_task.done()

    def start_camera(self):
        if self.camera_running() or not self.connected:
            return
        self._camera_task = asyncio.ensure_future(self._camera_loop())

    async def _camera_loop(self):
        try:
            async for frame in self.backend.camera_stream():
                img = QImage.fromData(frame.data) if frame.is_jpeg else _yuyv_to_qimage(frame)
                if not img.isNull():
                    self.camera_frame.emit(img)
        except asyncio.CancelledError:
            raise
        except PrinterError as e:
            self.error.emit(f"camera: {e}")

    async def stop_camera(self):
        task, self._camera_task = self._camera_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def snapshot(self):
        frame = await self.backend.snapshot()
        return frame


def _yuyv_to_qimage(frame):
    """Older printers' 320x240 camera sends YUYV; convert to RGB."""
    w, h, d = frame.width, frame.height, frame.data
    out = bytearray(w * h * 3)
    o = 0
    for i in range(0, min(len(d), w * h * 2) - 3, 4):
        y0, u, y1, v = d[i] - 16, d[i + 1] - 128, d[i + 2] - 16, d[i + 3] - 128
        for y in (y0, y1):
            c = 298 * y
            out[o] = max(0, min(255, (c + 409 * v + 128) >> 8))
            out[o + 1] = max(0, min(255, (c - 100 * u - 208 * v + 128) >> 8))
            out[o + 2] = max(0, min(255, (c + 516 * u + 128) >> 8))
            o += 3
    return QImage(bytes(out), w, h, w * 3, QImage.Format.Format_RGB888).copy()
