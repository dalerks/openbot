"""Convert slicer G-code into a Birdwing `print.jsontoolpath`.

Format matches MakerBot's own output (tests/fixtures/1cm_block_repplus.makerbot,
sliced by MakerBot Print for the Replicator+):

* every move is {"function": "move", "metadata": {"relative": {"a": true,
  "x": false, "y": false, "z": false}}, "parameters": {"x","y","z","a","feedrate"},
  "tags": [...]} - absolute XYZ in bed-centred mm, `a` = filament mm pushed
  during this move (relative), feedrate in mm/s;
* fans are `toggle_fan` / `fan_duty` (0..1);
* there are NO temperature commands - the printer heats (initial/final_heating)
  from meta.json's extruder_temperature before running the toolpath, and runs
  its own homing/start/end sequences;
* layer boundaries are marked with "Layer Section" comments.

Partly informed by charely6/mbotmake (GPL-3.0).
"""

import json
import math
from dataclasses import dataclass, field

# Replicator+ build volume, bed-centred (PLAN.md / queue3d profile, verified bed 295x195x165).
BED_X = 147.5
BED_Y = 97.5
MAX_Z = 165.0
# The firmware parks the head here before the toolpath starts (MakerBot's own file
# begins with a move at this position).
START_POSITION = (-150.0, -100.0, 0.2)
# Allow the start position itself, which sits just outside the printable area.
LIMIT_X = 150.5
LIMIT_Y = 100.5

MAX_FEEDRATE = 300.0        # mm/s; MakerBot's file tops out at 150 for travel
MAX_EXTRUDER_TEMP = 250     # hard stop; the Smart Extruder+ runs PLA at ~215

# OrcaSlicer ;TYPE: -> MakerBot move tag (MakerBot Print shows these in its preview;
# the firmware treats every move the same).
TYPE_TAGS = {
    "outer wall": "Inset", "inner wall": "Inset", "overhang wall": "Inset",
    "perimeter": "Inset", "external perimeter": "Inset", "overhang perimeter": "Inset",
    "sparse infill": "Infill", "internal solid infill": "Infill", "solid infill": "Infill",
    "top surface": "Infill", "bottom surface": "Infill", "bridge": "Infill",
    "internal bridge": "Infill", "gap infill": "Infill", "ironing": "Infill",
    "support": "Support", "support interface": "Support", "support transition": "Support",
    "skirt": "Infill", "brim": "Infill", "raft": "Support",
}

MOVE_META = {"relative": {"a": True, "x": False, "y": False, "z": False}}


class ToolpathError(ValueError):
    """The G-code can't safely become a toolpath (out of bounds, unsupported command...)."""


@dataclass
class ToolpathStats:
    commands: int = 0
    moves: int = 0
    extrusion_mm: float = 0.0
    duration_s: float = 0.0
    layers: int = 0
    extruder_temperature: int = 0
    bed_temperature: int = 0
    bbox: dict = field(default_factory=lambda: {
        "x_min": math.inf, "x_max": -math.inf, "y_min": math.inf,
        "y_max": -math.inf, "z_min": math.inf, "z_max": -math.inf})
    warnings: list = field(default_factory=list)
    segments: list = field(default_factory=list, repr=False)   # for thumbnails


class _Writer:
    """Streams the JSON array so huge toolpaths never sit in memory as one string."""

    def __init__(self, fh):
        self.fh = fh
        self.count = 0
        fh.write("[\n")

    def add(self, function, parameters, tags=(), metadata=None):
        cmd = {"command": {"function": function, "metadata": metadata or {},
                           "parameters": parameters, "tags": list(tags)}}
        self.fh.write((",\n" if self.count else "") + json.dumps(cmd, separators=(",", ":")))
        self.count += 1

    def close(self):
        self.fh.write("\n]\n")


def _words(line):
    out = {}
    for w in line.split()[1:]:
        if len(w) >= 2:
            try:
                out[w[0].upper()] = float(w[1:])
            except ValueError:
                pass
    return out


