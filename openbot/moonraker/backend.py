"""PrinterBackend for a Klipper printer behind Moonraker."""

from .. import backend as b
from .printer import MoonrakerPrinter


class MoonrakerBackend(b.PrinterBackend):
    storage_name = "Files on the printer's Raspberry Pi"

    def __init__(self, printer: MoonrakerPrinter, role=b.Role.ADMIN):
        self.printer = printer
        self.role = role
        self.machine_id = printer.machine.id
        caps = {b.SD_CARD, b.CONSOLE, b.HOME, b.EMERGENCY_STOP, b.HEATED_BED,
                b.FIRMWARE_RESTART}
        if printer.webcam:
            caps.add(b.CAMERA)
        self.capabilities = frozenset(caps)
        self.camera_transform = printer.camera_transform

    async def status(self):
        b.check_role(self.role, "status")
        return self.printer.status()

    def camera_stream(self):
        b.check_role(self.role, "camera_stream")
        return self.printer.camera_stream()

    def on_status(self, callback):
        return self.printer.on_status(callback)

    async def close(self):
        await self.printer.close()


b.delegate_operations(MoonrakerBackend, MoonrakerPrinter,
                      [n for n in b.REQUIRED_ROLE if n not in ("status", "camera_stream")])
