"""Load STL/OBJ meshes and wrap them, with settings, into a 3MF project for OrcaSlicer.

OrcaSlicer's CLI rejects separately loaded printer/process/filament presets in
2.4.x ("process not compatible with printer"), so we slice a self-contained
3MF with the settings embedded, the way queue3d (MIT) found works.
"""

import json
import struct
import zipfile
from dataclasses import dataclass

from ..machines import REPLICATOR


class MeshError(ValueError):
    pass


@dataclass
class Mesh:
    vertices: list      # [(x, y, z)]
    triangles: list     # [(i, j, k)]

    def bounds(self):
        xs, ys, zs = zip(*self.vertices)
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    @property
    def size(self):
        lo, hi = self.bounds()
        return tuple(h - l for l, h in zip(lo, hi))

    def transformed(self, scale=1.0, rx=0.0, ry=0.0, rz=0.0):
        """Scaled (uniformly) then rotated about X, Y, Z (degrees). Not yet placed."""
        if scale == 1.0 and not (rx % 360 or ry % 360 or rz % 360):
            return self
        rot = _rotation(rx, ry, rz)
        verts = []
        for x, y, z in self.vertices:
            x, y, z = x * scale, y * scale, z * scale
            verts.append((rot[0][0] * x + rot[0][1] * y + rot[0][2] * z,
                          rot[1][0] * x + rot[1][1] * y + rot[1][2] * z,
                          rot[2][0] * x + rot[2][1] * y + rot[2][2] * z))
        return Mesh(verts, self.triangles)

    def placed_on_bed(self, x=None, y=None, bed=None):
        """Copy centred at (x, y) (default: the bed centre), resting on z=0."""
        cx, cy = (bed or REPLICATOR.bed).center
        x = cx if x is None else x
        y = cy if y is None else y
        (x0, y0, z0), (x1, y1, _) = self.bounds()
        dx, dy, dz = x - (x0 + x1) / 2, y - (y0 + y1) / 2, -z0
        return Mesh([(vx + dx, vy + dy, vz + dz) for vx, vy, vz in self.vertices],
                    self.triangles)

    def check_fits(self, bed=None, printer_name="this printer"):
        bed = bed or REPLICATOR.bed
        if bed is REPLICATOR.bed and printer_name == "this printer":
            printer_name = "the Replicator+"
        sx, sy, sz = self.size
        if sx > bed.width or sy > bed.depth or sz > bed.z_max:
            raise MeshError(
                f"model is {sx:.0f} × {sy:.0f} × {sz:.0f} mm; {printer_name} builds up to "
                f"{bed.width:.0f} × {bed.depth:.0f} × {bed.z_max:.0f} mm. Scale or rotate it.")


def _rotation(rx, ry, rz):
    """3x3 matrix for rotating about X, then Y, then Z (degrees)."""
    import math
    ax, ay, az = (math.radians(a) for a in (rx, ry, rz))
    cx, sx, cy, sy, cz, sz = (math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay),
                              math.cos(az), math.sin(az))
    rxm = ((1, 0, 0), (0, cx, -sx), (0, sx, cx))
    rym = ((cy, 0, sy), (0, 1, 0), (-sy, 0, cy))
    rzm = ((cz, -sz, 0), (sz, cz, 0), (0, 0, 1))

    def mul(a, b):
        return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
                     for i in range(3))
    return mul(rzm, mul(rym, rxm))


def load(path):
    lower = path.lower()
    if lower.endswith(".stl"):
        return _load_stl(path)
    if lower.endswith(".obj"):
        return _load_obj(path)
    raise MeshError("supported model formats: .stl, .obj")


class _Dedup:
    def __init__(self):
        self.index = {}
        self.vertices = []

    def __call__(self, v):
        i = self.index.get(v)
        if i is None:
            i = self.index[v] = len(self.vertices)
            self.vertices.append(v)
        return i