def convert(gcode_lines, out_fh, keep_segments=20000):
    """Translate G-code lines into a jsontoolpath written to `out_fh`. Returns ToolpathStats."""
    st = ToolpathStats()
    w = _Writer(out_fh)
    x, y, z = START_POSITION
    e = 0.0                     # absolute E position as the G-code sees it
    abs_xyz = True
    abs_e = True                # G-code default is M82 (absolute E)
    feed = 23.0                 # mm/s, MakerBot's first-travel speed
    fan_on = False
    feature = None
    layer = 0
    pending_layer_z = None
    layer_zs = set()
    seg_stride = 1

    def emit_layer_comment(zv):
        nonlocal layer
        w.add("comment", {"comment": f"Layer Section {layer} ({layer})"})
        w.add("comment", {"comment": "Material 0"})
        w.add("comment", {"comment": f"Upper Position  {zv:g}"})
        layer += 1

    for lineno, raw in enumerate(gcode_lines, 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            c = line[1:].strip()
            if c.startswith("TYPE:"):
                feature = c[5:].strip().lower()
            elif c.startswith("Z:") and pending_layer_z is None:
                try:
                    pending_layer_z = float(c[2:])
                except ValueError:
                    pass
            elif c == "LAYER_CHANGE":
                pending_layer_z = None
            continue
        code = line.split(";", 1)[0].strip()
        if not code:
            continue
        cmd = code.split()[0].upper()
        p = _words(code)

        if cmd in ("G0", "G1"):
            nx = x if "X" not in p else (p["X"] if abs_xyz else x + p["X"])
            ny = y if "Y" not in p else (p["Y"] if abs_xyz else y + p["Y"])
            nz = z if "Z" not in p else (p["Z"] if abs_xyz else z + p["Z"])
            if "F" in p:
                if p["F"] <= 0:
                    raise ToolpathError(f"line {lineno}: feedrate must be positive")
                feed = p["F"] / 60.0
            de = 0.0
            if "E" in p:
                de = (p["E"] - e) if abs_e else p["E"]
                e = p["E"] if abs_e else e + p["E"]
            moved = (nx, ny, nz) != (x, y, z)
            if not moved and de == 0.0:
                continue                      # pure feedrate change
            if abs(nx) > LIMIT_X or abs(ny) > LIMIT_Y or nz < 0 or nz > MAX_Z + 10:
                raise ToolpathError(
                    f"line {lineno}: move to ({nx:.1f}, {ny:.1f}, {nz:.1f}) is outside the "
                    f"Replicator+ build volume (±{BED_X} × ±{BED_Y} × {MAX_Z} mm, "
                    "bed-centred). Is the model placed on the plate and the profile's "
                    "origin at the bed centre?")
            if feed > MAX_FEEDRATE:
                st.warnings.append(f"line {lineno}: feedrate {feed:.0f} mm/s capped at "
                                   f"{MAX_FEEDRATE:.0f}")
            f = min(feed, MAX_FEEDRATE)
            if de > 0 and moved:
                tag = TYPE_TAGS.get(feature or "", "Infill")
            elif de > 0:
                tag = "Restart"
            elif de < 0:
                tag = "Retract"
            else:
                tag = "Travel Move"
            if de > 0 and moved and pending_layer_z is not None or \
                    (de > 0 and moved and layer == 0):
                emit_layer_comment(nz)
                pending_layer_z = None
            w.add("move", {"a": round(de, 6), "feedrate": round(f, 4),
                           "x": round(nx, 5), "y": round(ny, 5), "z": round(nz, 5)},
                  [tag], MOVE_META)
            st.moves += 1
            dist = math.dist((x, y, z), (nx, ny, nz)) if moved else abs(de)
            st.duration_s += dist / f
            if de > 0:
                st.extrusion_mm += de
                if moved:
                    b = st.bbox
                    for vx, vy in ((x, y), (nx, ny)):
                        b["x_min"] = min(b["x_min"], vx)
                        b["x_max"] = max(b["x_max"], vx)
                        b["y_min"] = min(b["y_min"], vy)
                        b["y_max"] = max(b["y_max"], vy)
                    b["z_min"] = min(b["z_min"], nz)
                    b["z_max"] = max(b["z_max"], nz)
                    layer_zs.add(round(nz, 3))
                    st.segments.append((x, y, nx, ny, nz))
                    if len(st.segments) > keep_segments * 2:
                        seg_stride *= 2
                        st.segments = st.segments[::2]
            x, y, z = nx, ny, nz
        elif cmd == "G92":
            if "E" in p:
                e = p["E"]
            if any(k in p for k in "XYZ"):
                raise ToolpathError(f"line {lineno}: G92 on X/Y/Z is not supported")
        elif cmd == "G90":
            abs_xyz = True
        elif cmd == "G91":
            abs_xyz = False
        elif cmd == "M82":
            abs_e = True
        elif cmd == "M83":
            abs_e = False
        elif cmd in ("M104", "M109"):
            t = int(p.get("S", p.get("R", 0)))
            if t > MAX_EXTRUDER_TEMP:
                raise ToolpathError(f"line {lineno}: extruder temperature {t}°C is above "
                                    f"the {MAX_EXTRUDER_TEMP}°C safety limit")
            if t and not st.extruder_temperature:
                st.extruder_temperature = t
            elif t and t != st.extruder_temperature:
                st.warnings.append(f"line {lineno}: temperature change to {t}°C ignored; "
                                   "the printer holds one temperature per print")
        elif cmd in ("M140", "M190"):
            t = int(p.get("S", 0))
            if t:
                st.bed_temperature = t
                st.warnings.append("the Replicator+ has no heated bed; bed temperature ignored")
        elif cmd == "M106":
            duty = max(0.0, min(1.0, p.get("S", 255) / 255.0))
            if duty == 0:
                if fan_on:
                    w.add("toggle_fan", {"index": 0, "value": False})
                    fan_on = False
            else:
                if not fan_on:
                    w.add("toggle_fan", {"index": 0, "value": True})
                    fan_on = True
                w.add("fan_duty", {"index": 0, "value": round(duty, 3)})
        elif cmd == "M107":
            if fan_on:
                w.add("toggle_fan", {"index": 0, "value": False})
                fan_on = False
        elif cmd in ("G2", "G3"):
            raise ToolpathError(f"line {lineno}: arc moves (G2/G3) aren't supported; "
                                "turn off arc fitting in the slicer profile")
        elif cmd == "G20":
            raise ToolpathError(f"line {lineno}: inch units (G20) aren't supported")
        elif cmd.startswith("T") and cmd not in ("T0",):
            raise ToolpathError(f"line {lineno}: the Replicator+ has one extruder ({cmd})")
        # Everything else (G21, G28, M73 progress, M204 accel, M84, G4, ...) is
        # handled by the printer's own firmware sequences and is dropped.

    if fan_on:
        w.add("toggle_fan", {"index": 0, "value": False})
    w.add("comment", {"comment": "End of print"})
    w.close()
    st.commands = w.count
    st.layers = len(layer_zs)
    if st.moves == 0 or st.extrusion_mm == 0:
        raise ToolpathError("the G-code contains no extrusion")
    return st
