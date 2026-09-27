"""Typed views of the JSON the printer sends.

Shapes are taken from a real Replicator+ (fw 2.6.2) — see probe_out/ fixtures.
Every model keeps the original dict in `raw` so nothing is lost if the
firmware sends fields we don't model yet.
"""

from dataclasses import dataclass, field

# meta.json tool_type values for the Replicator+ extruders.
EXTRUDERS = {
    "mk13": "Smart Extruder+",
    "mk13_impla": "Tough Smart Extruder+",
    "mk13_experimental": "Experimental Extruder",
}

# toolheads.extruder[].tool_id -> tool_type. Only confirmed entries go here.
TOOL_IDS = {
    8: "mk13",  # observed 2026-09-27 on the owner's printer (believed Smart Extruder+)
}

CAMERA_FORMAT_YUYV = 1
CAMERA_FORMAT_JPEG = 2


def _version_str(fw):
    if not isinstance(fw, dict):
        return str(fw) if fw is not None else ""
    return "{major}.{minor}.{bugfix}.{build}".format(
        **{k: fw.get(k, 0) for k in ("major", "minor", "bugfix", "build")})


def mac_from_serial(iserial):
    """The last 12 hex digits of `iserial` are the printer's MAC address."""
    tail = (iserial or "")[-12:].lower()
    if len(tail) != 12 or any(c not in "0123456789abcdef" for c in tail):
        return None
    return ":".join(tail[i:i + 2] for i in range(0, 12, 2))


@dataclass
class PrinterInfo:
    """Identity of a printer, from discovery or the `handshake` reply."""
    serial: str
    name: str
    ip: str | None
    machine_type: str = ""
    bot_type: str = ""
    firmware: str = ""
    api_version: str = ""
    port: int = 9999
    ssl_port: int = 12309
    source: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_handshake(cls, d, ip=None, source="handshake"):
        ip_field = d.get("ip")
        return cls(
            serial=d.get("iserial", ""),
            name=d.get("machine_name", ""),
            ip=ip or (ip_field if ip_field not in (None, "None", "") else None),
            machine_type=d.get("machine_type", ""),
            bot_type=d.get("bot_type", ""),
            firmware=_version_str(d.get("firmware_version")),
            api_version=d.get("api_version", ""),
            port=int(d.get("port", 9999) or 9999),
            ssl_port=int(d.get("ssl_port", 12309) or 12309),
            source=source,
            raw=d,
        )

    @property
    def mac(self):
        return mac_from_serial(self.serial)

    @property
    def is_replicator_plus(self):
        return self.bot_type == "replicator_b"


@dataclass
class Extruder:
    index: int
    tool_id: int | None
    present: bool
    current_temperature: float
    target_temperature: float
    filament_present: bool | None     # None = the printer can't tell
    preheating: bool
    error: int

    @property
    def tool_type(self):
        return TOOL_IDS.get(self.tool_id)

    @property
    def display_name(self):
        return EXTRUDERS.get(self.tool_type, f"Unknown extruder (tool_id {self.tool_id})")


@dataclass
class Process:
    """The printer's `current_process` (print, load filament, leveling, ...)."""
    name: str
    step: str
    progress: int | None
    cancellable: bool
    complete: bool
    cancelled: bool
    error: dict | None
    filename: str | None
    elapsed_time: float | None
    username: str | None
    methods: list
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d):
        return cls(
            name=d.get("name", ""),
            step=d.get("step", ""),
            progress=d.get("progress"),
            cancellable=bool(d.get("cancellable")),
            complete=bool(d.get("complete")),
            cancelled=bool(d.get("cancelled")),
            error=d.get("error"),
            filename=d.get("filename") or d.get("filepath"),
            elapsed_time=d.get("elapsed_time"),
            username=d.get("username"),
            methods=list(d.get("methods") or []),
            raw=d,
        )

    @property
    def is_print(self):
        return self.name == "PrintProcess"


@dataclass
class PrinterStatus:
    """Parsed `get_system_information` / state/system notification payload."""
    name: str
    machine_type: str
    bot_type: str
    ip: str | None
    firmware: str
    extruders: list
    process: Process | None
    sound: bool | None
    raw: dict = field(default_factory=dict, repr=False)
    bed: tuple | None = None          # (current, target) °C on printers with a heated bed

    @classmethod
    def from_info(cls, d):
        extruders = []
        for e in (d.get("toolheads") or {}).get("extruder") or []:
            extruders.append(Extruder(
                index=e.get("index", len(extruders)),
                tool_id=e.get("tool_id"),
                present=bool(e.get("tool_present")),
                current_temperature=e.get("current_temperature", 0),
                target_temperature=e.get("target_temperature", 0),
                filament_present=bool(e.get("filament_presence")),
                preheating=bool(e.get("preheating")),
                error=e.get("error", 0),
            ))
        proc = d.get("current_process")
        return cls(
            name=d.get("machine_name", ""),
            machine_type=d.get("machine_type", ""),
            bot_type=d.get("bot_type", ""),
            ip=d.get("ip"),
            firmware=_version_str(d.get("firmware_version")),
            extruders=extruders,
            process=Process.from_dict(proc) if proc else None,
            sound=d.get("sound"),
            raw=d,
        )

    @property
    def idle(self):
        return self.process is None

    @property
    def extruder(self):
        return self.extruders[0] if self.extruders else None


@dataclass
class WifiNetwork:
    path: str          # connman service path; pass to wifi_connect/forget
    name: str          # SSID ("" for hidden networks)
    strength: int      # 0..100
    password: str      # "none" | "stored" | "required"

    @property
    def hidden(self):
        return not self.name

    @property
    def secured(self):
        return self.password != "none"

    @property
    def saved(self):
        return self.password == "stored"


@dataclass
class NetworkState:
    state: str                 # "offline" | "ethernet" | "wifi"
    ip: str | None = None
    netmask: str | None = None
    gateway: str | None = None
    dns: list = field(default_factory=list)
    static: bool = False
    name: str | None = None    # SSID when on wifi
    wifi_radio: str | None = None   # "enabled" | "disabled"
    tethering: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d):
        return cls(
            state=d.get("state", "offline"),
            ip=d.get("ip"),
            netmask=d.get("netmask"),
            gateway=d.get("gateway"),
            dns=list(d.get("dns") or []),
            static=bool(d.get("static")),
            name=d.get("name"),
            wifi_radio=d.get("wifi"),
            tethering=bool(d.get("tethering")),
            raw=d,
        )


@dataclass
class StaticIpConfig:
    use_static: bool
    ip: str | None = None
    netmask: str | None = None
    gateway: str | None = None
    dns: list | None = None

    @classmethod
    def from_dict(cls, d):
        return cls(use_static=bool(d.get("use_static")), ip=d.get("ip"),
                   netmask=d.get("netmask"), gateway=d.get("gateway"), dns=d.get("dns"))


@dataclass
class CameraFrame:
    width: int
    height: int
    format: int     # CAMERA_FORMAT_JPEG or CAMERA_FORMAT_YUYV
    data: bytes

    @property
    def is_jpeg(self):
        return self.format == CAMERA_FORMAT_JPEG
