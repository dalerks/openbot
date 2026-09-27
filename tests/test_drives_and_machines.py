import os

import pytest

from openbot import drives, machines
from openbot.slicing import PrintSettings, SettingsError, slice_model
from openbot.slicing.gcode_output import GcodeCheckError, analyse, parse_duration
from openbot.slicing.orca import SlicerError, find_orcaslicer


def test_safe_filename():
    assert drives.safe_filename("My Cube (v2) é.stl", ".gcode") == "My_Cube_v2_e.gcode"
    assert drives.safe_filename("???.stl", ".gcode") == "print.gcode"
    assert len(drives.safe_filename("x" * 100, ".gcode")) == 24 + 6


def test_save_to_drive_and_exfat_warning(tmp_path):
    src = tmp_path / "a.gcode"
    src.write_text("G28\n")
    card = tmp_path / "card"
    card.mkdir()
    d = drives.Drive(str(card), "SDCARD", "ExFAT", 8_000_000_000, 4_000_000_000)
    assert any("exFAT" in w for w in d.warnings_for("gcode"))
    assert drives.Drive(str(card), "S", "MS-DOS FAT32", 1, 1).warnings_for("gcode") == []
    out = drives.save_to_drive(str(src), d, "part.gcode")
    assert open(out).read() == "G28\n"
    assert not os.path.exists(out + ".tmp")
    full = drives.Drive(str(card), "FULL", "MS-DOS FAT32", 10, 1)
    with pytest.raises(drives.DriveError, match="space"):
        drives.save_to_drive(str(src), full)


def test_list_removable_runs():
    assert isinstance(drives.list_removable(), list)


def test_marlin_machine_type_guess():
    assert machines.for_marlin_machine_type("Ender-3 Pro") == "ender3_pro"
    assert machines.for_marlin_machine_type("Ender-3 V2") == "ender3_v2"
    assert machines.for_marlin_machine_type("ENDER 3 S1") == "ender3_s1"
    assert machines.for_marlin_machine_type("Prusa") is None


def test_parse_duration():
    assert parse_duration("1d 2h 3m 4s") == 93784
    assert parse_duration("15m 3s") == 903


def test_gcode_bounds_check(tmp_path):
    bed = machines.get("ender3_pro").bed
    ok = tmp_path / "ok.gcode"
    ok.write_text("; estimated printing time (normal mode) = 1m 5s\n"
                  "; filament used [g] = 1.5\nG90\nG1 X2 Y10 Z0.2 E3 ; purge\n"
                  ";LAYER_CHANGE\nG1 X10 Y10 Z0.2\nG1 X100 Y100 E5\n")
    s = analyse(str(ok), bed)
    assert s["duration_s"] == 65 and s["filament_g"] == 1.5
    assert s["bbox"]["x_min"] == 100                  # purge line before layer 1 ignored
    bad = tmp_path / "bad.gcode"
    bad.write_text("G90\n;LAYER_CHANGE\nG1 X300 Y10 E1\n")
    with pytest.raises(GcodeCheckError, match="outside"):
        analyse(str(bad), bed)
    hot = tmp_path / "hot.gcode"
    hot.write_text("M104 S300\n;LAYER_CHANGE\nG1 X10 Y10 E1\n")
    with pytest.raises(GcodeCheckError, match="safety"):
        analyse(str(hot), bed)
    warm = tmp_path / "warm.gcode"
    warm.write_text("M104 S245\n;LAYER_CHANGE\nG1 X10 Y10 E1\n")
    with pytest.raises(GcodeCheckError, match="240"):
        analyse(str(warm), bed, max_nozzle=240)       # PTFE-lined Ender-3 limit


def test_ender_temperature_limits():
    with pytest.raises(SettingsError, match="240"):
        PrintSettings(machine="ender3_pro", temperature=250).validate()
    with pytest.raises(SettingsError, match="no heated bed"):
        PrintSettings(machine="replicator_plus", bed_temperature=60).validate()
    with pytest.raises(SettingsError, match="can't print"):
        PrintSettings(machine="replicator_plus", material="abs").validate()


def _have_orca():
    try:
        find_orcaslicer()
        return True
    except SlicerError:
        return False


@pytest.mark.skipif(not _have_orca(), reason="OrcaSlicer not installed")
def test_machine_beds_match_orca_profiles():
    from openbot.slicing import orca_profiles
    for m in machines.MACHINES.values():
        if m.family != "marlin":
            continue
        cfg = orca_profiles.resolve(m.orca_vendor, "machine", m.orca_machine)
        xs = [float(p.split("x")[0]) for p in cfg["printable_area"]]
        ys = [float(p.split("x")[1]) for p in cfg["printable_area"]]
        assert (min(xs), max(xs), min(ys), max(ys)) == \
            (m.bed.x_min, m.bed.x_max, m.bed.y_min, m.bed.y_max), m.id
        assert float(cfg["printable_height"]) == m.bed.z_max, m.id
        assert "standard" in m.qualities(), m.id


@pytest.mark.skipif(not _have_orca(), reason="OrcaSlicer not installed")
@pytest.mark.parametrize("material,nozzle,bed", [("pla", 205, 60), ("petg", 235, 70)])
def test_ender_slice_to_gcode(tmp_path, material, nozzle, bed):
    from test_slicing import _write_cube_stl
    stl = tmp_path / "cube.stl"
    _write_cube_stl(stl, size=15)
    out = tmp_path / "cube.gcode"
    r = slice_model(str(stl), str(out), PrintSettings(machine="ender3_pro",
                                                     material=material, quality="draft"))
    text = out.read_text()
    assert f"M109 S{nozzle}" in text and f"M190 S{bed}" in text and "G28" in text
    centre = (r.bbox["x_min"] + r.bbox["x_max"]) / 2
    assert 105 < centre < 115                          # centred on the 220 mm bed
    assert r.duration_s > 0 and r.filament_g > 0 and r.layers > 0
