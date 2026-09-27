"""Models -> print files: mesh -> 3MF project -> OrcaSlicer -> .makerbot or .gcode.

The Replicator+ gets a .makerbot (G-code converted to a Birdwing toolpath);
Marlin printers such as the Ender-3 get the G-code itself, checked against
the printer's build volume.
"""

import os
import shutil
import tempfile
from dataclasses import dataclass, field

from . import mesh as mesh_mod
from .gcode_output import GcodeCheckError
from .mesh import MeshError
from .orca import SlicerError, find_orcaslicer, slice_project
from .package import gcode_to_makerbot
from .profiles import EXTRUDERS, PrintSettings, SettingsError, orca_config
from .toolpath import ToolpathError

__all__ = ["slice_model", "slice_models", "slice_plate", "SliceResult", "PrintSettings",
           "EXTRUDERS", "SlicerError", "SettingsError", "ToolpathError", "MeshError",
           "GcodeCheckError", "find_orcaslicer"]


@dataclass
class SliceResult:
    path: str
    settings: PrintSettings
    duration_s: float
    filament_mm: float
    filament_g: float
    layers: int
    bbox: dict
    warnings: list = field(default_factory=list)
    gcode_path: str | None = None


def slice_model(model_path, out_path, settings=None, *, keep_gcode=None):
    """Slice one STL/OBJ (centred on the plate)."""
    return slice_models([model_path], out_path, settings, keep_gcode=keep_gcode)


def slice_models(model_paths, out_path, settings=None, *, keep_gcode=None):
    """Load several STL/OBJ files, arrange them on the plate, and slice them together."""
    settings = (settings or PrintSettings()).validate()
    meshes = mesh_mod.arrange([mesh_mod.load(p) for p in model_paths],
                              bed=settings.machine_def.bed)
    return slice_plate(meshes, out_path, settings, keep_gcode=keep_gcode)


def slice_plate(meshes, out_path, settings=None, *, keep_gcode=None):
    """Slice meshes that are already placed on the bed (see mesh.arrange) into one print.

    keep_gcode: optional path to save the intermediate G-code (for previews/debugging).
    """
    settings = (settings or PrintSettings()).validate()
    machine = settings.machine_def
    if not meshes:
        raise MeshError("the plate is empty")
    for m in meshes:
        m.check_fits(machine.bed, machine.name)
    with tempfile.TemporaryDirectory(prefix="openbot-slice-") as tmp:
        project = os.path.join(tmp, "project.3mf")
        mesh_mod.write_project_3mf(meshes, orca_config(settings), project)
        gcode = slice_project(project, os.path.join(tmp, "out"))
        if keep_gcode:
            shutil.copyfile(gcode, keep_gcode)
        if machine.output == "makerbot":
            meta, stats = gcode_to_makerbot(gcode, out_path, tool_type=settings.extruder,
                                            material=settings.material,
                                            extruder_temperature=settings.nozzle_temperature)
            return SliceResult(path=out_path, settings=settings,
                               duration_s=stats.duration_s, filament_mm=stats.extrusion_mm,
                               filament_g=meta["extrusion_mass_g"], layers=stats.layers,
                               bbox=meta["bounding_box"], warnings=stats.warnings,
                               gcode_path=keep_gcode)
        from .gcode_output import analyse
        stats = analyse(gcode, machine.bed, machine.max_nozzle_temp)
        partial = out_path + ".partial"
        shutil.copyfile(gcode, partial)
        os.replace(partial, out_path)
    return SliceResult(path=out_path, settings=settings, duration_s=stats["duration_s"],
                       filament_mm=stats["filament_mm"], filament_g=stats["filament_g"],
                       layers=stats["layers"], bbox=stats["bbox"],
                       warnings=stats["warnings"], gcode_path=keep_gcode)
