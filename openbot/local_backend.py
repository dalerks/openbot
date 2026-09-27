"""PrinterBackend for a MakerBot Replicator+ over the network."""

from . import backend as b
from .printer import Printer


class LocalBackend(b.PrinterBackend):
    capabilities = frozenset({b.CAMERA, b.NETWORK, b.FILAMENT, b.CERTIFICATE})
    machine_id = "replicator_plus"

    def __init__(self, printer: Printer, role=b.Role.ADMIN):
        self.printer = printer
        self.role = role

    def camera_stream(self):
        b.check_role(self.role, "camera_stream")
        return self.printer.camera_stream()

    def on_status(self, callback):
        return self.printer.on_status(callback)

    async def close(self):
        await self.printer.close()


b.delegate_operations(LocalBackend, Printer,
                      [n for n in b.REQUIRED_ROLE if n != "camera_stream"])
