"""Modal dialogs: certificate trust, identity change, pairing, Wi-Fi password, print confirm."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QProgressBar, QPushButton, QVBoxLayout)


def _mono(text):
    lbl = QLabel(text)
    f = QFont("Menlo")
    f.setStyleHint(QFont.StyleHint.Monospace)
    lbl.setFont(f)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lbl.setWordWrap(True)
    return lbl


def _fingerprint_block(fp):
    # 32 pairs -> 4 rows of 8 for easy comparison
    pairs = fp.split(":")
    return "\n".join(":".join(pairs[i:i + 8]) for i in range(0, len(pairs), 8))


def _cert_form(cert):
    form = QFormLayout()
    form.addRow("Subject:", _mono(cert.subject))
    form.addRow("Issuer:", _mono(cert.issuer + ("  (self-signed)" if cert.self_signed else "")))
    form.addRow("Valid:", QLabel(f"{cert.not_before:%Y-%m-%d} → {cert.not_after:%Y-%m-%d}"))
    form.addRow("SHA-256:", _mono(_fingerprint_block(cert.fingerprint_display)))
    return form


class TrustDialog(QDialog):
    """Trust this printer?  (PLAN.md §3.3)"""
    TRUST, SESSION = 1, 2

    def __init__(self, cert, info, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Trust this printer?")
        self.choice = None
        lay = QVBoxLayout(self)
        head = QLabel(f"<b>{info.name}</b> (serial {info.serial})<br>"
                      "presents a self-signed certificate. OpenBot will only talk to a printer "
                      "presenting <i>exactly</i> this certificate for this serial number.")
        head.setWordWrap(True)
        lay.addWidget(head)
        lay.addLayout(_cert_form(cert))
        self.keychain = QCheckBox("Also add to macOS Keychain (SSL for this printer's address "
                                  "only; asks for your password)")
        lay.addWidget(self.keychain)
        buttons = QDialogButtonBox()
        trust = buttons.addButton("Trust", QDialogButtonBox.ButtonRole.AcceptRole)
        session = buttons.addButton("This Session Only", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        trust.setDefault(True)
        trust.clicked.connect(lambda: self._done(self.TRUST))
        session.clicked.connect(lambda: self._done(self.SESSION))
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _done(self, choice):
        self.choice = choice
        self.accept()


class CertificateChangedDialog(QDialog):
    def __init__(self, trusted, presented, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Printer identity changed")
        lay = QVBoxLayout(self)
        msg = QLabel("<b>This printer is presenting a different certificate.</b><br>"
                     "This happens after a factory reset or mainboard replacement, or if "
                     "something on your network is impersonating the printer. OpenBot has "
                     "not sent it anything.")
        msg.setWordWrap(True)
        lay.addWidget(msg)
        row = QHBoxLayout()
        for title, cert in (("Trusted", trusted), ("Presented now", presented)):
            col = QVBoxLayout()
            col.addWidget(QLabel(f"<b>{title}</b>"))
            col.addWidget(_mono(_fingerprint_block(cert.fingerprint_display)))
            row.addLayout(col)
        lay.addLayout(row)
        buttons = QDialogButtonBox()
        self.trust_new = buttons.addButton("Trust New Certificate",
                                           QDialogButtonBox.ButtonRole.DestructiveRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        self.trust_new.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


class PairDialog(QDialog):
    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pair with printer")
        lay = QVBoxLayout(self)
        lbl = QLabel(f"<h3>Press the knob on {info.name}</h3>"
                     "The printer's screen asks whether to allow OpenBot. Press the knob to "
                     "accept. You have about 2 minutes.")
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        bar = QProgressBar()
        bar.setRange(0, 0)
        lay.addWidget(bar)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        lay.addWidget(cancel, alignment=Qt.AlignmentFlag.AlignRight)


class WifiDialog(QDialog):
    def __init__(self, network, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Join Wi-Fi network")
        lay = QFormLayout(self)
        self.ssid = QLineEdit(network.name)
        self.ssid.setEnabled(network.hidden)
        self.ssid.setPlaceholderText("Network name (hidden network)")
        lay.addRow("Network:", self.ssid)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        needs = network.secured and not network.saved
        self.password.setEnabled(network.secured)
        self.password.setPlaceholderText("saved on printer" if network.saved else "")
        lay.addRow("Password:", self.password)
        show = QCheckBox("Show password")
        show.toggled.connect(lambda on: self.password.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        lay.addRow("", show)
        warn = QLabel("If this Mac reaches the printer over Wi-Fi, the connection will drop "
                      "briefly while the printer switches networks.")
        warn.setWordWrap(True)
        lay.addRow(warn)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Join")
        buttons.accepted.connect(self._check)
        buttons.rejected.connect(self.reject)
        lay.addRow(buttons)
        self._needs_password = needs
        self._hidden = network.hidden

    def _check(self):
        if self._hidden and not self.ssid.text().strip():
            self.ssid.setFocus()
            return
        if self._needs_password and not self.password.text():
            self.password.setFocus()
            return
        self.accept()


class ConfirmPrintDialog(QDialog):
    """Always shown before a print: the machine will heat and move (PLAN.md §5)."""

    def __init__(self, summary, problems, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Start print?")
        lay = QVBoxLayout(self)
        self.photo = QLabel("Getting a camera view of the build plate…")
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.photo.setMinimumSize(480, 360)
        lay.addWidget(self.photo)
        s = QLabel(summary)
        s.setWordWrap(True)
        lay.addWidget(s)
        for p in problems:
            w = QLabel(f"⚠️ {p}")
            w.setWordWrap(True)
            lay.addWidget(w)
        self.clear = QCheckBox("The build plate is clear and someone is at the printer")
        lay.addWidget(self.clear)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Print")
        self.ok.setEnabled(False)
        self.clear.toggled.connect(self.ok.setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def set_photo(self, image):
        self.photo.setPixmap(QPixmap.fromImage(image).scaled(
            480, 360, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))


class SaveToDriveDialog(QDialog):
    """Pick an SD card / USB stick, name the file, optionally eject afterwards."""

    def __init__(self, path, machine, parent=None):
        from .. import drives
        super().__init__(parent)
        self.setWindowTitle("Save to SD card")
        self._machine = machine
        self.drive = None
        self._drives = []
        lay = QFormLayout(self)
        where = ("Insert the SD card from your Ender" if machine.output == "gcode"
                 else "Insert the USB stick for your Replicator+")
        intro = QLabel(f"{where}, then pick it below.")
        intro.setWordWrap(True)
        lay.addRow(intro)
        from PySide6.QtWidgets import QComboBox
        row = QHBoxLayout()
        self.drive_box = QComboBox()
        self.drive_box.setMinimumWidth(360)
        self.drive_box.currentIndexChanged.connect(self._drive_changed)
        row.addWidget(self.drive_box, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh)
        row.addWidget(refresh)
        lay.addRow("Card:", row)
        self.name = QLineEdit(drives.safe_filename(path, machine.file_extension))
        lay.addRow("File name:", self.name)
        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        lay.addRow(self.warn)
        self.eject = QCheckBox("Eject the card after saving")
        self.eject.setChecked(True)
        lay.addRow(self.eject)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Save)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addRow(buttons)
        self._refresh()

    def _refresh(self):
        from .. import drives
        self._drives = drives.list_removable()
        self.drive_box.clear()
        for d in self._drives:
            self.drive_box.addItem(d.label())
        if not self._drives:
            self.drive_box.addItem("No SD card or USB drive found")
        self.ok.setEnabled(bool(self._drives))
        self._drive_changed()

    def _drive_changed(self):
        i = self.drive_box.currentIndex()
        d = self._drives[i] if 0 <= i < len(self._drives) else None
        self.warn.setText("\n".join(f"⚠️ {w}" for w in d.warnings_for(self._machine.output))
                          if d else "")

    def filename(self):
        from .. import drives
        return drives.safe_filename(self.name.text() or "print", self._machine.file_extension)

    def _accept(self):
        i = self.drive_box.currentIndex()
        if 0 <= i < len(self._drives):
            self.drive = self._drives[i]
            self.accept()
