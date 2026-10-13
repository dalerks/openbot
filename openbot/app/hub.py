"""Several printers connected at once.

The hub keeps one PrinterController per printer and stands in for "the selected printer":
pages talk to the hub exactly as they would to a single controller, and see whichever
printer is selected. The others stay connected and keep printing in the background.
"""

import json
import logging
from functools import partial

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtGui import QImage

from .controller import PrinterController, SessionTrustStore

log = logging.getLogger(__name__)

# Controller signals the hub passes on for the selected printer.
FORWARDED = ("connection_changed", "status_changed", "camera_frame", "trust_required",
             "certificate_changed", "pairing_required", "console_line",
             "server_trust_required", "server_pairing", "queue_changed", "error")
# Signals that open a dialog: a background printer asking for one is selected first.
NEEDS_USER = ("trust_required", "certificate_changed", "pairing_required",
              "server_trust_required", "server_pairing")
# Connection kinds remembered and reconnected at start-up (not USB: opening a serial
# port can reset the printer's board and stop an SD print).
REMEMBERED = ("printer", "moonraker", "server")


class PrinterHub(QObject):
    connection_changed = Signal(str, str)
    status_changed = Signal(object)
    camera_frame = Signal(QImage)
    trust_required = Signal(object, object)
    certificate_changed = Signal(object, object)
    pairing_required = Signal(object)
    console_line = Signal(str)
    server_trust_required = Signal(object, str, int)
    server_pairing = Signal(str, int, str)
    queue_changed = Signal(list)
    error = Signal(str)
    printers_changed = Signal()      # a printer was added/removed or its state changed

    def __init__(self, trust=None, parent=None, remember=True):
        super().__init__(parent)
        self.trust = trust or SessionTrustStore()
        self.controllers: list[PrinterController] = []
        self._remember = remember
        self.grid_cameras = False      # the Cameras page is streaming every printer
        self.active = self._new_controller()

    # ------------------------------------------------------------ the selected printer

    def __getattr__(self, name):
        # Only reached for names the hub doesn't define: backend, status, run, supports…
        if name in ("active", "controllers") or name.startswith("__"):
            raise AttributeError(name)
        return getattr(self.active, name)

    def select(self, ctrl):
        """Show `ctrl` in the pages. The previously selected printer keeps running."""
        if ctrl is self.active or ctrl not in self.controllers:
            return
        old, self.active = self.active, ctrl
        if old.camera_running() and not self.grid_cameras:
            old.run(old.stop_camera(), what="camera")
        if old.key is None and old.state == "disconnected":
            self._drop(old)                    # an unused blank slot
        self.connection_changed.emit(ctrl.state, ctrl.message)
        if ctrl.status is not None:
            self.status_changed.emit(ctrl.status)
        if ctrl.remote:
            self.queue_changed.emit(ctrl.backend.queue)
        self.printers_changed.emit()

    def find(self, kind, address):
        """The controller for a connection, or None."""
        if kind == "moonraker":
            from ..moonraker import base_url
            from ..printer.errors import PrinterError
            try:
                address = base_url(address)
            except PrinterError:
                return None
        return next((c for c in self.controllers if c.key == (kind, address)), None)

    def printers(self):
        """Controllers that are, or are trying to be, connected to something."""
        return [c for c in self.controllers if c.key is not None]

    # ------------------------------------------------------------ connecting

    async def connect_to(self, host, *, select=True):
        ctrl = self._slot(("printer", host), select)
        if ctrl:
            await ctrl.connect_to(host)

    async def connect_usb(self, port, machine_id, *, select=True):
        ctrl = self._slot(("usb", port), select)
        if ctrl:
            await ctrl.connect_usb(port, machine_id)

    async def connect_moonraker(self, address, machine_id, username=None, password=None, *,
                                select=True):
        from ..moonraker import base_url
        from ..printer.errors import PrinterError
        try:
            key = ("moonraker", base_url(address))
        except PrinterError as e:
            return str(e)
        ctrl = self._slot(key, select, machine=machine_id)
        if ctrl:
            return await ctrl.connect_moonraker(address, machine_id, username, password)
        return None

    async def connect_server(self, host, port=8765, *, select=True):
        ctrl = self._slot(("server", f"{host}:{port}"), select)
        if ctrl:
            await ctrl.connect_server(host, port)

    async def pair_server_with_setup_code(self, host, port, code):
        ctrl = self._slot(("server", f"{host}:{port}"), True, force=True)
        await ctrl.pair_server_with_setup_code(host, port, code)

    def _slot(self, key, select, force=False, machine=None):
        """The controller to use for `key`: None if that printer is already connected
        (it is just selected), otherwise an idle one."""
        for c in self.controllers:
            if c.key == key:
                if c.state != "disconnected" and not force:
                    if select:
                        self.select(c)
                    return None
                ctrl = c
                break
        else:
            blank = self.active if self.active.key is None and \
                self.active.state == "disconnected" else None
            ctrl = blank or self._new_controller()
        ctrl.key, ctrl.machine_hint = key, machine
        if select:
            self.select(ctrl)
        self.printers_changed.emit()
        return ctrl

    async def disconnect(self, ctrl=None):
        """Disconnect a printer (the selected one by default). If it was selected, select
        another connected one if there is one."""
        ctrl = ctrl or self.active
        if ctrl is not self.active:
            self._forget(ctrl)
            await ctrl.disconnect()
            ctrl.key = None
            self._drop(ctrl)
            self.printers_changed.emit()
            return
        self._forget(ctrl)
        await ctrl.disconnect()
        ctrl.key = None
        others = [c for c in self.printers() if c is not ctrl]
        if others:
            self.select(next((c for c in others if c.state == "connected"), others[0]))
            self._drop(ctrl)
        self.printers_changed.emit()

    async def disconnect_all(self):
        for c in list(self.controllers):
            await c.disconnect()

    # ------------------------------------------------------------ controllers

    def _new_controller(self):
        ctrl = PrinterController(self.trust)
        ctrl.key = None
        ctrl.machine_hint = None
        ctrl.state, ctrl.message = "disconnected", "Not connected"
        for name in FORWARDED:
            getattr(ctrl, name).connect(partial(self._forward, ctrl, name))
        self.controllers.append(ctrl)
        return ctrl

    def _drop(self, ctrl):
        if ctrl in self.controllers and ctrl is not self.active:
            self.controllers.remove(ctrl)
            ctrl.deleteLater()

    def _forward(self, ctrl, name, *args):
        if name == "connection_changed":
            ctrl.state, ctrl.message = args
            if args[0] == "connected":
                self._save(ctrl)
            self.printers_changed.emit()
        elif name == "status_changed":
            self.printers_changed.emit()
        if ctrl is not self.active:
            if name in NEEDS_USER:
                self.select(ctrl)
            elif name == "error":
                self.error.emit(f"{label(ctrl)}: {args[0]}")
                return
            else:
                return
        getattr(self, name).emit(*args)

    # ------------------------------------------------------------ remembered printers

    def _settings_entry(self, ctrl):
        kind, target = ctrl.key or (None, None)
        if kind not in REMEMBERED:
            return None
        if kind == "moonraker":
            return {"kind": kind, "address": ctrl.moonraker[0] if ctrl.moonraker else target,
                    "machine": ctrl.machine_id or ctrl.machine_hint or "ender3_pro"}
        return {"kind": kind, "address": target}

    def _saved(self):
        try:
            return json.loads(QSettings().value("printers", "[]"))
        except (TypeError, ValueError):
            return []

    def _store(self, entries):
        if self._remember:
            QSettings().setValue("printers", json.dumps(entries))

    def _save(self, ctrl):
        if not self._remember:
            return
        entry = self._settings_entry(ctrl)
        if entry:
            saved = [e for e in self._saved()
                     if (e.get("kind"), e.get("address")) != (entry["kind"], entry["address"])]
            self._store(saved + [entry])

    def _forget(self, ctrl):
        if not self._remember:
            return
        entry = self._settings_entry(ctrl)
        if entry:
            self._store([e for e in self._saved()
                         if (e.get("kind"), e.get("address")) !=
                         (entry["kind"], entry["address"])])

    def reconnect_saved(self):
        """At start-up: reconnect every remembered printer (the first one is selected)."""
        saved = self._saved()
        if not saved:
            saved = _legacy_saved()
        for i, e in enumerate(saved):
            kind, addr, sel = e.get("kind"), e.get("address", ""), i == 0
            if kind == "printer":
                coro = self.connect_to(addr, select=sel)
            elif kind == "moonraker":
                coro = self.connect_moonraker(addr, e.get("machine") or "ender3_pro",
                                              select=sel)
            elif kind == "server":
                host, _, port = addr.rpartition(":")
                coro = self.connect_server(host, int(port or 8765), select=sel)
            else:
                continue
            self.active.run(coro, what=f"connecting to {addr}")


