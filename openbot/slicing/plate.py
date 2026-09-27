"""The build plate: objects with their own position, rotation and scale.

Objects start auto-arranged. Moving one by hand pins it; new objects then go
to the first free spot instead of re-arranging everything, and "Arrange" lays
everything out again.
"""

import math
from dataclasses import dataclass, replace

from ..machines import REPLICATOR
from .mesh import Mesh, MeshError, arrange

SPACING = 5.0


@dataclass
class PlateObject:
    name: str
    mesh: Mesh                 # as loaded from the file
    scale: float = 1.0         # uniform
    rx: float = 0.0            # degrees, applied X then Y then Z
    ry: float = 0.0
    rz: float = 0.0
    x: float | None = None     # centre on the bed; None until placed
    y: float | None = None

    def shaped(self):
        return self.mesh.transformed(self.scale, self.rx, self.ry, self.rz)

    def placed(self, bed):
        return self.shaped().placed_on_bed(self.x, self.y, bed)

    @property
    def size(self):
        return self.shaped().size

    def copy(self, **changes):
        return replace(self, **changes)


def footprint(mesh):
    (x0, y0, _), (x1, y1, _) = mesh.bounds()
    return (x0, y0, x1, y1)


def overlap(a, b, gap=0.0):
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or
                a[3] + gap <= b[1] or b[3] + gap <= a[1])


def find_free_spot(footprints, width, depth, bed, spacing=SPACING, step=5.0):
    """Centre (x, y) for a width×depth footprint that clears all others, or None."""
    cx, cy = bed.center
    candidates = []
    y = bed.y_min + depth / 2
    while y <= bed.y_max - depth / 2 + 1e-6:
        x = bed.x_min + width / 2
        while x <= bed.x_max - width / 2 + 1e-6:
            candidates.append((math.hypot(x - cx, y - cy), x, y))
            x += step
        y += step
    for _, x, y in sorted(candidates):
        fp = (x - width / 2, y - depth / 2, x + width / 2, y + depth / 2)
        if not any(overlap(fp, other, spacing) for other in footprints):
            return x, y
    return None


class Plate:
    def __init__(self, bed=None):
        self.bed = bed or REPLICATOR.bed
        self.objects: list[PlateObject] = []
        self.manual = False            # has the user positioned anything by hand?

    def __len__(self):
        return len(self.objects)

    def placed(self):
        return [o.placed(self.bed) for o in self.objects]

    # ------------------------------------------------------------ editing

    def add(self, name, mesh):
        """Add an object; raises MeshError (and leaves the plate unchanged) if it can't fit."""
        obj = PlateObject(name, mesh)
        obj.shaped().check_fits(self.bed)
        if not self.manual:
            self._arrange_with(self.objects + [obj])
        else:
            self._place_in_free_spot(obj)
        self.objects.append(obj)
        return obj

    def duplicate(self, index):
        src = self.objects[index]
        copy = src.copy(x=None, y=None)
        if self.manual:
            self._place_in_free_spot(copy)
        else:
            self._arrange_with(self.objects + [copy])
        self.objects.insert(index + 1, copy)
        return copy

    def remove(self, indexes):
        for i in sorted(set(indexes), reverse=True):
            del self.objects[i]
        if not self.manual:
            self.arrange()

    def clear(self):
        self.objects.clear()
        self.manual = False

    def arrange(self):
        self._arrange_with(self.objects)
        self.manual = False

    def move(self, index, x, y):
        obj = self.objects[index]
        w, d, _ = obj.size
        b = self.bed
        obj.x = min(max(x, b.x_min + w / 2), b.x_max - w / 2)
        obj.y = min(max(y, b.y_min + d / 2), b.y_max - d / 2)
        self.manual = True

    def transform(self, index, *, scale=None, rx=None, ry=None, rz=None):
        """Change rotation/scale; raises MeshError (and reverts) if it no longer fits."""
        obj = self.objects[index]
        before = obj.copy()
        if scale is not None:
            if not 0.01 <= scale <= 20:
                raise MeshError("scale must be between 1% and 2000%")
            obj.scale = scale
        if rx is not None:
            obj.rx = rx % 360
        if ry is not None:
            obj.ry = ry % 360
        if rz is not None:
            obj.rz = ((rz + 180) % 360) - 180
        try:
            obj.shaped().check_fits(self.bed)
        except MeshError:
            self.objects[index] = before
            raise
        if not self.manual:
            self.arrange()
        else:
            self.move(index, obj.x if obj.x is not None else self.bed.center[0],
                      obj.y if obj.y is not None else self.bed.center[1])

    # ------------------------------------------------------------ checks

    def problems(self):
        """Human-readable reasons the plate can't be printed as-is (overlaps are allowed
        by the slicer but almost always a mistake, so they are reported too)."""
        out = []
        placed = self.placed()
        fps = [footprint(m) for m in placed]
        b = self.bed
        for o, fp in zip(self.objects, fps):
            if fp[0] < b.x_min - 1e-6 or fp[2] > b.x_max + 1e-6 or \
                    fp[1] < b.y_min - 1e-6 or fp[3] > b.y_max + 1e-6:
                out.append(f"{o.name} hangs over the edge of the plate")
        for i in range(len(fps)):
            for j in range(i + 1, len(fps)):
                if overlap(fps[i], fps[j]):
                    out.append(f"{self.objects[i].name} overlaps {self.objects[j].name}")
        return out

    def set_bed(self, bed):
        """Switch printer; re-arranges. Raises MeshError (plate unchanged) if it won't fit."""
        old = self.bed
        self.bed = bed
        try:
            for o in self.objects:
                o.shaped().check_fits(bed)
            self.arrange()
        except MeshError:
            self.bed = old
            self.arrange()
            raise

    # ------------------------------------------------------------ internals

    def _arrange_with(self, objs):
        shaped = [o.shaped() for o in objs]
        placed = arrange(shaped, SPACING, self.bed)
        for o, m in zip(objs, placed):
            (x0, y0, _), (x1, y1, _) = m.bounds()
            o.x, o.y = (x0 + x1) / 2, (y0 + y1) / 2

    def _place_in_free_spot(self, obj):
        w, d, _ = obj.size
        spot = find_free_spot([footprint(m) for m in self.placed()], w, d, self.bed)
        if spot is None:
            raise MeshError(f"no free space left on the plate for {obj.name}; remove "
                            "something or click Arrange")
        obj.x, obj.y = spot
