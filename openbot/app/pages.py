"""The app's pages: Printers, Prepare, Monitor, Network, Maintenance."""

import asyncio
import os
import time

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox,
                               QFileDialog, QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                               QMessageBox,
                               QProgressBar, QPushButton, QRadioButton, QSlider, QSpinBox,
                               QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import backend as caps
from .. import drives, machines
from ..printer import PrinterError, discovery
from ..printer.client import check_print_file, inspect_print_file
from ..slicing import mesh as mesh_mod
from ..slicing.plate import Plate
from ..slicing.profiles import QUALITY, QUALITY_LABELS, SUPPORT_STYLES, PrintSettings
from . import dialogs
from .viewer3d import PlateView

IPV4 = r"^((25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(25[0-5]|2[0-4]\d|1?\d?\d)$"


def _title(text):
    lbl = QLabel(f"<h2>{text}</h2>")
    return lbl


def later(fn):
    """Run fn on the next event-loop turn.

    Modal dialogs must never open inside a running asyncio task: their nested Qt event
    loop would make qasync re-enter other tasks ("Cannot enter into task ...").
    """
    QTimer.singleShot(0, fn)


def _fmt_duration(seconds):
    h, m = divmod(round(seconds / 60), 60)
    return f"{h} h {m} min" if h else f"{m} min"


