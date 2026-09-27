"""Keeps the server's printer connected (network or USB) and reports its status."""

import asyncio
import logging

from ..printer import AuthError, CertificateChanged, NotTrusted, Printer, PrinterError

log = logging.getLogger(__name__)


class PrinterHost:
    """Owns the printer connection for a headless server (`openbot serve`).

    For a network printer it reconnects with backoff; a USB printer is opened
    once (reopening can reset the board, see MarlinPrinter).
    """

    def __init__(self, *, host=None, usb_port=None, machine_id=None, trust=None,
                 credentials=None):
        self._net_host, self._usb_port, self._machine_id = host, usb_port, machine_id
        self._trust, self._credentials = trust, credentials
        self.backend = None
        self._status_cbs, self._conn_cbs = [], []
        self._task = None
        self.state = "disconnected"

    def on_status(self, callback):
        self._status_cbs.append(callback)

    def on_connection(self, callback):
        self._conn_cbs.append(callback)

    def _set_state(self, state):
        self.state = state
        for cb in list(self._conn_cbs):
            cb(state)

    def _status(self, status):
        for cb in list(self._status_cbs):
            try:
                cb(status)
            except Exception:  # noqa: BLE001
                log.exception("status listener failed")

    async def start(self):
        self._task = asyncio.ensure_future(self._keep_connected())

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self.backend:
            await self.backend.close()

    async def attach(self, backend):
        """Use an already-connected backend (the desktop app sharing its printer)."""
        self.backend = backend
        backend.on_status(self._status)
        self._set_state("connected")
        self._status(await backend.status())

    async def _keep_connected(self):
        delay = 2
        while True:
            try:
                await self._connect_once()
                delay = 2
                await self._wait_for_disconnect()
            except (NotTrusted, CertificateChanged, AuthError) as e:
                log.error("printer needs attention: %s (run `openbot pair` / `openbot cert "
                          "trust` on this machine)", e)
                self._set_state("disconnected")
                delay = 60
            except PrinterError as e:
                log.warning("printer unavailable: %s", e)
                self._set_state("disconnected")
                delay = min(delay * 2, 60)
            except asyncio.CancelledError:
                raise
            self.backend = None
            if self._usb_port:
                log.error("USB printer disconnected; restart the server to reconnect")
                return
            await asyncio.sleep(delay)

    async def _connect_once(self):
        if self._usb_port:
            from ..marlin import MarlinPrinter
            from ..marlin.backend import MarlinBackend
            printer = MarlinPrinter(self._usb_port, self._machine_id or "ender3_pro")
            await printer.connect()
            backend = MarlinBackend(printer)
        else:
            from ..local_backend import LocalBackend
            kwargs = {}
            if self._trust is not None:
                kwargs["trust"] = self._trust
            if self._credentials is not None:
                kwargs["credentials"] = self._credentials
            printer = Printer(self._net_host, **kwargs)
            await printer.connect()
            backend = LocalBackend(printer)
        self._lost = asyncio.Event()
        printer.on_disconnect(lambda *_: self._lost.set())
        await self.attach(backend)
        log.info("printer connected: %s", printer.info.name)

    async def _wait_for_disconnect(self):
        await self._lost.wait()
        self._set_state("disconnected")
