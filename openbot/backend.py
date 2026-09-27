"""The one interface the UI and CLI talk to (PLAN.md §3).

Backends: LocalBackend (MakerBot over the network), MarlinBackend (Ender-3 over
USB), and later RemoteBackend (an OpenBot server). Each declares `capabilities`
so the UI only offers what the printer can do; operations a printer lacks raise
Unsupported. Role checks live here so every path enforces them identically.
"""

import enum
import functools


class Role(enum.IntEnum):
    VIEWER = 1
    OPERATOR = 2
    ADMIN = 3


class PermissionDenied(Exception):
    pass


class Unsupported(Exception):
    pass


# Capability names used by the UI.
CAMERA, NETWORK, FILAMENT, CERTIFICATE = "camera", "network", "filament", "certificate"
SD_CARD, CONSOLE, HOME, EMERGENCY_STOP, HEATED_BED = (
    "sd_card", "console", "home", "emergency_stop", "heated_bed")

# Minimum role for each operation. The server exposes exactly these names.
REQUIRED_ROLE = {
    "status": Role.VIEWER,
    "snapshot": Role.VIEWER,
    "camera_stream": Role.VIEWER,
    "start_print": Role.OPERATOR,
    "cancel": Role.OPERATOR,
    "pause": Role.OPERATOR,
    "resume": Role.OPERATOR,
    "acknowledge": Role.OPERATOR,
    "confirm_build_plate_cleared": Role.OPERATOR,
    "load_filament": Role.OPERATOR,
    "unload_filament": Role.OPERATOR,
    "stop_filament": Role.OPERATOR,
    "preheat": Role.OPERATOR,
    "cool": Role.OPERATOR,
    "home": Role.OPERATOR,
    "motors_off": Role.OPERATOR,
    "emergency_stop": Role.OPERATOR,
    "sd_list": Role.VIEWER,
    "sd_print": Role.OPERATOR,
    "send_gcode": Role.ADMIN,
    "network_state": Role.VIEWER,
    "wifi_scan": Role.ADMIN,
    "wifi_connect": Role.ADMIN,
    "wifi_disconnect": Role.ADMIN,
    "wifi_forget": Role.ADMIN,
    "wifi_enable": Role.ADMIN,
    "wifi_disable": Role.ADMIN,
    "get_static_ipv4": Role.ADMIN,
    "set_static_ipv4": Role.ADMIN,
    "use_dhcp": Role.ADMIN,
    "rename": Role.ADMIN,
}


def check_role(role, operation):
    needed = REQUIRED_ROLE[operation]
    if role < needed:
        raise PermissionDenied(f"{operation} needs {needed.name.lower()} access")


class PrinterBackend:
    role: Role = Role.ADMIN
    capabilities: frozenset = frozenset()
    machine_id: str = ""

    def supports(self, capability):
        return capability in self.capabilities

    def on_status(self, callback):
        raise Unsupported("status updates")

    async def close(self):
        pass


def delegate_operations(backend_cls, target_cls, names, target_attr="printer"):
    """Give backend_cls role-checked async methods that forward to self.<target_attr>.

    Operations the target doesn't implement raise Unsupported.
    """
    for name in names:
        impl = getattr(target_cls, name, None)

        def make(name, impl):
            async def method(self, *args, **kwargs):
                check_role(self.role, name)
                if impl is None:
                    raise Unsupported(f"this printer can't {name.replace('_', ' ')}")
                return await getattr(getattr(self, target_attr), name)(*args, **kwargs)
            method.__name__ = name
            if impl is not None:
                functools.update_wrapper(method, impl)
            return method
        setattr(backend_cls, name, make(name, impl))
