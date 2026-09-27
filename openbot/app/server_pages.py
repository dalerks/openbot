"""Pages for sharing a printer: Queue (jobs on an OpenBot server) and Share (run a server)."""

import asyncio
import os
import socket

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from ..backend import Role
from .pages import Page, _title, later

STATE_LABELS = {"queued": "Queued", "waiting_for_plate": "⏸ Waiting: clear the plate",
                "printing": "Printing", "done": "✓ Done", "failed": "✗ Failed",
                "cancelled": "Cancelled"}


class AppHost:
    """Lets the in-app server share the printer this app is connected to."""

    def __init__(self, ctrl):
        self.ctrl = ctrl

    @property
    def backend(self):
        return None if self.ctrl.remote else self.ctrl.backend

    def on_status(self, callback):
        self.ctrl.status_changed.connect(
            lambda st: None if self.ctrl.remote else callback(st))

    def on_connection(self, callback):
        self.ctrl.connection_changed.connect(lambda state, msg: callback(state))


# ================================================================ Queue

class QueuePage(Page):
    """Jobs waiting on the OpenBot server this app is connected to, or on the one it runs."""

    def __init__(self, ctrl, share_page, parent=None):
        super().__init__(ctrl, parent)
        self.share = share_page
        self.jobs = []
        lay = QVBoxLayout(self)
        lay.addWidget(_title("Queue"))
        self.where = QLabel("")
        lay.addWidget(self.where)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["File", "State", "Progress", "Submitted by",
                                              "Submitted"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.confirm = QPushButton("Plate Is Clear: Start")
        self.confirm.clicked.connect(self._confirm)
        self.cancel = QPushButton("Cancel Job")
        self.cancel.clicked.connect(lambda: self._job_action("cancel"))
        self.remove = QPushButton("Remove")
        self.remove.clicked.connect(lambda: self._job_action("remove"))
        self.photo = QPushButton("View Result Photo")
        self.photo.clicked.connect(self._view_photo)
        for w in (self.confirm, self.cancel, self.remove, self.photo):
            row.addWidget(w)
        lay.addLayout(row)
        ctrl.queue_changed.connect(self._show)
        share_page.local_queue_changed.connect(lambda jobs: self._on_connection("", ""))

    # which queue?
    def _local(self):
        return (not self.ctrl.remote) and self.share.server is not None

    def applies(self):
        return self.ctrl.remote or self.share.server is not None

    def visible_in_nav(self, state):
        return self.applies()

    def _on_connection(self, state, message):
        self.setEnabled(self.applies())
        self._refresh()

    def _refresh(self):
        if self._local():
            self._show(self.share.server.queue.public())
            self.where.setText(f"Jobs sent to this Mac's server ({self.share.server.name}).")
        elif self.ctrl.remote:
            self._show(self.ctrl.backend.queue)
            self.where.setText(f"Jobs on {self.ctrl.backend.server_name}. New prints you "
                               "send join this queue.")

    def _show(self, jobs):
        self.jobs = list(jobs or [])
        self.table.setRowCount(len(self.jobs))
        for r, j in enumerate(self.jobs):
            vals = (j["name"], STATE_LABELS.get(j["state"], j["state"]),
                    f"{j.get('progress', 0)}%" if j["state"] == "printing" else "",
                    j["submitted_by"], (j["submitted_at"] or "").replace("T", " ")[:16])
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))
        self._update_buttons()

    def _selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() \
            else []
        return self.jobs[rows[0].row()] if rows else None

    def _role(self):
        return self.ctrl.backend.role if self.ctrl.remote else Role.ADMIN

    def _update_buttons(self):
        j = self._selected()
        operator = self._role() >= Role.OPERATOR
        self.confirm.setEnabled(bool(j) and operator and j["state"] == "waiting_for_plate")
        self.cancel.setEnabled(bool(j) and operator and j["state"] in
                               ("queued", "waiting_for_plate", "printing"))
        self.remove.setEnabled(bool(j) and operator and j["state"] != "printing")
        self.photo.setEnabled(bool(j) and j.get("has_snapshot", False))

    async def _call(self, action, job_id):
        if self._local():
            q = self.share.server.queue
            if action == "confirm":
                await q.confirm_plate(job_id, socket.gethostname().split(".")[0])
            elif action == "cancel":
                await q.cancel(job_id)
            elif action == "remove":
                await q.remove(job_id)
        else:
            b = self.ctrl.backend
            fn = {"confirm": b.confirm_plate, "cancel": b.cancel_job,
                  "remove": b.remove_job}[action]
            await fn(job_id)

    def _confirm(self):
        j = self._selected()
        if not j:
            return
        from .dialogs import ConfirmPrintDialog
        dlg = ConfirmPrintDialog(f"<b>{j['name']}</b> from {j['submitted_by']}. The printer "
                                 "will heat and move.", [], self)
        task = None
        if self.ctrl.supports("camera") or (self._local() and self.ctrl.supports("camera")):
            async def photo():
                try:
                    frame = await self.ctrl.backend.snapshot()
                    dlg.set_photo(QImage.fromData(frame.data))
                except Exception:  # noqa: BLE001
                    dlg.photo.setText("(camera unavailable)")
            task = self.ctrl.run(photo(), what="camera")
        else:
            dlg.photo.setText("This printer has no camera. Check the build plate yourself.")
            dlg.photo.setMinimumSize(0, 0)
        if dlg.exec() != dlg.DialogCode.Accepted:
            if task:
                task.cancel()
            return
        self.ctrl.run(self._call("confirm", j["id"]), what="starting the job")

    def _job_action(self, action):
        j = self._selected()
        if j and QMessageBox.question(self, "Queue", f"{action.title()} {j['name']}?") == \
                QMessageBox.StandardButton.Yes:
            self.ctrl.run(self._call(action, j["id"]), what=f"{action} job")

    def _view_photo(self):
        j = self._selected()
        if not j:
            return

        async def go():
            if self._local():
                path = self.share.server.queue.get(j["id"]).snapshot
                data = open(path, "rb").read()
            else:
                data = await self.ctrl.backend.job_snapshot(j["id"])
            later(lambda: _show_image(self, j["name"], data))
        self.ctrl.run(go(), what="loading the photo")