def _legacy_saved():
    """Settings from before multi-printer support: one last-used connection."""
    s = QSettings()
    kind = s.value("last_kind", "printer")
    if kind == "server" and s.value("last_server", ""):
        return [{"kind": "server", "address": s.value("last_server")}]
    if kind == "moonraker" and s.value("last_moonraker", ""):
        return [{"kind": "moonraker", "address": s.value("last_moonraker"),
                 "machine": s.value("last_moonraker_machine", "ender3_pro")}]
    if s.value("last_host", ""):
        return [{"kind": "printer", "address": s.value("last_host")}]
    return []


def label(ctrl):
    """Short name for a printer in the switcher and in background messages."""
    if ctrl.status is not None and ctrl.status.name:
        return ctrl.status.name
    info = getattr(ctrl.printer, "info", None)
    if info is not None and info.name:
        return info.name
    if ctrl.key:
        return ctrl.key[1]
    return "Printer"


def summary(ctrl):
    """'🟢 jnet3d · printing 45%' for the switcher."""
    icon = {"connected": "🟢", "connecting": "🟡", "reconnecting": "🟠"}.get(ctrl.state, "⚪️")
    text = f"{icon} {label(ctrl)}"
    p = ctrl.status.process if ctrl.status is not None and ctrl.state == "connected" else None
    if p is not None and not p.complete and p.step != "failed":
        text += f" · {p.progress:.0f}%" if p.progress is not None else f" · {p.step}"
    elif p is not None and p.step in ("completed", "failed"):
        text += " · done" if p.step == "completed" else " · ⚠️"
    elif ctrl.state != "connected":
        text += f" · {ctrl.state}"
    return text
