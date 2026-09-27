import pytest

from openbot import machines
from openbot.slicing.mesh import Mesh, MeshError
from openbot.slicing.plate import Plate, find_free_spot, footprint, overlap


def box(w, d, h):
    v = [(x * w, y * d, z * h) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
    f = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
         (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return Mesh(v, f)


def test_transform_scale_and_rotate():
    m = box(10, 20, 30)
    assert m.transformed(2.0).size == pytest.approx((20, 40, 60))
    assert m.transformed(rz=90).size == pytest.approx((20, 10, 30))
    assert m.transformed(rx=90).size == pytest.approx((10, 30, 20))     # lies on its side
    assert m.transformed(ry=90).size == pytest.approx((30, 20, 10))
    assert m.transformed() is m


def test_auto_arrange_then_manual_move_pins_objects():
    ender = machines.get("ender3_pro").bed
    plate = Plate(ender)
    plate.add("a", box(20, 20, 10))
    plate.add("b", box(20, 20, 10))
    assert not plate.problems()
    plate.move(0, 30, 30)
    assert plate.manual
    assert (plate.objects[0].x, plate.objects[0].y) == (30, 30)
    plate.add("c", box(20, 20, 10))          # goes to a free spot, others stay put
    assert (plate.objects[0].x, plate.objects[0].y) == (30, 30)
    assert not plate.problems()
    plate.arrange()
    assert not plate.manual and not plate.problems()


def test_move_is_clamped_to_the_bed_and_overlaps_are_reported():
    ender = machines.get("ender3_pro").bed
    plate = Plate(ender)
    plate.add("a", box(20, 20, 10))
    plate.add("b", box(20, 20, 10))
    plate.move(0, -500, 900)
    fp = footprint(plate.placed()[0])
    assert fp[0] == pytest.approx(0) and fp[3] == pytest.approx(220)
    plate.move(1, plate.objects[0].x + 5, plate.objects[0].y)
    assert any("overlaps" in p for p in plate.problems())


def test_transform_reverts_when_too_big():
    plate = Plate(machines.get("ender3_pro").bed)
    plate.add("a", box(100, 100, 100))
    with pytest.raises(MeshError):
        plate.transform(0, scale=3.0)
    assert plate.objects[0].scale == 1.0
    plate.transform(0, rz=45)
    assert plate.objects[0].size[0] == pytest.approx(141.42, abs=0.1)


def test_duplicate_and_remove():
    plate = Plate()
    plate.add("a", box(20, 20, 10))
    plate.duplicate(0)
    assert [o.name for o in plate.objects] == ["a", "a"]
    assert not plate.problems()
    plate.remove([0])
    assert len(plate) == 1


def test_full_plate_refuses_new_object():
    plate = Plate(machines.get("ender3_pro").bed)
    plate.add("big", box(200, 200, 10))
    plate.move(0, 110, 110)
    with pytest.raises(MeshError, match="no free space"):
        plate.add("more", box(50, 50, 10))
    assert len(plate) == 1


def test_switching_printer_rearranges_or_refuses():
    plate = Plate()                                    # Replicator+: 295 x 195
    plate.add("wide", box(250, 50, 10))
    with pytest.raises(MeshError):
        plate.set_bed(machines.get("ender3_pro").bed)  # 220 mm wide: doesn't fit
    assert plate.bed == machines.REPLICATOR.bed
    plate.clear()
    plate.add("small", box(20, 20, 10))
    plate.set_bed(machines.get("ender3_pro").bed)
    assert plate.objects[0].x == pytest.approx(110)


def test_find_free_spot_and_overlap():
    bed = machines.get("ender3_pro").bed
    assert overlap((0, 0, 10, 10), (5, 5, 15, 15))
    assert not overlap((0, 0, 10, 10), (10, 0, 20, 10))
    assert overlap((0, 0, 10, 10), (12, 0, 20, 10), gap=5)
    x, y = find_free_spot([(100, 100, 120, 120)], 20, 20, bed)
    assert not overlap((x - 10, y - 10, x + 10, y + 10), (100, 100, 120, 120), 5)
