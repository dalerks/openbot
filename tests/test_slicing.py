import io
import json
import os
import struct
import zipfile

import pytest

from openbot.slicing import PrintSettings, SettingsError, slice_model
from openbot.slicing import mesh as mesh_mod
from openbot.slicing import toolpath
from openbot.slicing.orca import SlicerError, find_orcaslicer
from openbot.slicing.package import build_meta, gcode_to_makerbot, read_toolpath
from openbot.slicing.profiles import orca_config
from openbot.printer.client import check_print_file

# MakerBot Print's own sample (not redistributable, so not in the repo). Tests that compare
# against it run only where it exists: copy it from
# "MakerBot Print.app/Contents/Resources/app.asar.unpacked/diagnostics/data/1cm_x_1cm_block_Rep+.makerbot".
REFERENCE = "tests/fixtures/1cm_block_repplus.makerbot"
needs_reference = pytest.mark.skipif(not os.path.exists(REFERENCE),
                                     reason="MakerBot reference file not present")


def convert(text):
    out = io.StringIO()
    stats = toolpath.convert(text.splitlines(), out)
    return json.loads(out.getvalue()), stats


def moves(cmds):
    return [c["command"] for c in cmds if c["command"]["function"] == "move"]


def test_absolute_e_becomes_relative_a():
    cmds, st = convert("M82\nG92 E0\nG1 X0 Y0 Z0.2 F3000\nG1 X10 E1.5 F1200\n"
                       "G1 X20 E2.0\nG92 E0\nG1 X30 E0.25\nM104 S215\n")
    m = moves(cmds)
    assert [c["parameters"]["a"] for c in m] == [0.0, 1.5, 0.5, 0.25]
    assert m[1]["parameters"]["feedrate"] == 20.0          # F1200 mm/min -> 20 mm/s
    assert all(c["metadata"] == {"relative": {"a": True, "x": False, "y": False,
                                              "z": False}} for c in m)
    assert st.extrusion_mm == pytest.approx(2.25)
    assert st.extruder_temperature == 215


def test_relative_e_and_tags():
    g = ("M83\nG1 Z0.3 F600\n;LAYER_CHANGE\n;Z:0.3\n;TYPE:Outer wall\n"
         "G1 X0 Y0 F9000\nG1 E0.5 F2100\nG1 X5 E0.2 F1200\n;TYPE:Sparse infill\n"
         "G1 X6 Y1 E0.1\nG1 E-0.5 F2100\n;TYPE:Support\nG1 X7 E0.1\n")
    cmds, st = convert(g)
    tags = [c["tags"][0] for c in moves(cmds)]
    assert tags == ["Travel Move", "Travel Move", "Restart", "Inset", "Infill", "Retract",
                    "Support"]
    comments = [c["command"]["parameters"]["comment"] for c in cmds
                if c["command"]["function"] == "comment"]
    assert comments[0] == "Layer Section 0 (0)" and comments[-1] == "End of print"


def test_fan_commands():
    cmds, _ = convert("G1 X0 Y0 Z0.2 E1 F600\nM106 S255\nM106 S127\nM107\nM106 S0\n")
    fans = [(c["command"]["function"], c["command"]["parameters"]["value"]) for c in cmds
            if "fan" in c["command"]["function"]]
    assert fans == [("toggle_fan", True), ("fan_duty", 1.0), ("fan_duty", 0.498),
                    ("toggle_fan", False)]


@pytest.mark.parametrize("gcode,msg", [
    ("G1 X200 Y0 Z1 E1", "outside"),
    ("G1 X0 Y0 Z200 E1", "outside"),
    ("M104 S300\nG1 X0 Y0 Z1 E1", "safety limit"),
    ("G2 X1 Y1 I1 J0 E1", "arc"),
    ("G20\nG1 X1 E1", "inch"),
    ("T1\nG1 X1 E1", "one extruder"),
    ("G1 X1 Y1 Z1", "no extrusion"),
])
def test_unsafe_gcode_is_rejected(gcode, msg):
    with pytest.raises(toolpath.ToolpathError, match=msg):
        convert(gcode)


def test_bed_temperature_is_ignored_with_warning():
    _, st = convert("M140 S60\nG1 X0 Y0 Z0.2 E1 F600\n")
    assert any("no heated bed" in w for w in st.warnings)


@needs_reference
def test_meta_matches_makerbot_fields():
    ref = json.loads(zipfile.ZipFile(REFERENCE).read("meta.json"))
    _, st = convert("M104 S215\nG1 X0 Y0 Z0.3 F600\nG1 X10 E1\nG1 Z0.5\nG1 X0 E2\n")
    meta = build_meta(st, tool_type="mk13", material="pla", extruder_temperature=215)
    for key in ("bot_type", "tool_type", "tool_types", "material", "materials",
                "extruder_temperature", "extruder_temperatures", "platform_temperature"):
        assert meta[key] == ref[key], key
    for key in ("bounding_box", "duration_s", "extrusion_distance_mm", "num_z_layers",
                "total_commands", "uuid", "version"):
        assert key in meta
    assert meta["miracle_config"]["gaggles"]["default"]["startPosition"] == \
        {"x": -150.0, "y": -100.0, "z": 0.2}
    assert meta["num_z_layers"] == 2