class Page(QWidget):
    """Base: pages that need a connected printer get enabled/disabled automatically.

    `capability` (optional) names what the connected printer must support for this
    page to apply at all (e.g. "network"); the main window hides such pages otherwise.
    """
    needs_connection = True
    capability = None
    min_role = None          # e.g. Role.ADMIN: hidden from clients of a server below it

    def __init__(self, ctrl, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        ctrl.connection_changed.connect(self._on_connection)

    def applies(self):
        return self.capability is None or not self.ctrl.connected or \
            self.ctrl.supports(self.capability)

    def visible_in_nav(self, state):
        """Listed in the sidebar? Pages tied to a capability only show when the connected
        printer has it."""
        if self.min_role is not None and state == "connected" and \
                self.ctrl.backend is not None and self.ctrl.backend.role < self.min_role:
            return False
        return self.capability is None or (state == "connected" and
                                           self.ctrl.supports(self.capability))

    def _on_connection(self, state, message):
        if self.needs_connection:
            self.setEnabled(state == "connected" and self.applies())


# ================================================================ Printers

class PrintersPage(Page):
    needs_connection = False

    def __init__(self, ctrl, parent=None):
        super().__init__(ctrl, parent)
        self.settings = QSettings()
        lay = QVBoxLayout(self)
        lay.addWidget(_title("Printers"))
        self.state = QLabel("Not connected")
        lay.addWidget(self.state)
        self.conveyor = QLabel("⚠️ MakerBot Print's background service is running. It blocks "
                               "one discovery method; OpenBot will use Bonjour instead.")
        self.conveyor.setWordWrap(True)
        self.conveyor.setVisible(discovery.conveyor_running())
        lay.addWidget(self.conveyor)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Address", "Serial", "Firmware", "Found via"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self._connect_selected)
        lay.addWidget(self.table)

        row = QHBoxLayout()
        self.find_btn = QPushButton("Find Printers")
        self.find_btn.clicked.connect(self._discover)
        row.addWidget(self.find_btn)
        self.ip = QLineEdit(self.settings.value("last_host", ""))
        self.ip.setPlaceholderText("or enter an IP address")
        row.addWidget(self.ip, 1)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setDefault(True)
        self.connect_btn.clicked.connect(self._connect)
        row.addWidget(self.connect_btn)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(lambda: ctrl.run(ctrl.disconnect(), what="disconnect"))
        row.addWidget(self.disconnect_btn)
        lay.addLayout(row)
        self.found = []

        usb = QGroupBox("USB printers (Creality Ender-3 and other Marlin printers)")
        ul = QGridLayout(usb)
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(320)
        ul.addWidget(QLabel("Port:"), 0, 0)
        ul.addWidget(self.port, 0, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_ports)
        ul.addWidget(refresh, 0, 2)
        self.usb_machine = QComboBox()
        for m in machines.MACHINES.values():
            if m.connection == "usb":
                self.usb_machine.addItem(m.name, m.id)
        last_machine = self.settings.value("last_usb_machine", "ender3_pro")
        self.usb_machine.setCurrentIndex(max(0, self.usb_machine.findData(last_machine)))
        ul.addWidget(QLabel("Printer:"), 1, 0)
        ul.addWidget(self.usb_machine, 1, 1)
        self.usb_btn = QPushButton("Connect USB")
        self.usb_btn.clicked.connect(self._connect_usb)
        ul.addWidget(self.usb_btn, 1, 2)
        note = QLabel("Tip: for long prints, save to an SD card from the Prepare page. A USB "
                      "print stops if this Mac sleeps or the cable is disturbed.")
        note.setWordWrap(True)
        ul.addWidget(note, 2, 0, 1, 3)
        lay.addWidget(usb)

        srv = QGroupBox("OpenBot servers (a printer shared by another Mac or a Raspberry Pi)")
        sl = QGridLayout(srv)
        self.servers = QListWidget()
        self.servers.setMaximumHeight(90)
        self.servers.itemDoubleClicked.connect(lambda *_: self._connect_server())
        self.servers.currentRowChanged.connect(self._server_selected)
        sl.addWidget(self.servers, 0, 0, 1, 4)
        self.server_addr = QLineEdit(self.settings.value("last_server", ""))
        self.server_addr.setPlaceholderText("host or host:port (default port 8765)")
        sl.addWidget(self.server_addr, 1, 0, 1, 2)
        find_srv = QPushButton("Find Servers")
        find_srv.clicked.connect(self._find_servers)
        sl.addWidget(find_srv, 1, 2)
        conn_srv = QPushButton("Connect to Server")
        conn_srv.clicked.connect(self._connect_server)
        sl.addWidget(conn_srv, 1, 3)
        setup = QPushButton("I have a setup code…")
        setup.setToolTip("A new headless server (e.g. a Raspberry Pi) prints a setup code; "
                         "entering it makes this Mac its first Admin.")
        setup.clicked.connect(self._setup_code)
        sl.addWidget(setup, 2, 3)
        lay.addWidget(srv)
        lay.addStretch()
        self._found_servers = []
        self._refresh_ports()

    def _refresh_ports(self):
        from ..marlin import list_ports
        current = self.port.currentText()
        self.port.clear()
        try:
            ports = list_ports()
        except Exception:  # noqa: BLE001 - pyserial missing or no permission
            ports = []
        for sp in ports:
            self.port.addItem(f"{sp.device}  ({sp.description})", sp.device)
        last = self.settings.value("last_usb_port", "")
        idx = self.port.findData(last)
        if idx >= 0:
            self.port.setCurrentIndex(idx)
        elif current and self.port.findText(current) < 0 and not ports:
            self.port.setEditText(current)

    def _selected_port(self):
        data = self.port.currentData()
        text = self.port.currentText().strip()
        return data if data and text.startswith(data) else text.split("  (")[0]

    def _connect_usb(self):
        port = self._selected_port()
        if not port:
            QMessageBox.information(self, "USB", "Plug the printer in with a USB cable, "
                                                 "switch it on, and click Refresh.")
            return
        if QMessageBox.question(
                self, "Connect over USB",
                "Opening the USB connection restarts some printers' control boards.\n\n"
                "Don't connect while the printer is printing from its SD card: that print "
                "would stop. Continue?") != QMessageBox.StandardButton.Yes:
            return
        machine_id = self.usb_machine.currentData()
        self.settings.setValue("last_usb_port", port)
        self.settings.setValue("last_usb_machine", machine_id)
        self.ctrl.run(self.ctrl.connect_usb(port, machine_id), what="USB connect")

    def _on_connection(self, state, message):
        icon = {"connected": "🟢", "connecting": "🟡", "reconnecting": "🟠"}.get(state, "⚪️")
        self.state.setText(f"{icon} {message}")
        self.disconnect_btn.setEnabled(state != "disconnected")
        if state == "connected" and self.ctrl.host:
            if self.ctrl.remote:
                self.settings.setValue("last_server", self.ctrl.host)
                self.settings.setValue("last_kind", "server")
            elif not self.ctrl.usb_port:
                self.settings.setValue("last_host", self.ctrl.host)
                self.settings.setValue("last_kind", "printer")

    # --- OpenBot servers

    @staticmethod
    def _split_addr(text):
        text = text.strip()
        if text.count(":") == 1:
            host, port = text.split(":")
            if port.isdigit():
                return host, int(port)
        return text, 8765

    def _find_servers(self):
        from ..server.advertise import browse

        async def go():
            self._found_servers = await browse(3.0)
            self.servers.clear()
            for sv in self._found_servers:
                self.servers.addItem(f"{sv['name']}  —  {sv['host']}:{sv['port']}")
            if not self._found_servers:
                self.servers.addItem("(no OpenBot servers found; enter an address below)")
        self.ctrl.run(go(), what="finding servers")

    def _server_selected(self, row):
        if 0 <= row < len(self._found_servers):
            sv = self._found_servers[row]
            self.server_addr.setText(f"{sv['host']}:{sv['port']}")

    def _connect_server(self):
        host, port = self._split_addr(self.server_addr.text())
        if host:
            self.ctrl.run(self.ctrl.connect_server(host, port), what="connecting to server")

    def _setup_code(self):
        from PySide6.QtWidgets import QInputDialog
        host, port = self._split_addr(self.server_addr.text())
        if not host:
            QMessageBox.information(self, "Setup code", "Enter the server's address first.")
            return
        code, ok = QInputDialog.getText(self, "Setup code",
                                        f"Setup code printed by the server at {host}:")
        if ok and code.strip():
            self.ctrl.run(self.ctrl.pair_server_with_setup_code(host, port, code.strip()),
                          what="pairing with the setup code")

    def _discover(self):
        self.find_btn.setEnabled(False)
        self.find_btn.setText("Searching…")

        async def go():
            try:
                self.found = await discovery.discover(3.0)
            finally:
                self.find_btn.setEnabled(True)
                self.find_btn.setText("Find Printers")
            self.table.setRowCount(len(self.found))
            for r, p in enumerate(self.found):
                for c, v in enumerate((p.name, p.ip or "?", p.serial, p.firmware, p.source)):
                    self.table.setItem(r, c, QTableWidgetItem(v))
            if self.found:
                self.table.selectRow(0)
                self.ip.setText(self.found[0].ip or "")
        self.ctrl.run(go(), what="discovery")

    def _connect_selected(self):
        r = self.table.currentRow()
        if 0 <= r < len(self.found) and self.found[r].ip:
            self.ip.setText(self.found[r].ip)
            self._connect()

    def _connect(self):
        host = self.ip.text().strip()
        if host:
            self.ctrl.run(self.ctrl.connect_to(host), what="connect")


# ================================================================ Prepare

class PreparePage(Page):
    needs_connection = False

    def __init__(self, ctrl, parent=None):
        super().__init__(ctrl, parent)
        self.plate = Plate(machines.get(machines.DEFAULT_MACHINE).bed)
        self.result_path = None
        self.result_machine = None
        self._machine_id = machines.DEFAULT_MACHINE
        lay = QHBoxLayout(self)

        left = QVBoxLayout()
        left.addWidget(_title("Prepare"))
        self.view = PlateView()
        left.addWidget(self.view, 1)
        self.model_info = QLabel("Add models (STL or OBJ), or open a ready print file.")
        left.addWidget(self.model_info)
        plate_row = QHBoxLayout()
        self.objects_list = QListWidget()
        self.objects_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.objects_list.setMaximumHeight(120)
        self.objects_list.currentRowChanged.connect(self._selection_changed)
        self.view.object_picked.connect(self.objects_list.setCurrentRow)
        self.view.object_moved.connect(self._object_dragged)
        for key in (QKeySequence.StandardKey.Delete, QKeySequence(Qt.Key.Key_Backspace)):
            QShortcut(key, self.objects_list, self._remove_selected,
                      context=Qt.ShortcutContext.WidgetShortcut)
        plate_row.addWidget(self.objects_list, 1)
        buttons = QVBoxLayout()
        add_btn = QPushButton("Add Models…")
        add_btn.clicked.connect(self._add_models)
        buttons.addWidget(add_btn)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setToolTip("Remove the selected objects (Delete key)")
        self.remove_btn.clicked.connect(self._remove_selected)
        buttons.addWidget(self.remove_btn)
        self.clear_btn = QPushButton("Clear Plate")
        self.clear_btn.clicked.connect(self._clear_plate)
        buttons.addWidget(self.clear_btn)
        open_file = QPushButton("Open Print File…")
        open_file.setToolTip("Open a ready .makerbot or .gcode file")
        open_file.clicked.connect(self._open_print_file)
        buttons.addWidget(open_file)
        buttons.addStretch()
        plate_row.addLayout(buttons)
        left.addLayout(plate_row)
        left.addWidget(self._object_panel())
        self.plate_problems = QLabel("")
        self.plate_problems.setWordWrap(True)
        self.plate_problems.setStyleSheet("color: #c62828;")
        left.addWidget(self.plate_problems)
        lay.addLayout(left, 3)

        right = QVBoxLayout()
        box = QGroupBox("Print settings")
        form = QFormLayout(box)
        self.machine = QComboBox()
        for m in machines.MACHINES.values():
            self.machine.addItem(m.name, m.id)
        self.machine.currentIndexChanged.connect(self._machine_changed)
        form.addRow("Printer:", self.machine)
        self.extruder = QComboBox()
        self.extruder.currentIndexChanged.connect(self._fill_materials)
        form.addRow("Extruder:", self.extruder)
        self.extruder_note = QLabel("")
        self.extruder_note.setWordWrap(True)
        form.addRow("", self.extruder_note)
        self.material = QComboBox()
        self.material.currentIndexChanged.connect(self._material_changed)
        form.addRow("Material:", self.material)
        self.temp = QSpinBox()
        self.temp.setSuffix(" °C")
        form.addRow("Nozzle:", self.temp)
        self.bed_temp = QSpinBox()
        self.bed_temp.setSuffix(" °C")
        self.bed_temp_label = QLabel("Bed:")
        form.addRow(self.bed_temp_label, self.bed_temp)
        self.quality = QComboBox()
        form.addRow("Quality:", self.quality)
        self.infill = QSlider(Qt.Orientation.Horizontal)
        self.infill.setRange(0, 100)
        self.infill.setValue(15)
        self.infill_lbl = QLabel("15%")
        self.infill.valueChanged.connect(lambda v: self.infill_lbl.setText(f"{v}%"))
        irow = QHBoxLayout()
        irow.addWidget(self.infill, 1)
        irow.addWidget(self.infill_lbl)
        form.addRow("Infill:", irow)
        self.walls = QSpinBox()
        self.walls.setRange(1, 10)
        self.walls.setValue(2)
        form.addRow("Walls:", self.walls)
        self.supports = QCheckBox("Generate supports")
        form.addRow("Supports:", self.supports)
        self.support_style = QComboBox()
        for s in SUPPORT_STYLES:
            self.support_style.addItem(s.replace("_", " ").title(), s)
        self.support_style.setCurrentIndex(self.support_style.findData("organic"))
        self.support_style.setEnabled(False)
        self.supports.toggled.connect(self.support_style.setEnabled)
        form.addRow("Style:", self.support_style)
        self.adhesion = QComboBox()
        for a in ("none", "brim", "raft"):
            self.adhesion.addItem(a.title(), a)
        form.addRow("Adhesion:", self.adhesion)
        right.addWidget(box)

        self.slice_btn = QPushButton("Slice")
        self.slice_btn.setEnabled(False)
        self.slice_btn.clicked.connect(self._slice)
        right.addWidget(self.slice_btn)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        right.addWidget(self.result)
        self.thumb = QLabel()
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self.thumb)
        brow = QHBoxLayout()
        self.save_btn = QPushButton("Save File…")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        brow.addWidget(self.save_btn)
        self.sd_btn = QPushButton("Save to SD Card…")
        self.sd_btn.setToolTip("Copy the print file to an SD card or USB stick and eject it")
        self.sd_btn.setEnabled(False)
        self.sd_btn.clicked.connect(self._save_to_drive)
        brow.addWidget(self.sd_btn)
        self.print_btn = QPushButton("Print")
        self.print_btn.setEnabled(False)
        self.print_btn.clicked.connect(self._print)
        brow.addWidget(self.print_btn)
        right.addLayout(brow)
        self.upload = QProgressBar()
        self.upload.setVisible(False)
        right.addWidget(self.upload)
        right.addStretch()
        lay.addLayout(right, 2)

        self._machine_changed()
        ctrl.status_changed.connect(self._status)
        self._auto_extruder_done = False

    # --- printer / settings

    @property
    def machine_def(self):
        return machines.get(self.machine.currentData())

    def select_machine(self, machine_id):
        i = self.machine.findData(machine_id)
        if i >= 0 and i != self.machine.currentIndex():
            self.machine.setCurrentIndex(i)

    def _on_connection(self, state, message):
        super()._on_connection(state, message)
        if state == "connected" and self.ctrl.machine_id:
            self.select_machine(self.ctrl.machine_id)
            self._auto_extruder_done = False
        self._update_print_button()

    def _machine_changed(self):
        m = self.machine_def
        if len(self.plate):
            try:
                self.plate.set_bed(m.bed)
            except mesh_mod.MeshError as e:
                QMessageBox.warning(self, "Doesn't fit", f"The current plate doesn't fit on "
                                    f"the {m.name}:\n\n{e}")
                self.machine.blockSignals(True)
                self.machine.setCurrentIndex(self.machine.findData(self._machine_id))
                self.machine.blockSignals(False)
                return
        self._machine_id = m.id
        self.plate.bed = m.bed
        self.extruder.blockSignals(True)
        self.extruder.clear()
        for key, e in m.extruders.items():
            self.extruder.addItem(e["name"], key)
        self.extruder.setCurrentIndex(max(0, self.extruder.findData(m.default_extruder)))
        self.extruder.blockSignals(False)
        self.extruder.setEnabled(len(m.extruders) > 1)
        self.quality.clear()
        for q in m.qualities():
            if m.family == "birdwing":
                label = f"{QUALITY_LABELS[q]} ({QUALITY[q]} mm layers)"
            else:
                layer = (m.process_profile(q) or "").split("mm")[0]
                label = f"{QUALITY_LABELS[q]} ({layer} mm layers)"
            self.quality.addItem(label, q)
        self.quality.setCurrentIndex(max(0, self.quality.findData("standard")))
        self.temp.setRange(170, m.max_nozzle_temp)
        self.bed_temp.setRange(0, m.max_bed_temp or 0)
        self.bed_temp.setVisible(m.heated_bed)
        self.bed_temp_label.setVisible(m.heated_bed)
        self.view.set_bed(m.bed)
        self._fill_materials()
        self._refresh_plate()

    def _fill_materials(self):
        m = self.machine_def
        key = self.extruder.currentData() or m.default_extruder
        self.material.blockSignals(True)
        self.material.clear()
        for mkey, spec in m.extruders[key]["materials"].items():
            self.material.addItem(spec[0], mkey)
        self.material.blockSignals(False)
        self._material_changed()
        self._check_extruder()

    def _material_changed(self):
        m = self.machine_def
        key, mat = self.extruder.currentData() or m.default_extruder, self.material.currentData()
        if mat:
            spec = m.extruders[key]["materials"][mat]
            if spec[1]:
                self.temp.setValue(spec[1])
            if len(spec) > 2 and spec[2] is not None:
                self.bed_temp.setValue(spec[2])

    def _status(self, status):
        m = self.machine_def
        if (not self._auto_extruder_done and m.family == "birdwing" and status.extruder
                and status.extruder.tool_type):
            i = self.extruder.findData(status.extruder.tool_type)
            if i >= 0:
                self.extruder.setCurrentIndex(i)
                self._auto_extruder_done = True
        self._check_extruder()
        self._update_print_button()

    def _check_extruder(self):
        s = self.ctrl.status
        if self.machine_def.family != "birdwing" or not s or s.bot_type != "replicator_b":
            self.extruder_note.setText("")
            return
        attached = s.extruder.tool_type if s.extruder else None
        chosen = self.extruder.currentData()
        if attached and attached != chosen:
            self.extruder_note.setText(f"⚠️ The printer has "
                                       f"{self.machine_def.extruders[attached]['name']} "
                                       "attached.")
        elif attached:
            self.extruder_note.setText("✓ matches the attached extruder")
        else:
            self.extruder_note.setText("")

    def settings(self):
        m = self.machine_def
        return PrintSettings(
            machine=m.id, extruder=self.extruder.currentData() or m.default_extruder,
            material=self.material.currentData(), quality=self.quality.currentData(),
            infill_percent=self.infill.value(), walls=self.walls.value(),
            supports=self.supports.isChecked(),
            support_style=self.support_style.currentData(),
            adhesion=self.adhesion.currentData(), temperature=self.temp.value(),
            bed_temperature=self.bed_temp.value() if m.heated_bed else None).validate()

    def _update_print_button(self):
        ok = (bool(self.result_path) and self.ctrl.connected and
              self.result_machine is not None and self.result_machine == self.ctrl.machine_id)
        self.print_btn.setEnabled(ok)
        if self.result_path and self.ctrl.connected and not ok:
            self.print_btn.setToolTip("This file was made for a different printer than the "
                                      "one connected")
        else:
            self.print_btn.setToolTip("")

    # --- models

    def _add_models(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add models to the plate", "",
                                                "3D models (*.stl *.obj)")
        m = self.machine_def
        added, problems = 0, []
        for path in paths:
            try:
                mesh = mesh_mod.load(path)
                mesh.check_fits(m.bed, m.name)
                self.plate.add(os.path.basename(path), mesh)
            except (mesh_mod.MeshError, OSError, ValueError) as e:
                problems.append(f"{os.path.basename(path)}: {e}")
                continue
            added += 1
        if problems:
            QMessageBox.warning(self, "Some models weren't added", "\n\n".join(problems))
        if added:
            self._refresh_plate(select=len(self.plate) - 1)

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.objects_list.selectedIndexes()})
        if not rows:
            return
        self.plate.remove(rows)
        self._refresh_plate(select=min(rows[0], len(self.plate) - 1))

    def _clear_plate(self):
        self.plate.clear()
        self._refresh_plate()

    def _arrange(self):
        """Lay every object out again (also clears manual positions)."""
        self.plate.arrange()
        self._refresh_plate(select=self.objects_list.currentRow())

    def _refresh_plate(self, select=None):
        """Refresh the object list, 3D view, selected-object panel and summary."""
        placed = self.plate.placed()
        row = self.objects_list.currentRow() if select is None else select
        self.objects_list.blockSignals(True)
        self.objects_list.clear()
        for o in self.plate.objects:
            sx, sy, sz = o.size
            extra = f" · {o.scale * 100:.0f}%" if o.scale != 1 else ""
            self.objects_list.addItem(f"{o.name}  —  {sx:.0f} × {sy:.0f} × {sz:.0f} mm{extra}")
        if 0 <= row < len(self.plate):
            self.objects_list.setCurrentRow(row)
        self.objects_list.blockSignals(False)
        self.view.set_meshes(placed, self.objects_list.currentRow())
        n = len(self.plate)
        self.model_info.setText(
            "Add models (STL or OBJ), or open a ready print file." if not n else
            f"{n} object{'s' if n != 1 else ''} on the plate · "
            f"{sum(len(o.mesh.triangles) for o in self.plate.objects):,} triangles"
            " · drag objects in the view to move them")
        problems = self.plate.problems()
        self.plate_problems.setText("\n".join(f"⚠️ {p}" for p in problems))
        self.remove_btn.setEnabled(n > 0)
        self.clear_btn.setEnabled(n > 0)
        self.slice_btn.setEnabled(n > 0 and not problems)
        self._show_selected()
        self._set_result(None)

    # --- selected object

    def _object_panel(self):
        from PySide6.QtWidgets import QDoubleSpinBox
        box = QGroupBox("Selected object")
        grid = QGridLayout(box)
        self.obj_x, self.obj_y, self.obj_rz, self.obj_scale = (QDoubleSpinBox() for _ in range(4))
        for w, suffix, lo, hi, dec in ((self.obj_x, " mm", -500, 500, 1),
                                       (self.obj_y, " mm", -500, 500, 1),
                                       (self.obj_rz, "°", -180, 180, 0),
                                       (self.obj_scale, " %", 1, 2000, 0)):
            w.setSuffix(suffix)
            w.setRange(lo, hi)
            w.setDecimals(dec)
            w.setKeyboardTracking(False)          # apply on Enter / arrows, not each digit
        self.obj_rz.setWrapping(True)
        self.obj_rz.setSingleStep(15)
        self.obj_scale.setSingleStep(10)
        for col, (label, w) in enumerate((("X", self.obj_x), ("Y", self.obj_y),
                                          ("Rotate", self.obj_rz), ("Scale", self.obj_scale))):
            grid.addWidget(QLabel(label), 0, col * 2)
            grid.addWidget(w, 0, col * 2 + 1)
        self.obj_x.valueChanged.connect(self._position_edited)
        self.obj_y.valueChanged.connect(self._position_edited)
        self.obj_rz.valueChanged.connect(lambda v: self._transform(rz=v))
        self.obj_scale.valueChanged.connect(lambda v: self._transform(scale=v / 100))
        buttons = QHBoxLayout()
        for text, fn in (("Tip over X", lambda: self._quarter_turn("rx")),
                         ("Tip over Y", lambda: self._quarter_turn("ry")),
                         ("Reset", self._reset_object), ("Duplicate", self._duplicate),
                         ("Arrange All", self._arrange)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            buttons.addWidget(b)
        grid.addLayout(buttons, 1, 0, 1, 8)
        self.object_box = box
        return box

    def _current(self):
        i = self.objects_list.currentRow()
        return (i, self.plate.objects[i]) if 0 <= i < len(self.plate) else (None, None)

    def _show_selected(self):
        i, o = self._current()
        self.object_box.setEnabled(o is not None)
        widgets = (self.obj_x, self.obj_y, self.obj_rz, self.obj_scale)
        for w in widgets:
            w.blockSignals(True)
        if o is not None:
            self.obj_x.setValue(o.x or 0)
            self.obj_y.setValue(o.y or 0)
            self.obj_rz.setValue(o.rz)
            self.obj_scale.setValue(o.scale * 100)
        for w in widgets:
            w.blockSignals(False)

    def _selection_changed(self, row):
        self.view.set_selected(row)
        self._show_selected()

    def _object_dragged(self, index, x, y):
        self.plate.move(index, x, y)
        self._refresh_plate(select=index)

    def _position_edited(self):
        i, o = self._current()
        if o is not None:
            self.plate.move(i, self.obj_x.value(), self.obj_y.value())
            self._refresh_plate(select=i)

    def _transform(self, **kw):
        i, o = self._current()
        if o is None:
            return
        try:
            self.plate.transform(i, **kw)
        except mesh_mod.MeshError as e:
            QMessageBox.warning(self, "Doesn't fit", str(e))
        self._refresh_plate(select=i)

    def _quarter_turn(self, axis):
        i, o = self._current()
        if o is not None:
            self._transform(**{axis: getattr(o, axis) + 90})

    def _reset_object(self):
        i, o = self._current()
        if o is not None:
            self._transform(scale=1.0, rx=0, ry=0, rz=0)

    def _duplicate(self):
        i, o = self._current()
        if o is None:
            return
        try:
            self.plate.duplicate(i)
        except mesh_mod.MeshError as e:
            QMessageBox.warning(self, "No room", str(e))
            return
        self._refresh_plate(select=i + 1)

    # Kept for tools/tests that inspect the plate directly.
    @property
    def objects(self):
        return self.plate.objects

    @property
    def placed(self):
        return self.plate.placed()

    def _open_print_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open print file", "", "Print files (*.makerbot *.gcode *.gco *.g)")
        if not path:
            return
        self.plate.clear()
        self._refresh_plate()
        if path.lower().endswith(".makerbot"):
            try:
                meta = inspect_print_file(path)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "Can't open", str(e))
                return
            self.select_machine("replicator_plus")
            text = (f"{meta.get('material', '?').upper()} {meta.get('extruder_temperature')} °C"
                    f" · about {_fmt_duration(meta.get('duration_s') or 0)} · "
                    f"{meta.get('extrusion_mass_g', 0):.1f} g")
        else:
            from ..slicing.gcode_output import GcodeCheckError, analyse
            m = self.machine_def
            if m.output != "gcode":
                self.select_machine(self.ctrl.machine_id if self.ctrl.machine_id and
                                    machines.get(self.ctrl.machine_id).output == "gcode"
                                    else "ender3_pro")
                m = self.machine_def
            try:
                st = analyse(path, m.bed, m.max_nozzle_temp)
            except GcodeCheckError as e:
                QMessageBox.warning(self, f"Not safe for the {m.name}", str(e))
                return
            text = (f"G-code for the {m.name} · about {_fmt_duration(st['duration_s'])} · "
                    f"{st['filament_g']:.1f} g")
        self.model_info.setText(f"<b>{os.path.basename(path)}</b> (ready to print)")
        self._set_result(path, text, self.machine_def.id)

    def _set_result(self, path, text="", machine_id=None):
        self.result_path = path
        self.result_machine = machine_id
        self.result.setText(text)
        self.save_btn.setEnabled(bool(path))
        self.sd_btn.setEnabled(bool(path))
        self._update_print_button()
        self.thumb.clear()
        if path and path.endswith(".makerbot"):
            try:
                from ..slicing.package import read_thumbnail
                img = QImage.fromData(read_thumbnail(path))
                self.thumb.setPixmap(QPixmap.fromImage(img))
            except Exception:  # noqa: BLE001 - files from other tools may lack thumbnails
                pass

    # --- slicing / saving / printing

    def _slice(self):
        from ..slicing import slice_plate
        try:
            settings = self.settings()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Settings", str(e))
            return
        m = settings.machine_def
        out_dir = os.path.join(os.path.expanduser("~/Library/Caches/OpenBot"))
        os.makedirs(out_dir, exist_ok=True)
        # Short, plain names suit printer SD menus; keep the object count visible.
        stem = drives.safe_filename(self.plate.objects[0].name, "", max_stem=18)
        if len(self.plate) > 1:
            stem += f"_{len(self.plate)}pcs"
        out = os.path.join(out_dir, stem + m.file_extension)
        placed = self.plate.placed()
        self.slice_btn.setEnabled(False)
        self.slice_btn.setText("Slicing…")
        t0 = time.monotonic()

        async def go():
            try:
                r = await asyncio.to_thread(slice_plate, placed, out, settings)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                later(lambda: QMessageBox.warning(self, "Slicing failed", msg))
                return
            finally:
                self.slice_btn.setEnabled(True)
                self.slice_btn.setText("Slice")
            text = (f"<b>Ready for the {m.name}:</b> about {_fmt_duration(r.duration_s)} · "
                    f"{r.filament_g:.1f} g {settings.material_name} · {r.layers} layers "
                    f"(sliced in {time.monotonic() - t0:.0f} s)")
            if r.warnings:
                text += "<br>" + "<br>".join(f"⚠️ {w}" for w in r.warnings[:5])
            self._set_result(out, text, m.id)
        self.ctrl.run(go(), what="slicing")

    def _send_to_queue(self):
        path, b = self.result_path, self.ctrl.backend
        if QMessageBox.question(
                self, "Send to queue",
                f"Send {os.path.basename(path)} to {b.server_name}'s print queue?\n\n"
                "It starts after someone confirms the build plate is clear (Queue "
                "page).") != QMessageBox.StandardButton.Yes:
            return
        self.upload.setVisible(True)
        self.upload.setValue(0)

        async def go():
            try:
                await b.start_print(path, progress=lambda s, t:
                                    self.upload.setValue(int(s * 100 / t)))
            finally:
                self.upload.setVisible(False)
            self.window().show_page("Queue")
        self.ctrl.run(go(), what="sending to the queue")

    def _save(self):
        ext = os.path.splitext(self.result_path)[1]
        dest, _ = QFileDialog.getSaveFileName(self, "Save print file",
                                              os.path.basename(self.result_path),
                                              f"Print file (*{ext})")
        if dest:
            import shutil
            shutil.copyfile(self.result_path, dest)

    def _save_to_drive(self):
        m = machines.get(self.result_machine) if self.result_machine else self.machine_def
        dlg = dialogs.SaveToDriveDialog(self.result_path, m, self)
        if dlg.exec() != dlg.DialogCode.Accepted or not dlg.drive:
            return
        drive, name, eject = dlg.drive, dlg.filename(), dlg.eject.isChecked()

        async def go():
            dest = await asyncio.to_thread(drives.save_to_drive, self.result_path, drive, name)
            msg = f"Saved {os.path.basename(dest)} to {drive.name}."
            if eject:
                await asyncio.to_thread(drives.eject, drive)
                msg += " It's safe to remove the card now."
            later(lambda: QMessageBox.information(self, "Saved", msg))
        self.ctrl.run(go(), what="saving to the card")

    def _print(self):
        if self.ctrl.remote:
            self._send_to_queue()
            return
        path = self.result_path
        m = machines.get(self.result_machine)
        if m.output == "makerbot":
            problems = check_print_file(path, self.ctrl.status)
            meta = inspect_print_file(path)
            summary = (f"<b>{os.path.basename(path)}</b><br>{meta.get('material', '').upper()} "
                       f"at {meta.get('extruder_temperature')} °C, about "
                       f"{_fmt_duration(meta.get('duration_s') or 0)}. The printer will heat "
                       "and move.")
        else:
            problems = ["This streams the print over USB: keep this Mac awake and the cable "
                        "connected until it finishes. For long prints, Save to SD Card is "
                        "more reliable."]
            summary = (f"<b>{os.path.basename(path)}</b> on the {m.name}. The printer will "
                       "heat and move.")
        dlg = dialogs.ConfirmPrintDialog(summary, problems, self)
        task = None
        if self.ctrl.supports(caps.CAMERA):
            async def photo():
                try:
                    frame = await self.ctrl.snapshot()
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
        self.upload.setVisible(m.output == "makerbot")
        self.upload.setValue(0)

        async def go():
            try:
                await self.ctrl.backend.start_print(
                    path, progress=lambda s, t: self.upload.setValue(int(s * 100 / t)))
            finally:
                self.upload.setVisible(False)
            await self.ctrl.refresh_status()
            self.window().show_page("Monitor")
        self.ctrl.run(go(), what="starting the print")


