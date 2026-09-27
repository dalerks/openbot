"""Read OrcaSlicer's bundled vendor profiles and flatten their `inherits` chains.

A vendor folder (e.g. profiles/Creality) holds machine/, process/ and filament/
JSON files; each may `inherits` another by name (often an fdm_* base in the
same folder). We merge child-over-parent into one flat dict per profile, then
machine + process + filament into one project config for slicing.
"""

import json
import os
from functools import lru_cache

from .orca import SlicerError, find_orcaslicer

# Keys that describe the profile file itself, not slicer settings.
_META_KEYS = {"type", "name", "inherits", "from", "setting_id", "instantiation",
              "filament_id", "description"}


def profiles_root():
    exe = find_orcaslicer()
    # .../OrcaSlicer.app/Contents/MacOS/OrcaSlicer -> .../Contents/Resources/profiles
    mac = os.path.normpath(os.path.join(os.path.dirname(exe), "..", "Resources", "profiles"))
    if os.path.isdir(mac):
        return mac
    linux = os.path.join(os.path.dirname(exe), "resources", "profiles")   # extracted AppImage
    if os.path.isdir(linux):
        return linux
    raise SlicerError("OrcaSlicer's bundled printer profiles weren't found")


@lru_cache(maxsize=None)
def _index(vendor):
    """name -> file path for every profile JSON in a vendor folder."""
    root = os.path.join(profiles_root(), vendor)
    out = {}
    for sub in ("machine", "process", "filament"):
        d = os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".json"):
                path = os.path.join(d, fn)
                try:
                    with open(path, encoding="utf-8") as f:
                        name = json.load(f).get("name") or fn[:-5]
                except (OSError, ValueError):
                    continue
                out.setdefault((sub, name), path)
    return out


def resolve(vendor, kind, name, _depth=0):
    """Flattened profile `name` of `kind` (machine | process | filament)."""
    if _depth > 20:
        raise SlicerError(f"profile inheritance too deep at '{name}'")
    path = _index(vendor).get((kind, name))
    if path is None:
        raise SlicerError(f"OrcaSlicer profile not found: {vendor}/{kind}/{name}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    parent = data.get("inherits")
    merged = resolve(vendor, kind, parent, _depth + 1) if parent else {}
    merged.update({k: v for k, v in data.items() if k not in _META_KEYS})
    return merged


def exists(vendor, kind, name):
    try:
        return (kind, name) in _index(vendor)
    except SlicerError:
        return False


def project_config(vendor, machine, process, filament):
    """One flat config: machine < process < filament, plus the *_settings_id names."""
    cfg = {}
    cfg.update(resolve(vendor, "machine", machine))
    cfg.update(resolve(vendor, "process", process))
    fil = resolve(vendor, "filament", filament)
    cfg.update(fil)
    cfg["printer_settings_id"] = machine
    cfg["print_settings_id"] = process
    cfg["filament_settings_id"] = [filament]
    return cfg