def _load_stl(path):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 84:
        raise MeshError("file is too small to be an STL")
    count = struct.unpack_from("<I", data, 80)[0]
    dedup = _Dedup()
    tris = []
    if 84 + count * 50 == len(data):                     # binary
        for rec in struct.iter_unpack("<12fH", data[84:84 + count * 50]):
            tris.append((dedup(rec[3:6]), dedup(rec[6:9]), dedup(rec[9:12])))
    else:                                                # ASCII
        tri = []
        for line in data.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if parts[:1] == ["vertex"] and len(parts) >= 4:
                tri.append(dedup(tuple(float(p) for p in parts[1:4])))
                if len(tri) == 3:
                    tris.append(tuple(tri))
                    tri = []
    if not tris:
        raise MeshError("no triangles found in STL")
    return Mesh(dedup.vertices, tris)


def _load_obj(path):
    verts, tris = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "v" and len(parts) >= 4:
                verts.append(tuple(float(p) for p in parts[1:4]))
            elif parts[0] == "f" and len(parts) >= 4:
                idx = [int(p.split("/")[0]) for p in parts[1:]]
                idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
                for k in range(1, len(idx) - 1):               # fan-triangulate polygons
                    tris.append((idx[0], idx[k], idx[k + 1]))
    if not tris:
        raise MeshError("no faces found in OBJ")
    return Mesh(verts, tris)


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""
_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""


def arrange(meshes, spacing=5.0, bed=None):
    """Lay meshes out in rows, centred on the bed, without overlapping.

    Returns new meshes (each resting on z=0). Raises MeshError if they don't fit.
    """
    bed = bed or REPLICATOR.bed
    if not meshes:
        return []
    for m in meshes:
        m.check_fits(bed)
    cx, cy = bed.center
    order = sorted(range(len(meshes)), key=lambda i: -meshes[i].size[1])
    rows, row, row_w = [], [], 0.0
    for i in order:
        w = meshes[i].size[0]
        if row and row_w + spacing + w > bed.width:
            rows.append(row)
            row, row_w = [], 0.0
        row_w += (spacing if row else 0.0) + w
        row.append(i)
    rows.append(row)
    depths = [max(meshes[i].size[1] for i in r) for r in rows]
    total_d = sum(depths) + spacing * (len(rows) - 1)
    if total_d > bed.depth:
        raise MeshError(f"these {len(meshes)} objects don't fit on the plate together "
                        f"(need about {total_d:.0f} mm of the {bed.depth:.0f} mm depth). "
                        "Remove some or print them separately.")
    placed = [None] * len(meshes)
    y = total_d / 2
    for r, depth in zip(rows, depths):
        row_w = sum(meshes[i].size[0] for i in r) + spacing * (len(r) - 1)
        x = -row_w / 2
        for i in r:
            w = meshes[i].size[0]
            placed[i] = meshes[i].placed_on_bed(cx + x + w / 2, cy + y - depth / 2, bed)
            x += w + spacing
        y -= depth + spacing
    return placed


def write_project_3mf(meshes, config, path):
    """Write meshes (already placed on the bed) plus the Orca `config` as one 3MF project."""
    if isinstance(meshes, Mesh):
        meshes = [meshes]
    objects, items = [], []
    for n, mesh in enumerate(meshes, 1):
        verts = "\n".join(f'     <vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>'
                          for x, y, z in mesh.vertices)
        tris = "\n".join(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>'
                         for a, b, c in mesh.triangles)
        objects.append(f"""  <object id="{n}" type="model">
   <mesh>
    <vertices>
{verts}
    </vertices>
    <triangles>
{tris}
    </triangles>
   </mesh>
  </object>""")
        items.append(f'  <item objectid="{n}" printable="1"/>')
    nl = "\n"
    model = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources>
{nl.join(objects)}
 </resources>
 <build>
{nl.join(items)}
 </build>
</model>
"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/project_settings.config", json.dumps(config, indent=1))