# ================================================================ Monitor

class MonitorPage(Page):
    def __init__(self, ctrl, parent=None):
        super().__init__(ctrl, parent)
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(_title("Monitor"))
        self.process = QLabel("<h3>Idle</h3>")
        left.addWidget(self.process)
        self.progress = QProgressBar()
        left.addWidget(self.progress)
        self.times = QLabel("")
        left.addWidget(self.times)
        grid = QGridLayout()
        self.temp = QLabel("–")
        self.bed = QLabel("–")
        self.filament = QLabel("–")
        self.extr = QLabel("–")
        self._rows = {}
        for r, (k, w) in enumerate((("Extruder", self.extr), ("Nozzle", self.temp),
                                     ("Bed", self.bed), ("Filament", self.filament))):
            lbl = QLabel(f"<b>{k}</b>")
            grid.addWidget(lbl, r, 0)
            grid.addWidget(w, r, 1)
            self._rows[k] = (lbl, w)
        left.addLayout(grid)
        row = QHBoxLayout()
        self.pause = QPushButton("Pause")
        self.pause.clicked.connect(self._pause_resume)
        self.cancel = QPushButton("Cancel Print")
        self.cancel.clicked.connect(self._cancel)
        self.ack = QPushButton("Done / Clear")
        self.ack.setToolTip("Dismiss the finished print on the printer")
        self.ack.clicked.connect(lambda: ctrl.run(ctrl.backend.acknowledge(), what="acknowledge"))
        for b in (self.pause, self.cancel, self.ack):
            row.addWidget(b)
        left.addLayout(row)
        self.estop = QPushButton("⛔ Emergency Stop")
        self.estop.setToolTip("M112: halts the printer at once. It must be switched off and "
                              "on again afterwards.")
        self.estop.setStyleSheet("QPushButton { color: #c62828; font-weight: bold; }")
        self.estop.clicked.connect(self._emergency_stop)
        left.addWidget(self.estop)

        self.sd_box = QGroupBox("Printer's SD card")
        sdl = QVBoxLayout(self.sd_box)
        self.sd_list = QListWidget()
        self.sd_list.setMaximumHeight(160)
        sdl.addWidget(self.sd_list)
        sdrow = QHBoxLayout()
        sd_refresh = QPushButton("Refresh")
        sd_refresh.clicked.connect(self._sd_refresh)
        sdrow.addWidget(sd_refresh)
        self.sd_print = QPushButton("Print Selected")
        self.sd_print.clicked.connect(self._sd_print)
        sdrow.addWidget(self.sd_print)
        sdl.addLayout(sdrow)
        left.addWidget(self.sd_box)
        left.addStretch()
        lay.addLayout(left, 2)

        self.camera_panel = QWidget()
        right = QVBoxLayout(self.camera_panel)
        right.setContentsMargins(0, 0, 0, 0)
        self.camera = QLabel("Camera off")
        self.camera.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.camera.setMinimumSize(480, 360)
        self.camera.setStyleSheet("background:#111; color:#aaa; border-radius:6px;")
        right.addWidget(self.camera, 1)
        self.cam_pct = QLabel(self.camera)      # print progress overlaid on the feed
        self.cam_pct.setStyleSheet("background:rgba(0,0,0,160); color:white; font-weight:600;"
                                   "font-size:15px; padding:3px 8px; border-radius:4px;")
        self.cam_pct.move(8, 8)
        self.cam_pct.hide()
        crow = QHBoxLayout()
        self.cam_btn = QPushButton("Start Camera")
        self.cam_btn.clicked.connect(self._toggle_camera)
        crow.addWidget(self.cam_btn)
        snap = QPushButton("Save Snapshot…")
        snap.clicked.connect(self._snapshot)
        crow.addWidget(snap)
        right.addLayout(crow)
        lay.addWidget(self.camera_panel, 3)

        self._sd_files = []
        self._last_image = None
        ctrl.status_changed.connect(self._status)
        ctrl.camera_frame.connect(self._frame)

    def _on_connection(self, state, message):
        super()._on_connection(state, message)
        if state != "connected":
            self.cam_btn.setText("Start Camera")
        connected = state == "connected"
        self.camera_panel.setVisible(not connected or self.ctrl.supports(caps.CAMERA))
        self.sd_box.setVisible(connected and self.ctrl.supports(caps.SD_CARD))
        self.estop.setVisible(connected and self.ctrl.supports(caps.EMERGENCY_STOP))
        heated = connected and self.ctrl.supports(caps.HEATED_BED)
        for w in self._rows["Bed"]:
            w.setVisible(heated)
        for w in self._rows["Filament"]:
            w.setVisible(not connected or not self.ctrl.supports(caps.SD_CARD))
        if connected and self.ctrl.supports(caps.SD_CARD):
            self._sd_refresh()

    def _status(self, s):
        e = s.extruder
        if e:
            self.extr.setText(e.display_name if s.bot_type == "replicator_b" else
                              machines.get(s.bot_type).name if s.bot_type in
                              machines.MACHINES else "–")
            self.temp.setText(f"{e.current_temperature:.0f} / {e.target_temperature:.0f} °C")
            self.filament.setText("–" if e.filament_present is None else
                                  "loaded" if e.filament_present else "⚠️ not detected")
        if s.bed:
            self.bed.setText(f"{s.bed[0]:.0f} / {s.bed[1]:.0f} °C")
        p = s.process
        if p is None:
            self.process.setText("<h3>Idle</h3>")
            self.progress.setValue(0)
            self.times.setText("")
            self.cam_pct.hide()
        else:
            step = (p.step or "").replace("_", " ")
            what = ("Printing" if p.is_print else "Printing from SD" if p.name == "SDPrint"
                    else p.name)
            pct = f" · {p.progress:.0f}%" if p.progress is not None else ""
            self.process.setText(f"<h3>{what}: {step}{pct}</h3>")
            self.cam_pct.setText(f"{p.progress:.0f}%" if p.progress is not None else "")
            self.cam_pct.adjustSize()
            self.cam_pct.setVisible(p.progress is not None and not p.complete)
            self.progress.setValue(int(p.progress or 0))
            if p.elapsed_time and p.progress:
                remaining = p.elapsed_time * (100 - p.progress) / max(p.progress, 1)
                self.times.setText(f"Elapsed {_fmt_duration(p.elapsed_time)} · about "
                                   f"{_fmt_duration(remaining)} left")
        busy = p is not None and not p.complete
        self.pause.setEnabled(busy and (p.is_print or p.name == "SDPrint"))
        self.sd_print.setEnabled(not busy)
        self.pause.setText("Resume" if p and p.step == "suspended" else "Pause")
        self.cancel.setEnabled(busy and p.cancellable)
        self.ack.setEnabled(p is not None and (p.complete or p.step in ("completed",
                                                                         "failed")))

    def _sd_refresh(self):
        async def go():
            self._sd_files = await self.ctrl.backend.sd_list()
            self.sd_list.clear()
            for f in self._sd_files:
                self.sd_list.addItem(f"{f['display']}  ({f['size'] / 1e6:.1f} MB)")
            if not self._sd_files:
                self.sd_list.addItem("(no files, or no card in the printer)")
        self.ctrl.run(go(), what="reading the printer's SD card")

    def _sd_print(self):
        i = self.sd_list.currentRow()
        if not 0 <= i < len(self._sd_files):
            return
        f = self._sd_files[i]
        if QMessageBox.question(
                self, "Print from SD card",
                f"Start {f['display']} from the printer's SD card? The printer will heat "
                "and move; make sure the build plate is clear.") != \
                QMessageBox.StandardButton.Yes:
            return
        self.ctrl.run(self.ctrl.backend.sd_print(f["name"]), what="SD print")

    def _emergency_stop(self):
        if QMessageBox.warning(
                self, "Emergency stop",
                "Halt the printer immediately? Heaters and motors stop, and the printer must "
                "be switched off and on again before it can be used.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == \
                QMessageBox.StandardButton.Yes:
            self.ctrl.run(self.ctrl.backend.emergency_stop(), what="emergency stop")

    def _pause_resume(self):
        b = self.ctrl.backend
        if self.pause.text() == "Resume":
            self.ctrl.run(b.resume(), what="resume")
        else:
            self.ctrl.run(b.pause(), what="pause")

    def _cancel(self):
        if QMessageBox.question(self, "Cancel print", "Cancel the current print?") == \
                QMessageBox.StandardButton.Yes:
            self.ctrl.run(self.ctrl.backend.cancel(), what="cancel")

    def _toggle_camera(self):
        if self.ctrl.camera_running():
            self.ctrl.run(self.ctrl.stop_camera(), what="camera")
            self.cam_btn.setText("Start Camera")
        else:
            self.ctrl.start_camera()
            self.cam_btn.setText("Stop Camera")

    def _frame(self, img):
        self._last_image = img
        self.camera.setPixmap(QPixmap.fromImage(img).scaled(
            self.camera.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def _snapshot(self):
        async def go():
            frame = await self.ctrl.snapshot()
            later(lambda: self._save_snapshot(frame))
        self.ctrl.run(go(), what="snapshot")

    def _save_snapshot(self, frame):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save snapshot", time.strftime("plate-%Y%m%d-%H%M%S.jpg"), "JPEG (*.jpg)")
        if path:
            with open(path, "wb") as f:
                f.write(frame.data)


# ================================================================ Network

class NetworkPage(Page):
    capability = caps.NETWORK
    min_role = caps.Role.ADMIN

    def __init__(self, ctrl, parent=None):
        super().__init__(ctrl, parent)
        self.nets = []
        lay = QVBoxLayout(self)
        lay.addWidget(_title("Network"))
        self.current = QLabel("–")
        self.current.setWordWrap(True)
        lay.addWidget(self.current)

        wbox = QGroupBox("Wi-Fi")
        wl = QVBoxLayout(wbox)
        self.radio = QCheckBox("Wi-Fi radio on")
        self.radio.clicked.connect(self._radio)
        wl.addWidget(self.radio)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Network", "Signal", "Security"])
        self.list.setRootIsDecorated(False)
        self.list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.list.itemDoubleClicked.connect(lambda *_: self._join())
        wl.addWidget(self.list)
        row = QHBoxLayout()
        for text, fn in (("Scan", self.refresh), ("Join…", self._join), ("Forget", self._forget),
                         ("Disconnect", self._disconnect)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        wl.addLayout(row)
        lay.addWidget(wbox)

        ibox = QGroupBox("IP address (Ethernet)")
        il = QFormLayout(ibox)
        self.dhcp = QRadioButton("Automatic (DHCP)")
        self.static = QRadioButton("Manual")
        grp = QButtonGroup(self)
        grp.addButton(self.dhcp)
        grp.addButton(self.static)
        il.addRow(self.dhcp)
        il.addRow(self.static)
        from PySide6.QtCore import QRegularExpression
        from PySide6.QtGui import QRegularExpressionValidator
        val = QRegularExpressionValidator(QRegularExpression(IPV4))
        self.ip, self.mask, self.gw, self.dns = (QLineEdit() for _ in range(4))
        for w, label in ((self.ip, "Address"), (self.mask, "Netmask"), (self.gw, "Gateway")):
            w.setValidator(val)
            il.addRow(label + ":", w)
        il.addRow("DNS:", self.dns)
        self.static.toggled.connect(self._static_toggled)
        apply = QPushButton("Apply")
        apply.clicked.connect(self._apply_ip)
        il.addRow(apply)
        lay.addWidget(ibox)

        nbox = QGroupBox("Printer name")
        nl = QHBoxLayout(nbox)
        self.name = QLineEdit()
        nl.addWidget(self.name, 1)
        rb = QPushButton("Rename")
        rb.clicked.connect(lambda: ctrl.run(ctrl.backend.rename(self.name.text().strip()),
                                            what="rename"))
        nl.addWidget(rb)
        lay.addWidget(nbox)
        lay.addStretch()

    def _on_connection(self, state, message):
        super()._on_connection(state, message)
        if state == "connected" and self.applies() and self.visible_in_nav(state):
            self.name.setText(self.ctrl.printer.info.name)
            self.refresh()

    def _static_toggled(self, on):
        for w in (self.ip, self.mask, self.gw, self.dns):
            w.setEnabled(on)

    def refresh(self):
        b = self.ctrl.backend

        async def go():
            st = await b.network_state()
            self.nets = await b.wifi_scan(force_rescan=True)
            ip = await b.get_static_ipv4("ethernet")
            self.radio.setChecked(st.wifi_radio == "enabled")
            joined = [n for n in self.nets if n.saved]
            wifi_line = (f"Wi-Fi: joined {', '.join(n.name for n in joined if n.name)}"
                         if joined else "Wi-Fi: not joined")
            self.current.setText(
                f"<b>Connected via {st.state}</b> · {st.ip}/{st.netmask} · gateway "
                f"{st.gateway} · {'static' if st.static else 'DHCP'}<br>{wifi_line}")
            self.list.clear()
            for n in self.nets:
                bars = "▂▄▆█"[:max(1, min(4, n.strength // 25 + 1))]
                sec = "Open" if not n.secured else ("🔒 Saved" if n.saved else "🔒")
                QTreeWidgetItem(self.list, [n.name or "(hidden network)", f"{bars} {n.strength}",
                                            sec])
            (self.static if ip.use_static else self.dhcp).setChecked(True)
            self._static_toggled(ip.use_static)
            self.ip.setText(ip.ip or st.ip or "")
            self.mask.setText(ip.netmask or st.netmask or "")
            self.gw.setText(ip.gateway or st.gateway or "")
            self.dns.setText(", ".join(ip.dns or st.dns or []))
        self.ctrl.run(go(), what="network scan")

    def _selected(self):
        i = self.list.indexOfTopLevelItem(self.list.currentItem()) if self.list.currentItem() \
            else -1
        return self.nets[i] if 0 <= i < len(self.nets) else None

    def _join(self):
        net = self._selected()
        if not net:
            return
        dlg = dialogs.WifiDialog(net, self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        pw = dlg.password.text() or None
        name = dlg.ssid.text().strip() if net.hidden else None

        async def go():
            st = await self.ctrl.backend.wifi_connect(net.path, pw, name)
            text = f"Joined {name or net.name}: {st.ip}"
            later(lambda: QMessageBox.information(self, "Wi-Fi", text))
            self.refresh()
        self.ctrl.run(go(), what="joining Wi-Fi")

    def _forget(self):
        net = self._selected()
        if net and QMessageBox.question(self, "Forget network",
                                        f"Forget {net.name or 'this network'}?") == \
                QMessageBox.StandardButton.Yes:
            async def go():
                await self.ctrl.backend.wifi_forget(net.path)
                self.refresh()
            self.ctrl.run(go(), what="forget network")

    def _disconnect(self):
        async def go():
            await self.ctrl.backend.wifi_disconnect()
            self.refresh()
        self.ctrl.run(go(), what="Wi-Fi disconnect")

    def _radio(self, on):
        b = self.ctrl.backend
        self.ctrl.run(b.wifi_enable() if on else b.wifi_disable(), what="Wi-Fi radio")

    def _apply_ip(self):
        b = self.ctrl.backend
        if QMessageBox.question(
                self, "Change IP settings",
                "Changing the address can move the printer away from the address OpenBot "
                "uses now. Continue?") != QMessageBox.StandardButton.Yes:
            return
        if self.dhcp.isChecked():
            self.ctrl.run(b.use_dhcp("ethernet"), what="switch to DHCP")
            return
        if not all(w.hasAcceptableInput() for w in (self.ip, self.mask, self.gw)):
            QMessageBox.warning(self, "IP settings", "Enter a valid address, netmask and "
                                                     "gateway.")
            return
        dns = [d.strip() for d in self.dns.text().split(",") if d.strip()] or None
        self.ctrl.run(b.set_static_ipv4("ethernet", ip=self.ip.text(), netmask=self.mask.text(),
                                        gateway=self.gw.text(), dns=dns),
                      what="set static IP")


# ================================================================ Maintenance

class MaintenancePage(Page):
    def __init__(self, ctrl, parent=None):
        super().__init__(ctrl, parent)
        lay = QVBoxLayout(self)
        lay.addWidget(_title("Maintenance"))
        self.fbox = fbox = QGroupBox("Filament")
        fl = QHBoxLayout(fbox)
        for text, fn in (("Load", lambda: ctrl.backend.load_filament(0)),
                         ("Unload", lambda: ctrl.backend.unload_filament(0)),
                         ("Stop", lambda: ctrl.backend.stop_filament())):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn, t=text: ctrl.run(f(), what=f"filament {t}"))
            fl.addWidget(b)
        lay.addWidget(fbox)
        self.fnote = QLabel("Loading and unloading heat the extruder; follow the prompts on "
                            "the printer's screen.")
        self.fnote.setWordWrap(True)
        lay.addWidget(self.fnote)
        hbox = QGroupBox("Heating")
        hl = QHBoxLayout(hbox)
        self.preheat_btn = QPushButton("Preheat")
        self.preheat_btn.clicked.connect(lambda: ctrl.run(ctrl.backend.preheat(),
                                                          what="preheat"))
        hl.addWidget(self.preheat_btn)
        # Marlin printers: preheat for a material (nozzle / bed °C from the machine profile).
        self.preset_btns = []
        for mat in ("pla", "petg", "abs"):
            b = QPushButton(f"Preheat {mat.upper()}")
            b.clicked.connect(lambda _=False, m=mat: self._preheat_material(m))
            hl.addWidget(b)
            self.preset_btns.append(b)
        cool = QPushButton("Cool Down")
        cool.clicked.connect(lambda: ctrl.run(ctrl.backend.cool(), what="cool down"))
        hl.addWidget(cool)
        lay.addWidget(hbox)

        self.motion = QGroupBox("Motion")
        ml = QHBoxLayout(self.motion)
        for text, name in (("Home All Axes", "home"), ("Motors Off", "motors_off")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, n=name, t=text:
                              ctrl.run(getattr(ctrl.backend, n)(), what=t))
            ml.addWidget(b)
        lay.addWidget(self.motion)

        self.cbox = cbox = QGroupBox("Printer certificate")
        cl = QVBoxLayout(cbox)
        self.cert = QLabel("–")
        self.cert.setWordWrap(True)
        self.cert.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.cert)
        crow = QHBoxLayout()
        for text, fn in (("Export…", self._export), ("Import…", self._import),
                         ("Add to Keychain", self._keychain), ("Remove Trust", self._remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            crow.addWidget(b)
        cl.addLayout(crow)
        lay.addWidget(cbox)
        lay.addStretch()

    def _on_connection(self, state, message):
        super()._on_connection(state, message)
        connected = state == "connected"
        marlin = connected and self.ctrl.supports(caps.HOME)
        self.fbox.setVisible(not marlin)
        self.fnote.setVisible(not marlin)
        self.preheat_btn.setVisible(not marlin)
        for b in self.preset_btns:
            b.setVisible(marlin)
        self.motion.setVisible(marlin)
        self.cbox.setVisible(not connected or self.ctrl.supports(caps.CERTIFICATE))
        if connected and self.ctrl.supports(caps.CERTIFICATE):
            self._show_cert()

    def _preheat_material(self, material):
        m = machines.get(self.ctrl.machine_id)
        spec = next(iter(m.extruders.values()))["materials"].get(material)
        if not spec:
            return
        self.ctrl.run(self.ctrl.backend.preheat(spec[1], spec[2]),
                      what=f"preheat {material.upper()}")

    def _serial(self):
        return self.ctrl.printer.info.serial

    def _show_cert(self):
        c = self.ctrl.trust.get(self._serial())
        self.cert.setText("Not trusted" if not c else
                          f"{c.subject}<br>SHA-256 {c.fingerprint_display}<br>valid until "
                          f"{c.not_after:%Y-%m-%d}")

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export certificate",
                                              f"{self._serial()}.pem", "PEM (*.pem)")
        if path:
            self.ctrl.trust.export_file(self._serial(), path)

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import certificate", "",
                                              "Certificates (*.pem *.crt *.cer)")
        if path:
            self.ctrl.trust.import_file(self._serial(), path)
            self._show_cert()

    def _keychain(self):
        from ..printer.trust import add_to_keychain
        c = self.ctrl.trust.get(self._serial())
        if not c:
            return
        try:
            add_to_keychain(c, self.ctrl.host)
            self.ctrl.trust.mark_keychain(self._serial(), self.ctrl.host)
            QMessageBox.information(self, "Keychain", "Added to your login keychain, trusted "
                                                      f"for SSL to {self.ctrl.host} only.")
        except PrinterError as e:
            QMessageBox.warning(self, "Keychain", str(e))

    def _remove(self):
        if QMessageBox.question(self, "Remove trust",
                                "Remove trust? OpenBot will ask again on the next "
                                "connection.") == QMessageBox.StandardButton.Yes:
            self.ctrl.trust.remove(self._serial())
            self.ctrl.run(self.ctrl.disconnect(), what="disconnect")


# ================================================================ Console

class ConsolePage(Page):
    """Send G-code to a Marlin printer and watch everything it says."""
    capability = caps.CONSOLE
    min_role = caps.Role.ADMIN

    def __init__(self, ctrl, parent=None):
        from PySide6.QtWidgets import QPlainTextEdit
        super().__init__(ctrl, parent)
        lay = QVBoxLayout(self)
        lay.addWidget(_title("Console"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        f = self.log.font()
        f.setFamily("Menlo")
        self.log.setFont(f)
        lay.addWidget(self.log, 1)
        self.hide_temps = QCheckBox("Hide routine replies (ok, temperature reports)")
        self.hide_temps.setChecked(True)
        lay.addWidget(self.hide_temps)
        row = QHBoxLayout()
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("G-code, e.g. M503 (show settings) or G28 (home)")
        self.cmd.returnPressed.connect(self._send)
        row.addWidget(self.cmd, 1)
        send = QPushButton("Send")
        send.clicked.connect(self._send)
        row.addWidget(send)
        lay.addLayout(row)
        self._history = []
        ctrl.console_line.connect(self._line)

    def _line(self, line):
        if self.hide_temps.isChecked() and (line == "ok" or line.lstrip().startswith("T:")
                                            or line.startswith("ok T:")
                                            or line.startswith("SD printing byte")):
            return
        self.log.appendPlainText(line)

    def _send(self):
        cmd = self.cmd.text().strip()
        if not cmd:
            return
        self._history.append(cmd)
        self.cmd.clear()
        self.log.appendPlainText(f"> {cmd}")

        async def go():
            reply = await self.ctrl.backend.send_gcode(cmd)
            if self.ctrl.remote:                  # no live line stream through a server
                for line in reply or []:
                    self.log.appendPlainText(line)
        self.ctrl.run(go(), what=f"'{cmd}'")

