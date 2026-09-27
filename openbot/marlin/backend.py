"""PrinterBackend for a Marlin printer (Ender-3 family) on USB."""

from .. import backend as b
from .printer import MarlinPrinter


class MarlinBackend(b.PrinterBackend):
    capabilities = frozenset({b.SD_CARD, b.CONSOLE, b.HOME, b.EMERGENCY_STOP,
                              b.HEATED_BED})

    def __init__(self, printer: MarlinPrinter, role=b.Role.ADMIN):
        self.printer = printer
        self.role = role
        self.machine_id = printer.machine.id

    async def status(self):
        b.check_role(self.role, "status")
        return self.printer.status()

    def on_status(self, callback):
        return self.printer.on_status(callback)

    async def close(self):
        await self.printer.close()


b.delegate_operations(MarlinBackend, MarlinPrinter,
                      [n for n in b.REQUIRED_ROLE if n not in ("status", "camera_stream")])
