"""Print settings: printer, extruder (a per-print option, PLAN.md §2.1), material, quality."""

import json
from dataclasses import asdict, dataclass
from importlib import resources

from .. import machines
from ..machines import REPLICATOR_EXTRUDERS as EXTRUDERS  # noqa: F401 (back-compat name)

# Replicator+ layer heights (its profile is our own, not an Orca vendor one).
QUALITY = {"fine": 0.1, "standard": 0.2, "draft": 0.3}

# OrcaSlicer needs support_type and support_style set together, or it silently
# ignores the style (found by queue3d).
SUPPORT_STYLES = {
    "grid": ("normal(auto)", "grid"),
    "snug": ("normal(auto)", "snug"),
    "organic": ("tree(auto)", "organic"),
    "tree_slim": ("tree(auto)", "tree_slim"),
    "tree_hybrid": ("tree(auto)", "tree_hybrid"),
}

QUALITY_LABELS = {"fine": "Fine", "optimal": "Optimal", "standard": "Standard",
                  "draft": "Draft", "superdraft": "Super draft"}


class SettingsError(ValueError):
    pass


@dataclass
class PrintSettings:
    machine: str = machines.DEFAULT_MACHINE
    extruder: str = ""                  # "" = the machine's default extruder
    material: str = "pla"
    quality: str = "standard"
    infill_percent: int = 15
    walls: int = 2
    supports: bool = False
    support_style: str = "organic"
    adhesion: str = "none"              # none | brim | raft
    temperature: int | None = None      # nozzle override
    bed_temperature: int | None = None  # heated-bed override (Marlin printers)

    @property
    def machine_def(self):
        return machines.get(self.machine)

    def validate(self):
        try:
            m = self.machine_def
        except ValueError as e:
            raise SettingsError(str(e)) from None
        if not self.extruder:
            self.extruder = m.default_extruder
        if self.extruder not in m.extruders:
            raise SettingsError(f"{m.name} has no extruder '{self.extruder}'")
        mats = m.extruders[self.extruder]["materials"]
        if self.material not in mats:
            raise SettingsError(
                f"{m.extruders[self.extruder]['name']} can't print '{self.material}'; "
                f"choose one of {', '.join(mats)}")
        if m.family == "birdwing" and self.quality not in QUALITY:
            raise SettingsError(f"quality must be one of {', '.join(QUALITY)}")
        if m.family == "marlin":
            qualities = m.qualities()      # empty when OrcaSlicer isn't installed (a Pi server)
            if qualities and self.quality not in qualities:
                raise SettingsError(f"{m.name} qualities: {', '.join(qualities)}")
        if not 0 <= self.infill_percent <= 100:
            raise SettingsError("infill must be 0-100%")
        if not 1 <= self.walls <= 10:
            raise SettingsError("walls must be 1-10")
        if self.support_style not in SUPPORT_STYLES:
            raise SettingsError(f"support style must be one of {', '.join(SUPPORT_STYLES)}")
        if self.adhesion not in ("none", "brim", "raft"):
            raise SettingsError("adhesion must be none, brim or raft")
        if self.temperature is not None and not 170 <= self.temperature <= m.max_nozzle_temp:
            raise SettingsError(f"nozzle temperature must be 170-{m.max_nozzle_temp} °C "
                                f"on the {m.name}")
        if self.bed_temperature is not None:
            if not m.heated_bed:
                raise SettingsError(f"the {m.name} has no heated bed")
            if not 0 <= self.bed_temperature <= m.max_bed_temp:
                raise SettingsError(f"bed temperature must be 0-{m.max_bed_temp} °C")
        return self

    @property
    def extruder_name(self):
        return self.machine_def.extruders[self.extruder or
                                          self.machine_def.default_extruder]["name"]

    @property
    def material_name(self):
        m = self.machine_def
        return m.extruders[self.extruder or m.default_extruder]["materials"][self.material][0]

    @property
    def default_temperature(self):
        m = self.machine_def
        return m.extruders[self.extruder or m.default_extruder]["materials"][self.material][1]

    @property
    def nozzle_temperature(self):
        return self.temperature or self.default_temperature

    @property
    def default_bed_temperature(self):
        m = self.machine_def
        mat = m.extruders[self.extruder or m.default_extruder]["materials"][self.material]
        return mat[2] if len(mat) > 2 else None

    @property
    def effective_bed_temperature(self):
        if self.bed_temperature is not None:
            return self.bed_temperature
        return self.default_bed_temperature

    def to_dict(self):
        return asdict(self)