def _show_image(parent, title, data):
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    lay = QVBoxLayout(dlg)
    lbl = QLabel()
    lbl.setPixmap(QPixmap.fromImage(QImage.fromData(data)))
    lay.addWidget(lbl)
    dlg.exec()


# ================================================================ Share

class SharePage(Page):
    """Run an OpenBot server for the connected printer, and manage who may use it.

    When this app is itself a client of a server as Admin, the same controls manage
    that server's clients and pairing requests.
    """
    needs_connection = False
    local_queue_changed = Signal(list)

    def __init__(self, ctrl, parent=None):
        super().__init__(ctrl, parent)
        self.server = None
        self.advertiser = None
        lay = QVBoxLayout(self)
        lay.addWidget(_title("Share"))
        self.share_box = QCheckBox("Share the connected printer with other Macs on this "
                                   "network")
        self.share_box.toggled.connect(self._toggle)
        lay.addWidget(self.share_box)
        self.info = QLabel("Other Macs running OpenBot can then find this printer under "
                           "Printers → OpenBot servers.")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.info)
        web_row = QHBoxLayout()
        self.web_btn = QPushButton("Open Web Page")
        self.web_btn.setToolTip("Status, camera, queue and devices in your browser; phones and "
                                "tablets on the network can use the same address")
        self.web_btn.clicked.connect(self._open_web)
        self.web_btn.setVisible(False)
        web_row.addWidget(self.web_btn)
        web_row.addStretch()
        lay.addLayout(web_row)
        self._web_token = None

        pbox = QGroupBox("Waiting to join: compare the code with the one on their screen")
        pl = QVBoxLayout(pbox)
        self.pending = QTableWidget(0, 3)
        self.pending.setHorizontalHeaderLabels(["Device", "Code", "Requested"])
        self.pending.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.pending.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pending.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        pl.addWidget(self.pending)
        prow = QHBoxLayout()
        self.role = QComboBox()
        for r in (Role.VIEWER, Role.OPERATOR, Role.ADMIN):
            self.role.addItem({Role.VIEWER: "Viewer (watch only)",
                               Role.OPERATOR: "Operator (print, filament)",
                               Role.ADMIN: "Admin (everything, incl. Wi-Fi)"}[r], int(r))
        self.role.setCurrentIndex(1)
        prow.addWidget(self.role)
        approve = QPushButton("Allow")
        approve.clicked.connect(lambda: self._decide(True))
        deny = QPushButton("Deny")
        deny.clicked.connect(lambda: self._decide(False))
        prow.addWidget(approve)
        prow.addWidget(deny)
        pl.addLayout(prow)
        lay.addWidget(pbox)

        cbox = QGroupBox("Allowed devices")
        cl = QVBoxLayout(cbox)
        self.clients = QTableWidget(0, 3)
        self.clients.setHorizontalHeaderLabels(["Device", "Role", "Last seen"])
        self.clients.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.clients.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.clients.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        cl.addWidget(self.clients)
        revoke = QPushButton("Remove Access")
        revoke.clicked.connect(self._revoke)
        cl.addWidget(revoke, alignment=Qt.AlignmentFlag.AlignRight)
        lay.addWidget(cbox)
        self._pending, self._clients = [], []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(3000)

    # --- sharing

    def _on_connection(self, state, message):
        self.share_box.setEnabled(not self.ctrl.remote)
        self.refresh()

    def _toggle(self, on):
        if on and self.server is None:
            self.ctrl.run(self._start(), what="starting the server")
        elif not on and self.server is not None:
            self.ctrl.run(self._stop(), what="stopping the server")

    async def _start(self):
        from ..printer.trust import default_data_dir
        from ..server.advertise import Advertiser
        from ..server.app import OpenBotServer
        server = OpenBotServer(default_data_dir(), AppHost(self.ctrl))
        server.queue.on_change(self.local_queue_changed.emit)
        # Env overrides exist for automated tests (keep them off the real network).
        port = await server.start(os.environ.get("OPENBOT_SERVER_BIND", "0.0.0.0"),
                                  int(os.environ.get("OPENBOT_SERVER_PORT", "8765")))
        self.server = server
        if not os.environ.get("OPENBOT_NO_BONJOUR"):
            self.advertiser = Advertiser(server.name, port, {"id": server.server_id,
                                                              "fp": server.fingerprint[:16],
                                                              "ver": 1})
            await self.advertiser.start()
        if self.ctrl.status:
            server.queue.status_changed(self.ctrl.status)
        fp = server.fingerprint.upper()
        self.info.setText(f"<b>Sharing as “{server.name}” on port {port}.</b><br>"
                          f"Web page: {server.web_url()} (any browser on your network)<br>"
                          f"Certificate SHA-256 {':'.join(fp[i:i + 2] for i in range(0, 16, 2))}"
                          "…<br>Keep this Mac awake while others print through it.")
        self.web_btn.setVisible(True)
        self._sharing_changed()

    def _sharing_changed(self):
        win = self.window()
        if hasattr(win, "update_nav"):
            win.update_nav()
        self.local_queue_changed.emit(self.server.queue.public() if self.server else [])
        self.refresh()

    async def _stop(self):
        server, self.server = self.server, None
        if self.advertiser:
            await self.advertiser.stop()
        if server:
            await server.stop()
        self.info.setText("Not sharing.")
        self.web_btn.setVisible(False)
        self._web_token = None
        self._sharing_changed()

    def _open_web(self):
        """Open the server's web page in the default browser, already signed in as Admin."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        if self.server is None:
            return
        if self._web_token is None:
            _, self._web_token = self.server.clients.add_local_admin("this Mac's browser")
        QMessageBox.information(
            self, "Web page",
            "Your browser will warn that the connection isn't private: the server uses its "
            "own certificate. Check the address is this Mac, then choose to continue.")
        QDesktopServices.openUrl(QUrl(f"https://localhost:{self.server.port}/"
                                      f"#token={self._web_token}"))

    # --- pairing / clients (local server, or the remote server when we're its Admin)

    def _admin_remote(self):
        return self.ctrl.remote and self.ctrl.backend.role >= Role.ADMIN

    def refresh(self):
        if self.server is not None:
            self._pending = self.server.clients.pending_requests()
            self._clients = [c.public() for c in self.server.clients.clients.values()
                             if not c.console]
            self._fill()
        elif self._admin_remote():
            async def go():
                self._pending = await self.ctrl.backend.pending_pairings()
                self._clients = [c for c in await self.ctrl.backend.list_clients()
                                 if not c.get("console")]
                self._fill()
            self.ctrl.run(go(), what="loading the server's devices")
        else:
            self._pending, self._clients = [], []
            self._fill()

    def _fill(self):
        self.pending.setRowCount(len(self._pending))
        for r, p in enumerate(self._pending):
            code = p["code"]
            for c, v in enumerate((p["name"], f"{code[:3]} {code[3:]}", f"{p['age_s']} s ago")):
                self.pending.setItem(r, c, QTableWidgetItem(v))
        self.clients.setRowCount(len(self._clients))
        for r, cl in enumerate(self._clients):
            for c, v in enumerate((cl["name"], cl["role_name"],
                                   (cl.get("last_seen") or "never").replace("T", " ")[:16])):
                self.clients.setItem(r, c, QTableWidgetItem(v))

    def _row(self, table, items):
        rows = table.selectionModel().selectedRows()
        return items[rows[0].row()] if rows and rows[0].row() < len(items) else None

    def _decide(self, allow):
        p = self._row(self.pending, self._pending)
        if not p:
            return
        role = Role(self.role.currentData())
        if self.server is not None:
            if allow:
                self.server.clients.approve(p["id"], role)
            else:
                self.server.clients.deny(p["id"])
            self.refresh()
        elif self._admin_remote():
            b = self.ctrl.backend
            self.ctrl.run(b.approve_pairing(p["id"], role) if allow else
                          b.deny_pairing(p["id"]), what="pairing decision")
            QTimer.singleShot(500, self.refresh)

    def _revoke(self):
        cl = self._row(self.clients, self._clients)
        if not cl or QMessageBox.question(self, "Remove access",
                                          f"Remove {cl['name']}'s access?") != \
                QMessageBox.StandardButton.Yes:
            return
        if self.server is not None:
            self.server.clients.revoke(cl["id"])
            self.refresh()
        elif self._admin_remote():
            self.ctrl.run(self.ctrl.backend.revoke_client(cl["id"]), what="remove access")
            QTimer.singleShot(500, self.refresh)
