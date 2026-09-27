"""Build a .makerbot file: meta.json + print.jsontoolpath + thumbnails, zipped.

The meta.json template comes from charely6/mbotmake (GPL-3.0), whose output
queue3d has printed on a real Replicator+; field values follow MakerBot Print's
own file for the same printer.
"""

import copy
import io
import json
import math
import os
import tempfile
import uuid
import zipfile
from importlib import resources

from . import thumbnails, toolpath

FILAMENT_DIAMETER_MM = 1.75
MATERIAL_DENSITY = {"pla": 1.24, "im-pla": 1.24, "petg": 1.27, "abs": 1.04}


def _template():
    return json.loads(resources.files(__package__).joinpath(
        "data/meta_template.json").read_text())


def build_meta(stats, *, tool_type, material, extruder_temperature):
    meta = _template()
    mc = meta["miracle_config"]
    meta["bot_type"] = mc["_bot"] = "replicator_b"
    sx, sy, sz = toolpath.START_POSITION
    mc["gaggles"]["default"]["startPosition"] = {"x": sx, "y": sy, "z": sz}
    meta["tool_type"] = tool_type
    meta["tool_types"] = [tool_type]
    mc["_extruders"] = [tool_type]
    meta["material"] = material
    meta["materials"] = [material]
    mc["_materials"] = [material]
    meta["bounding_box"] = copy.deepcopy(stats.bbox)
    meta["total_commands"] = stats.commands
    meta["duration_s"] = round(stats.duration_s, 3)
    meta["commanded_duration_s"] = round(stats.duration_s, 3)
    meta["num_z_layers"] = stats.layers
    meta["num_z_transitions"] = max(stats.layers - 1, 0)
    meta["platform_temperature"] = 0          # no heated bed on the Replicator+
    meta["chamber_temperature"] = None
    meta["extruder_temperature"] = extruder_temperature
    meta["extruder_temperatures"] = [extruder_temperature]
    area = math.pi * (FILAMENT_DIAMETER_MM / 2) ** 2
    mass = stats.extrusion_mm * area * MATERIAL_DENSITY.get(material, 1.24) / 1000
    meta["extrusion_distance_mm"] = round(stats.extrusion_mm, 3)
    meta["extrusion_distances_mm"] = [meta["extrusion_distance_mm"]]
    meta["extrusion_mass_g"] = round(mass, 3)
    meta["extrusion_masses_g"] = [meta["extrusion_mass_g"]]
    meta["uuid"] = str(uuid.uuid4())
    meta["version"] = "1.2.0"
    return meta


def gcode_to_makerbot(gcode_path, out_path, *, tool_type, material,
                      extruder_temperature=None):
    """Convert a G-code file into a .makerbot. Returns (meta, stats)."""
    out_dir = os.path.dirname(os.path.abspath(out_path))
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        tp_path = os.path.join(tmp, "print.jsontoolpath")
        with open(gcode_path, encoding="utf-8", errors="replace") as src, \
                open(tp_path, "w") as dst:
            stats = toolpath.convert(src, dst)
        temp = extruder_temperature or stats.extruder_temperature
        if not temp:
            raise toolpath.ToolpathError("no extruder temperature set in G-code or profile")
        meta = build_meta(stats, tool_type=tool_type, material=material,
                          extruder_temperature=temp)
        partial = out_path + ".partial"
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tp_path, "print.jsontoolpath")
            z.writestr("meta.json", json.dumps(meta, indent=4))
            for name, png in thumbnails.render_all(stats.segments, stats.bbox).items():
                z.writestr(name, png)
        os.replace(partial, out_path)
    return meta, stats


def read_thumbnail(path, size="320x200"):
    with zipfile.ZipFile(path) as z:
        return z.read(f"thumbnail_{size}.png")


def read_toolpath(path):
    """Load the toolpath commands (for previews/tests)."""
    with zipfile.ZipFile(path) as z:
        return json.load(io.TextIOWrapper(z.open("print.jsontoolpath"), encoding="utf-8"))