def base_profile():
    """Our own Replicator+ OrcaSlicer profile."""
    return json.loads(resources.files(__package__).joinpath(
        "data/replicator_plus_base.json").read_text())


def _stringify(cfg):
    """Orca project configs store scalars as strings."""
    out = {}
    for k, v in cfg.items():
        if isinstance(v, bool):
            out[k] = "1" if v else "0"
        elif isinstance(v, (int, float)):
            out[k] = str(v)
        elif isinstance(v, list):
            out[k] = [str(x) if isinstance(x, (int, float)) and not isinstance(x, bool)
                      else x for x in v]
        else:
            out[k] = v
    return out


def _common_overrides(cfg, s):
    cfg["sparse_infill_density"] = f"{s.infill_percent}%"
    cfg["wall_loops"] = str(s.walls)
    cfg["enable_support"] = "1" if s.supports else "0"
    cfg["support_type"], cfg["support_style"] = SUPPORT_STYLES[s.support_style]
    cfg["brim_type"] = "outer_only" if s.adhesion == "brim" else "no_brim"
    cfg["raft_layers"] = "3" if s.adhesion == "raft" else "0"
    cfg["enable_arc_fitting"] = "0"
    if s.nozzle_temperature:
        t = str(s.nozzle_temperature)
        cfg["nozzle_temperature"] = [t]
        cfg["nozzle_temperature_initial_layer"] = [t]


def orca_config(settings: PrintSettings):
    """Full OrcaSlicer project config for these settings."""
    s = settings.validate()
    m = s.machine_def
    if m.family == "birdwing":
        cfg = base_profile()
        cfg["filament_type"] = ["PETG" if s.material == "petg" else "PLA"]
        cfg["filament_settings_id"] = [f"OpenBot {s.material_name}"]
        cfg["layer_height"] = str(QUALITY[s.quality])
        cfg["initial_layer_print_height"] = "0.3" if s.quality != "fine" else "0.2"
        cfg["print_settings_id"] = f"OpenBot {QUALITY[s.quality]:.2f}mm {s.quality}"
        _common_overrides(cfg, s)
        return cfg
    from . import orca_profiles
    cfg = orca_profiles.project_config(
        m.orca_vendor, m.orca_machine, m.process_profile(s.quality),
        machines.CREALITY_FILAMENTS[s.material])
    cfg = _stringify(cfg)
    _common_overrides(cfg, s)
    bed = s.effective_bed_temperature
    if bed is not None:
        for k in list(cfg):
            if k.endswith("plate_temp") or k.endswith("plate_temp_initial_layer"):
                cfg[k] = [str(bed)]
    _check_limits(cfg, m)
    return cfg


def _check_limits(cfg, m):
    """Last line of defence: never hand the slicer temperatures the machine can't take."""
    for key in ("nozzle_temperature", "nozzle_temperature_initial_layer"):
        for v in cfg.get(key) or []:
            if float(v) > m.max_nozzle_temp:
                raise SettingsError(f"{key} {v} °C exceeds the {m.name}'s "
                                    f"{m.max_nozzle_temp} °C limit")
    if m.max_bed_temp:
        for k, vals in cfg.items():
            if k.endswith("plate_temp") or k.endswith("plate_temp_initial_layer"):
                for v in vals or []:
                    if float(v) > m.max_bed_temp:
                        raise SettingsError(f"bed temperature {v} °C exceeds the {m.name}'s "
                                            f"{m.max_bed_temp} °C limit")
