"""Finish G-code for Marlin printers: sanity-check it and read OrcaSlicer's own estimates."""

import math
import re

_TIME = re.compile(r";\s*estimated printing time[^=]*=\s*(.+)$", re.I)
_GRAMS = re.compile(r";\s*filament used \[g\]\s*=\s*([\d.]+)", re.I)
_MM = re.compile(r";\s*filament used \[mm\]\s*=\s*([\d.]+)", re.I)
_LAYERS = re.compile(r";\s*total layer number:\s*(\d+)", re.I)
MARGIN = 3.0     # purge lines may sit right at the bed edge


class GcodeCheckError(ValueError):
    pass


def parse_duration(text):
    """'1d 2h 3m 4s' / '15m 3s' -> seconds."""
    total = 0
    for value, unit in re.findall(r"(\d+)\s*([dhms])", text):
        total += int(value) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total


def analyse(path, bed, max_nozzle=275):
    """Return stats and raise GcodeCheckError if any move leaves the machine's volume
    or a nozzle temperature exceeds `max_nozzle`."""
    stats = {"duration_s": 0.0, "filament_g": 0.0, "filament_mm": 0.0, "layers": 0,
             "bbox": {"x_min": math.inf, "x_max": -math.inf, "y_min": math.inf,
                      "y_max": -math.inf, "z_min": math.inf, "z_max": -math.inf},
             "warnings": []}
    x = y = z = 0.0
    absolute = True
    in_body = False          # the bounding box ignores the start G-code's purge line
    whole = {"x_min": math.inf, "x_max": -math.inf, "y_min": math.inf,
             "y_max": -math.inf, "z_min": math.inf, "z_max": -math.inf}
    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, raw in enumerate(f, 1):
            if raw.startswith(";"):
                if raw.startswith(";LAYER_CHANGE") or raw.startswith(";LAYER:"):
                    in_body = True       # OrcaSlicer/PrusaSlicer, Cura
                for rx, key, conv in ((_TIME, "duration_s", parse_duration),
                                      (_GRAMS, "filament_g", float),
                                      (_MM, "filament_mm", float),
                                      (_LAYERS, "layers", int)):
                    m = rx.match(raw.strip())
                    if m and not stats[key]:
                        stats[key] = conv(m.group(1))
                continue
            code = raw.split(";", 1)[0].split()
            if not code:
                continue
            cmd = code[0].upper()
            if cmd == "G90":
                absolute = True
            elif cmd == "G91":
                absolute = False
            elif cmd in ("G0", "G1"):
                words = {w[0].upper(): w[1:] for w in code[1:] if len(w) > 1}
                try:
                    nx = float(words["X"]) if "X" in words else None
                    ny = float(words["Y"]) if "Y" in words else None
                    nz = float(words["Z"]) if "Z" in words else None
                except ValueError:
                    continue
                if absolute:
                    x = x if nx is None else nx
                    y = y if ny is None else ny
                    z = z if nz is None else nz
                else:
                    x += nx or 0.0
                    y += ny or 0.0
                    z += nz or 0.0
                if (x < bed.x_min - MARGIN or x > bed.x_max + MARGIN or
                        y < bed.y_min - MARGIN or y > bed.y_max + MARGIN or
                        z > bed.z_max + 5):
                    raise GcodeCheckError(
                        f"line {lineno}: move to ({x:.1f}, {y:.1f}, {z:.1f}) is outside the "
                        f"printer's {bed.width:.0f} × {bed.depth:.0f} × {bed.z_max:.0f} mm "
                        "build volume")
                if "E" in words:
                    try:
                        extruding = float(words["E"]) > 0
                    except ValueError:
                        extruding = False
                    if extruding:
                        for b in ((stats["bbox"], whole) if in_body else (whole,)):
                            b["x_min"], b["x_max"] = min(b["x_min"], x), max(b["x_max"], x)
                            b["y_min"], b["y_max"] = min(b["y_min"], y), max(b["y_max"], y)
                            b["z_min"], b["z_max"] = min(b["z_min"], z), max(b["z_max"], z)
            elif cmd in ("M104", "M109"):
                for w in code[1:]:
                    if w[:1].upper() != "S":
                        continue
                    try:
                        temp = float(w[1:])
                    except ValueError:
                        continue
                    # Raised outside the try: GcodeCheckError is itself a ValueError.
                    if temp > max_nozzle:
                        raise GcodeCheckError(
                            f"line {lineno}: nozzle temperature {temp:g} °C is above the "
                            f"{max_nozzle} °C safety limit")
    if stats["bbox"]["x_min"] == math.inf:
        if whole["x_min"] == math.inf:
            raise GcodeCheckError("the G-code contains no extrusion")
        stats["bbox"] = whole    # no layer markers (hand-written or unusual slicer)
    return stats