def test_package_roundtrip(tmp_path):
    g = tmp_path / "t.gcode"
    g.write_text("M83\nM104 S215\nG1 X0 Y0 Z0.3 F600\nG1 X10 E1\nG1 Y10 E1\n")
    out = tmp_path / "t.makerbot"
    meta, st = gcode_to_makerbot(str(g), str(out), tool_type="mk13", material="pla")
    names = zipfile.ZipFile(out).namelist()
    assert {"meta.json", "print.jsontoolpath", "thumbnail_55x40.png",
            "thumbnail_110x80.png", "thumbnail_320x200.png"} <= set(names)
    assert zipfile.ZipFile(out).read("thumbnail_55x40.png")[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(read_toolpath(str(out))) == meta["total_commands"]
    assert check_print_file(str(out)) == []


@needs_reference
def test_reference_file_shape_is_what_we_emit():
    """Every command MakerBot's own file uses has the same shape as ours."""
    ref = read_toolpath(REFERENCE)
    ref_shapes = {(c["command"]["function"], tuple(sorted(c["command"]["parameters"])))
                  for c in ref}
    cmds, _ = convert("M83\nG1 X0 Y0 Z0.3 F600\nM106 S200\nG1 X10 E1\nM107\n")
    ours = {(c["command"]["function"], tuple(sorted(c["command"]["parameters"])))
            for c in cmds}
    assert ours <= ref_shapes


def test_settings_validation_and_config():
    with pytest.raises(SettingsError, match="can't print"):
        PrintSettings(extruder="mk13", material="petg").validate()
    cfg = orca_config(PrintSettings(extruder="mk13_impla", material="im-pla",
                                    quality="fine", supports=True, support_style="grid",
                                    adhesion="raft", infill_percent=30))
    assert cfg["layer_height"] == "0.1" and cfg["enable_support"] == "1"
    assert (cfg["support_type"], cfg["support_style"]) == ("normal(auto)", "grid")
    assert cfg["raft_layers"] == "3" and cfg["sparse_infill_density"] == "30%"
    assert cfg["use_relative_e_distances"] == "1"
    assert all(v == ["0"] for k, v in cfg.items() if k.endswith("plate_temp"))


def _write_cube_stl(path, size=10.0, offset=(50, 50, 5)):
    ox, oy, oz = offset
    s = size
    v = [(ox + x * s, oy + y * s, oz + z * s) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(faces)))
        for a, b, c in faces:
            f.write(struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0))


def test_mesh_is_centered_and_dropped(tmp_path):
    p = tmp_path / "cube.stl"
    _write_cube_stl(p)
    m = mesh_mod.load(str(p)).placed_on_bed()
    (x0, y0, z0), (x1, y1, z1) = m.bounds()
    assert (x0 + x1) / 2 == pytest.approx(0) and (y0 + y1) / 2 == pytest.approx(0)
    assert z0 == pytest.approx(0) and z1 == pytest.approx(10)
    big = mesh_mod.Mesh([(0, 0, 0), (400, 0, 0), (0, 10, 10)], [(0, 1, 2)])
    with pytest.raises(mesh_mod.MeshError, match="builds up to"):
        big.check_fits()


def _have_orca():
    try:
        find_orcaslicer()
        return True
    except SlicerError:
        return False


@pytest.mark.skipif(not _have_orca(), reason="OrcaSlicer not installed")
def test_full_pipeline_with_orcaslicer(tmp_path):
    stl = tmp_path / "cube.stl"
    _write_cube_stl(stl)
    out = tmp_path / "cube.makerbot"
    r = slice_model(str(stl), str(out), PrintSettings(quality="draft"))
    assert os.path.getsize(out) > 1000
    assert 20 <= r.layers <= 40                   # 10 mm at 0.3 mm layers
    assert r.bbox["x_min"] < -4 and r.bbox["x_max"] > 4   # centred on the bed
    assert check_print_file(str(out)) == []


def test_arrange_places_objects_without_overlap(tmp_path):
    p = tmp_path / "cube.stl"
    _write_cube_stl(p, size=40)
    cube = mesh_mod.load(str(p))
    placed = mesh_mod.arrange([cube] * 12)
    boxes = [m.bounds() for m in placed]
    for i, (lo, hi) in enumerate(boxes):
        assert -147.5 <= lo[0] and hi[0] <= 147.5 and -97.5 <= lo[1] and hi[1] <= 97.5
        assert lo[2] == pytest.approx(0)
        for lo2, hi2 in boxes[i + 1:]:
            assert hi[0] <= lo2[0] or hi2[0] <= lo[0] or hi[1] <= lo2[1] or hi2[1] <= lo[1]
    with pytest.raises(mesh_mod.MeshError, match="don't fit"):
        mesh_mod.arrange([cube] * 40)
    assert mesh_mod.arrange([]) == []


def test_project_3mf_has_one_object_per_model(tmp_path):
    p = tmp_path / "cube.stl"
    _write_cube_stl(p)
    placed = mesh_mod.arrange([mesh_mod.load(str(p))] * 3)
    out = tmp_path / "p.3mf"
    mesh_mod.write_project_3mf(placed, {"layer_height": "0.2"}, str(out))
    model = zipfile.ZipFile(out).read("3D/3dmodel.model").decode()
    assert model.count("<object ") == 3 and model.count("<item ") == 3


@pytest.mark.skipif(not _have_orca(), reason="OrcaSlicer not installed")
def test_multi_object_plate_slices_together(tmp_path):
    from openbot.slicing import slice_models
    stl = tmp_path / "cube.stl"
    _write_cube_stl(stl)
    out = tmp_path / "plate.makerbot"
    r = slice_models([str(stl)] * 3, str(out), PrintSettings(quality="draft"))
    width = r.bbox["x_max"] - r.bbox["x_min"]
    assert width > 3 * 10 + 2 * 5 - 1        # three 10 mm cubes, 5 mm apart
    assert check_print_file(str(out)) == []
