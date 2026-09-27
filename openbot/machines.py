"""Printers OpenBot knows how to slice for and talk to."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Bed:
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_max: float

    @property
    def width(self):
        return self.x_max - self.x_min

    @property
    def depth(self):
        return self.y_max - self.y_min

    @property
    def center(self):
        return ((self.x_min + self.x_max) / 2, (self.y_min + self.y_max) / 2)


# tool_type -> display name and materials (name, default nozzle °C).
# Replicator+ temperatures follow MakerBot Print's defaults (its own test file: PLA 215 °C).
REPLICATOR_EXTRUDERS = {
    "mk13": {"name": "Smart Extruder+",
             "materials": {"pla": ("PLA", 215), "im-pla": ("Tough PLA", 215)}},
    "mk13_impla": {"name": "Tough Smart Extruder+",
                   "materials": {"im-pla": ("Tough PLA", 215), "pla": ("PLA", 215)}},
    "mk13_experimental": {"name": "Experimental Extruder",
                          "materials": {"pla": ("PLA", 215), "petg": ("PETG", 235),
                                        "custom": ("Custom", 215)}},
}

CREALITY_FILAMENTS = {"pla": "Creality Generic PLA", "petg": "Creality Generic PETG",
                      "abs": "Creality Generic ABS", "tpu": "Creality Generic TPU"}
# material -> (name, nozzle °C, bed °C). OrcaSlicer's generic Creality filaments are
# tuned for all-metal hotends (PETG 255, ABS 260, TPU 240); the stock Ender-3/Pro/V2
# hotend has a PTFE liner down to the nozzle that degrades above ~240 °C, so we set
# conservative values explicitly.
ENDER_EXTRUDERS = {
    "stock": {"name": "Stock hotend, 0.4 mm nozzle",
              "materials": {"pla": ("PLA", 205, 60), "petg": ("PETG", 235, 70),
                            "abs": ("ABS", 240, 100), "tpu": ("TPU", 225, 40)}},
}

# Quality name -> the OrcaSlicer process profile prefix it maps to.
ORCA_QUALITY_PREFIX = {
    "fine": "0.12mm Fine", "optimal": ("0.16mm Optimal", "0.15mm Optimal"),
    "standard": "0.20mm Standard", "draft": "0.24mm Draft",
    # Orca's "0.28mm SuperDraft" files are named "0.28mm Draft" inside.
    "superdraft": ("0.28mm SuperDraft", "0.28mm Draft"),
}


@dataclass(frozen=True)
class Machine:
    id: str
    name: str
    family: str             # "birdwing" (MakerBot, network) | "marlin" (USB serial)
    output: str             # "makerbot" | "gcode"
    connection: str         # "network" | "usb"
    bed: Bed
    heated_bed: bool
    extruders: dict = field(hash=False)
    default_extruder: str = ""
    # OrcaSlicer vendor profiles (Marlin machines)
    orca_vendor: str = ""
    orca_machine: str = ""
    orca_process_suffix: str = ""
    baud: int = 115200
    max_nozzle_temp: int = 250
    max_bed_temp: int = 0

    @property
    def file_extension(self):
        return ".makerbot" if self.output == "makerbot" else ".gcode"

    def qualities(self):
        """Available quality names, in fine-to-draft order."""
        if self.family == "birdwing":
            return ["fine", "standard", "draft"]
        from .slicing.orca_profiles import exists
        out = []
        for q, prefixes in ORCA_QUALITY_PREFIX.items():
            if self.process_profile(q, exists_fn=exists):
                out.append(q)
        return out

    def process_profile(self, quality, exists_fn=None):
        if exists_fn is None:
            from .slicing.orca_profiles import exists as exists_fn
        prefixes = ORCA_QUALITY_PREFIX.get(quality, ())
        if isinstance(prefixes, str):
            prefixes = (prefixes,)
        for p in prefixes:
            name = f"{p} {self.orca_process_suffix}"
            if exists_fn(self.orca_vendor, "process", name):
                return name
        return None


def _ender(id_, name, machine, suffix, z_max, max_nozzle):
    return Machine(id=id_, name=name, family="marlin", output="gcode", connection="usb",
                   bed=Bed(0, 220, 0, 220, z_max), heated_bed=True,
                   extruders=ENDER_EXTRUDERS, default_extruder="stock",
                   orca_vendor="Creality", orca_machine=machine,
                   orca_process_suffix=suffix, max_nozzle_temp=max_nozzle,
                   max_bed_temp=100)


MACHINES = {m.id: m for m in [
    Machine(id="replicator_plus", name="MakerBot Replicator+", family="birdwing",
            output="makerbot", connection="network",
            bed=Bed(-147.5, 147.5, -97.5, 97.5, 165), heated_bed=False,
            extruders=REPLICATOR_EXTRUDERS, default_extruder="mk13", max_nozzle_temp=250),
    _ender("ender3", "Creality Ender-3", "Creality Ender-3 0.4 nozzle",
           "@Creality Ender3 0.4", 250, 240),
    _ender("ender3_pro", "Creality Ender-3 Pro", "Creality Ender-3 Pro 0.4 nozzle",
           "@Creality Ender3 Pro 0.4", 250, 240),
    _ender("ender3_v2", "Creality Ender-3 V2", "Creality Ender-3 V2 0.4 nozzle",
           "@Creality Ender3V2", 250, 240),
    _ender("ender3_s1", "Creality Ender-3 S1", "Creality Ender-3 S1 0.4 nozzle",
           "@Creality Ender3S1", 270, 260),
    _ender("ender3_v3_se", "Creality Ender-3 V3 SE", "Creality Ender-3 V3 SE 0.4 nozzle",
           "@Creality Ender3V3SE 0.4", 250, 260),
]}

DEFAULT_MACHINE = "replicator_plus"
REPLICATOR = MACHINES["replicator_plus"]


def get(machine_id):
    try:
        return MACHINES[machine_id]
    except KeyError:
        raise ValueError(f"unknown printer type '{machine_id}'; choose one of "
                         f"{', '.join(MACHINES)}") from None


def for_marlin_machine_type(text):
    """Best guess from Marlin's M115 MACHINE_TYPE (e.g. 'Ender-3 Pro')."""
    t = (text or "").lower().replace(" ", "").replace("-", "")
    for key, mid in (("ender3v3se", "ender3_v3_se"), ("ender3s1", "ender3_s1"),
                     ("ender3v2", "ender3_v2"), ("ender3pro", "ender3_pro"),
                     ("ender3", "ender3")):
        if key in t:
            return mid
    return None
