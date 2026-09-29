"""OpenBot desktop app entry point:  `openbot-app`  or  `python -m openbot.app`."""

import asyncio
import logging
import os
import sys

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QListWidget, QMainWindow,
                               QMessageBox, QStackedWidget, QStatusBar, QVBoxLayout, QWidget)

from .. import __version__
from . import dialogs
from .controller import PrinterController
from .pages import (ConsolePage, MaintenancePage, MonitorPage, NetworkPage, PreparePage,
                    PrintersPage)
from .server_pages import QueuePage, SharePage

PAGES = ("Printers", "Prepare", "Monitor", "Queue", "Network", "Maintenance", "Console",
         "Share")


class MainWindow(QMainWindow):
    def __init__(self, ctrl):
        super().__init__()
        self.ctrl = ctrl
        self.setWindowTitle("OpenBot")
        self.resize(1180, 760)
        central = QWidget()
        lay = QHBoxLayout(central)
        self.nav = QListWidget()
        self.nav.addItems(PAGES)
        self.nav.setFixedWidth(160)
        lay.addWidget(self.nav)
        self.stack = QStackedWidget()
        self.pages = {
            "Printers": PrintersPage(ctrl), "Prepare": PreparePage(ctrl),
            "Monitor": MonitorPage(ctrl), "Network": NetworkPage(ctrl),
            "Maintenance": MaintenancePage(ctrl), "Console": ConsolePage(ctrl),
            "Share": SharePage(ctrl),
        }
        self.pages["Queue"] = QueuePage(ctrl, self.pages["Share"])
        for name in PAGES:
            self.stack.addWidget(self.pages[name])
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        self._build_menus()
        self.setStatusBar(QStatusBar())
        self.conn = QLabel("⚪️ Not connected")
        self.statusBar().addPermanentWidget(self.conn)

        ctrl.connection_changed.connect(self._connection)
        ctrl.error.connect(self._error)
        # Dialogs are opened on the next event-loop turn: these signals are emitted from
        # inside running asyncio tasks, and a nested modal loop there would re-enter them.
        ctrl.trust_required.connect(lambda c, i: QTimer.singleShot(0, lambda: self._trust(c, i)))
        ctrl.certificate_changed.connect(
            lambda t, p: QTimer.singleShot(0, lambda: self._cert_changed(t, p)))
        ctrl.pairing_required.connect(lambda i: QTimer.singleShot(0, lambda: self._pair(i)))
        ctrl.server_trust_required.connect(
            lambda c, h, p: QTimer.singleShot(0, lambda: self._trust_server(c, h, p)))
        ctrl.server_pairing.connect(self._server_code)
        self._code_box = None
        ctrl.connection_changed.emit("disconnected", "Not connected")

    def _build_menus(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QAction, QDesktopServices
        from .. import project
        help_menu = self.menuBar().addMenu("Help")
        about = QAction("About OpenBot", self)
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(self._about)
        help_menu.addAction(about)
        donate = QAction(f"Support OpenBot (suggested ${project.SUGGESTED_DONATION_USD})…", self)
        donate.triggered.connect(self._donate)
        help_menu.addAction(donate)
        home = QAction("OpenBot Project Page", self)
        home.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(project.HOMEPAGE_URL)))
        help_menu.addAction(home)
        source = QAction("Source Code on GitHub", self)
        source.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(project.REPO_URL)))
        help_menu.addAction(source)
        site = QAction(f"{project.PUBLISHER} Website", self)
        site.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(project.PUBLISHER_URL)))
        help_menu.addAction(site)

    def _about(self):
        from .. import project
        QMessageBox.about(
            self, "About OpenBot",
            f"<h3>OpenBot {__version__}</h3>"
            "<p>Host software for the MakerBot Replicator+ and Creality Ender-3 printers.</p>"
            f"<p>A <a href='{project.PUBLISHER_URL}'>{project.PUBLISHER}</a> project.<br>"
            f"Free software under the GNU GPL v3 or later: "
            f"<a href='{project.REPO_URL}'>source code</a>.</p>"
            f"<p><a href='{project.HOMEPAGE_URL}'>Project page</a></p>"
            "<p>Includes OrcaSlicer (AGPL-3.0). Not affiliated with MakerBot, UltiMaker or "
            "Creality.</p>")

    def _donate(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from .. import project
        url = project.donate_url()
        if url:
            QDesktopServices.openUrl(QUrl(url))
        else:
            QMessageBox.information(
                self, "Support OpenBot",
                f"Thank you! Donations (suggested ${project.SUGGESTED_DONATION_USD}) are being "
                f"set up. For now, visit {project.PUBLISHER_URL}.")

    def show_page(self, name):
        self.nav.setCurrentRow(PAGES.index(name))

    def _connection(self, state, message):
        icon = {"connected": "🟢", "connecting": "🟡", "reconnecting": "🟠"}.get(state, "⚪️")
        self.conn.setText(f"{icon} {message}")
        if state in ("connected", "disconnected") and self._code_box is not None:
            self._code_box.close()
            self._code_box = None
        self._state = state
        self.update_nav()

    def update_nav(self):
        """Only list pages that apply (no Network page for a USB Ender, no Console for the
        MakerBot, Queue only with a server); with nothing connected, hide printer-specific
        ones."""
        state = getattr(self, "_state", "disconnected")
        for i, name in enumerate(PAGES):
            visible = self.pages[name].visible_in_nav(state)
            self.nav.item(i).setHidden(not visible)
            if not visible and self.nav.currentRow() == i:
                self.nav.setCurrentRow(0)

    def _error(self, message):
        self.statusBar().showMessage(message, 10000)
        if "failed" in message and "camera" not in message:
            # Emitted from inside asyncio tasks: open the dialog on the next turn.
            QTimer.singleShot(0, lambda: QMessageBox.warning(self, "OpenBot", message))

    def _trust(self, cert, info):
        dlg = dialogs.TrustDialog(cert, info, self)
        if dlg.exec() == dlg.DialogCode.Accepted and dlg.choice:
            self.ctrl.run(self.ctrl.trust_and_connect(
                cert, session_only=dlg.choice == dlg.SESSION,
                keychain=dlg.keychain.isChecked()), what="connect")

    def _trust_server(self, cert, host, port):
        from types import SimpleNamespace
        info = SimpleNamespace(name=f"OpenBot server at {host}:{port}", serial=cert.subject)
        dlg = dialogs.TrustDialog(cert, info, self)
        dlg.keychain.setVisible(False)
        if dlg.exec() == dlg.DialogCode.Accepted and dlg.choice:
            self.ctrl.run(self.ctrl.trust_server_and_connect(cert, host, port),
                          what="connecting to server")

    def _server_code(self, host, port, code):
        box = QMessageBox(QMessageBox.Icon.Information, "Waiting for approval",
                          f"Asked to join the OpenBot server at {host}.\n\n"
                          f"Code: {code[:3]} {code[3:]}\n\nCheck that the same code is "
                          "shown on the server (its Share page, or `openbot server pending` "
                          "on a headless server), and ask its owner to allow this Mac.",
                          QMessageBox.StandardButton.Ok, self)
        box.setModal(False)
        box.show()
        self._code_box = box

    def _cert_changed(self, trusted, presented):
        dlg = dialogs.CertificateChangedDialog(trusted, presented, self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            self.ctrl.run(self.ctrl.trust_and_connect(presented), what="connect")

    def _pair(self, info):
        if QMessageBox.question(
                self, "Pair with printer",
                f"OpenBot isn't paired with {info.name} yet. Pair now? You'll need to press "
                "the knob on the printer.") != QMessageBox.StandardButton.Yes:
            return
        dlg = dialogs.PairDialog(info, self)
        task = self.ctrl.run(self.ctrl.pair(), what="pairing")
        task.add_done_callback(lambda _: dlg.accept())
        if dlg.exec() != dlg.DialogCode.Accepted and not task.done():
            task.cancel()

    def closeEvent(self, e):
        self.ctrl.run(self.ctrl.disconnect(), what="disconnect")
        super().closeEvent(e)


def self_test():
    """Packaged-app check: every subsystem imports and basic objects work (no printer)."""
    import platform
    import keyring
    from .. import drives, machines  # noqa: F401
    from ..marlin import list_ports
    from ..remote.client import RemoteBackend  # noqa: F401
    from ..server.app import OpenBotServer  # noqa: F401
    from ..slicing import orca_profiles, profiles
    from ..slicing.orca import SlicerError, find_orcaslicer
    list_ports()
    profiles.base_profile()
    try:
        orca = find_orcaslicer()
        orca_profiles.resolve("Creality", "machine", "Creality Ender-3 Pro 0.4 nozzle")
    except SlicerError:
        orca = "not bundled"
    print(f"OpenBot {__version__} self-test OK on {platform.machine()} "
          f"(keyring: {type(keyring.get_keyring()).__name__}, OrcaSlicer: {orca})")
    stl = os.environ.get("OPENBOT_SELFTEST_STL")
    if stl:
        import tempfile
        from ..slicing import PrintSettings, slice_model
        for machine in ("replicator_plus", "ender3_pro"):
            ext = machines.get(machine).file_extension
            out = os.path.join(tempfile.mkdtemp(), "selftest" + ext)
            r = slice_model(stl, out, PrintSettings(machine=machine, quality="draft"))
            print(f"  sliced for {machine}: {r.layers} layers, {r.duration_s / 60:.0f} min")


def main(argv=None):
    if "--self-test" in (argv or sys.argv):
        self_test()
        return
    import qasync
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = QApplication(argv or sys.argv)
    app.setApplicationName("OpenBot")
    app.setOrganizationName("OpenBot")
    app.setApplicationVersion(__version__)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    ctrl = PrinterController()
    win = MainWindow(ctrl)
    win.show()
    settings = QSettings()
    if "--no-autoconnect" not in sys.argv:
        if settings.value("last_kind", "printer") == "server" and \
                settings.value("last_server", ""):
            host, _, port = settings.value("last_server").rpartition(":")
            ctrl.run(ctrl.connect_server(host, int(port or 8765)), what="connect")
        elif settings.value("last_host", ""):
            ctrl.run(ctrl.connect_to(settings.value("last_host")), what="connect")
    closing = asyncio.Event()
    app.aboutToQuit.connect(closing.set)
    with loop:
        loop.run_until_complete(closing.wait())


if __name__ == "__main__":
    main()
