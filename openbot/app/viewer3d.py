"""3D build-plate view: the Replicator+ plate plus the loaded model. Drag to orbit, scroll to zoom.

Plain OpenGL 2.1 / GLSL 1.20 through Qt so it runs on every Mac that runs macOS 12,
including Intel integrated graphics.
"""

import array
import math

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QMatrix4x4, QSurfaceFormat, QVector3D
from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from ..machines import REPLICATOR

GL_COLOR_BUFFER_BIT = 0x4000
GL_DEPTH_BUFFER_BIT = 0x0100
GL_DEPTH_TEST = 0x0B71
GL_BLEND = 0x0BE2
GL_SRC_ALPHA = 0x0302
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_TRIANGLES = 0x0004
GL_LINES = 0x0001
GL_FLOAT = 0x1406

VERT = """
#version 120
attribute vec3 position;
attribute vec3 normal;
uniform mat4 mvp;
uniform vec3 offset;
uniform vec3 color;
uniform float lit;
varying vec3 v_color;
void main() {
    gl_Position = mvp * vec4(position + offset, 1.0);
    vec3 light = normalize(vec3(0.4, -0.6, 0.8));
    float d = lit > 0.5 ? 0.35 + 0.65 * abs(dot(normalize(normal), light)) : 1.0;
    v_color = color * d;
}
"""
FRAG = """
#version 120
varying vec3 v_color;
uniform float alpha;
void main() { gl_FragColor = vec4(v_color, alpha); }
"""


def _pack(values):
    return array.array("f", values).tobytes()


def _triangles_bytes(mesh):
    """Flat-shaded triangle soup: position + face normal per vertex."""
    data = []
    v = mesh.vertices
    for a, b, c in mesh.triangles:
        p0, p1, p2 = v[a], v[b], v[c]
        ux, uy, uz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
        wx, wy, wz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
        n = (uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx)
        for p in (p0, p1, p2):
            data.extend(p)
            data.extend(n)
    return _pack(data), len(data) // 6


class PlateView(QOpenGLWidget):
    """Click an object to select it and drag it across the plate; drag empty space to
    orbit, scroll to zoom, double-click empty space to reset the view."""
    object_picked = Signal(int)
    object_moved = Signal(int, float, float)     # index, new centre x, y (mm)

    def __init__(self, parent=None):
        super().__init__(parent)
        fmt = QSurfaceFormat()
        fmt.setDepthBufferSize(24)
        fmt.setSamples(4)
        self.setFormat(fmt)
        self.setMinimumSize(420, 320)
        self._bed = REPLICATOR.bed
        self._bed_dirty = True
        self._grid_count = 0
        self._yaw, self._pitch, self._dist = -30.0, 55.0, self._home_distance()
        self._last = None
        self._objects = []          # [(bytes, vertex_count)]
        self._footprints = []       # [(x0, y0, x1, y1)] per object, for picking
        self._drag = None           # {"index", "start", "offset"} while dragging
        self._selected = None
        self._dirty = False
        self._program = None
        self._mesh_bufs = []
        self._plate_buf = None
        self._grid_buf = None
        self.model_color = (0.45, 0.62, 0.85)
        self.selected_color = (1.0, 0.55, 0.15)
        self.message = ""

    # ------------------------------------------------------------ model

    def set_mesh(self, mesh):
        """Show a single mesh (or nothing)."""
        self.set_meshes([mesh] if mesh is not None else [])

    def set_meshes(self, meshes, selected=None):
        """meshes: openbot.slicing.mesh.Mesh objects already placed on the bed."""
        self._objects = [_triangles_bytes(m) for m in meshes]
        self._footprints = []
        for m in meshes:
            (x0, y0, _), (x1, y1, _) = m.bounds()
            self._footprints.append((x0, y0, x1, y1))
        self._drag = None
        self._selected = selected
        self._dirty = True
        self.update()

    def set_selected(self, index):
        self._selected = index
        self.update()

    # ------------------------------------------------------------ GL

    def initializeGL(self):
        self._program = QOpenGLShaderProgram(self)
        self._program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, VERT)
        self._program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, FRAG)
        self._program.bindAttributeLocation("position", 0)
        self._program.bindAttributeLocation("normal", 1)
        if not self._program.link():
            self.message = "3D preview unavailable: " + self._program.log()
            self._program = None
            return
        self._bed_dirty = True
        self._dirty = True

    def set_bed(self, bed):
        """Show a different printer's build plate (a machines.Bed)."""
        self._bed = bed
        self._bed_dirty = True
        self._dist = self._home_distance()
        self.update()

    def _home_distance(self):
        return max(self._bed.width, self._bed.depth) * 1.9

    def _build_bed(self):
        """Plate: two triangles; grid: 10 mm lines; frame: build volume edges."""
        b = self._bed
        for buf in (self._plate_buf, self._grid_buf):
            if buf is not None:
                buf.destroy()
        corners = ((b.x_min, b.y_min), (b.x_max, b.y_min), (b.x_max, b.y_max),
                   (b.x_min, b.y_max))
        plate = []
        for x, y in (corners[0], corners[1], corners[2], corners[0], corners[2], corners[3]):
            plate += [x, y, -0.05, 0, 0, 1]
        self._plate_buf = self._make_buffer(_pack(plate))
        grid = []
        gx = b.x_min
        while gx <= b.x_max + 1e-6:
            grid += [gx, b.y_min, 0, 0, 0, 1, gx, b.y_max, 0, 0, 0, 1]
            gx += 10
        gy = b.y_min
        while gy <= b.y_max + 1e-6:
            grid += [b.x_min, gy, 0, 0, 0, 1, b.x_max, gy, 0, 0, 0, 1]
            gy += 10
        for x, y in corners:
            grid += [x, y, 0, 0, 0, 1, x, y, b.z_max, 0, 0, 1]
        for i in range(4):
            (x0, y0), (x1, y1) = corners[i], corners[(i + 1) % 4]
            grid += [x0, y0, b.z_max, 0, 0, 1, x1, y1, b.z_max, 0, 0, 1]
        self._grid_count = len(grid) // 6
        self._grid_buf = self._make_buffer(_pack(grid))
        self._bed_dirty = False

    def _make_buffer(self, data):
        buf = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        buf.create()
        buf.bind()
        buf.allocate(data, len(data))
        buf.release()
        return buf

    def _draw(self, f, buf, mode, count, color, lit, alpha=1.0, offset=(0.0, 0.0, 0.0)):
        if buf is None or count == 0:
            return
        p = self._program
        buf.bind()
        p.enableAttributeArray(0)
        p.enableAttributeArray(1)
        p.setAttributeBuffer(0, GL_FLOAT, 0, 3, 24)
        p.setAttributeBuffer(1, GL_FLOAT, 12, 3, 24)
        p.setUniformValue("color", QVector3D(*color))
        p.setUniformValue("offset", QVector3D(*offset))
        p.setUniformValue1f("lit", 1.0 if lit else 0.0)
        p.setUniformValue1f("alpha", alpha)
        f.glDrawArrays(mode, 0, count)
        buf.release()

    def paintGL(self):
        f = self.context().functions()
        dark = self.palette().window().color().lightness() < 128
        bg = (0.13, 0.13, 0.15) if dark else (0.93, 0.93, 0.95)
        f.glClearColor(*bg, 1.0)
        f.glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        if self._program is None:
            return
        if self._bed_dirty:
            self._build_bed()
        if self._dirty:
            for buf, _ in self._mesh_bufs:
                buf.destroy()
            self._mesh_bufs = [(self._make_buffer(data), count)
                               for data, count in self._objects if count]
            self._dirty = False
        f.glEnable(GL_DEPTH_TEST)
        f.glEnable(GL_BLEND)
        f.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        self._program.bind()
        self._program.setUniformValue("mvp", self._mvp())
        plate = (0.22, 0.22, 0.25) if dark else (0.78, 0.78, 0.82)
        grid = (0.35, 0.35, 0.4) if dark else (0.62, 0.62, 0.68)
        self._draw(f, self._plate_buf, GL_TRIANGLES, 6, plate, False)
        self._draw(f, self._grid_buf, GL_LINES, self._grid_count, grid, False, 0.8)
        single = len(self._mesh_bufs) == 1
        for i, (buf, count) in enumerate(self._mesh_bufs):
            color = self.selected_color if (single or i == self._selected) else self.model_color
            off = (0.0, 0.0, 0.0)
            if self._drag and self._drag["index"] == i:
                off = (self._drag["offset"][0], self._drag["offset"][1], 0.0)
            self._draw(f, buf, GL_TRIANGLES, count, color, True, offset=off)
        self._program.release()

    def _mvp(self):
        proj = QMatrix4x4()
        aspect = self.width() / max(self.height(), 1)
        proj.perspective(35.0, aspect, 5.0, 5000.0)
        view = QMatrix4x4()
        yaw, pitch = math.radians(self._yaw), math.radians(self._pitch)
        eye = QVector3D(self._dist * math.sin(pitch) * math.sin(yaw),
                        -self._dist * math.sin(pitch) * math.cos(yaw),
                        self._dist * math.cos(pitch))
        cx, cy = self._bed.center
        target = QVector3D(cx, cy, 30)
        view.lookAt(eye + target, target, QVector3D(0, 0, 1))
        return proj * view

    # ------------------------------------------------------------ input

    def _plate_point(self, pos):
        """Where the mouse ray meets the plate (z = 0), in mm; None if it misses."""
        inv, ok = self._mvp().inverted()
        if not ok:
            return None
        nx = 2.0 * pos.x() / max(self.width(), 1) - 1.0
        ny = 1.0 - 2.0 * pos.y() / max(self.height(), 1)
        near = inv.map(QVector3D(nx, ny, -1.0))
        far = inv.map(QVector3D(nx, ny, 1.0))
        dz = far.z() - near.z()
        if abs(dz) < 1e-9:
            return None
        t = -near.z() / dz
        if t < 0:
            return None
        return (near.x() + (far.x() - near.x()) * t, near.y() + (far.y() - near.y()) * t)

    def _object_at(self, point):
        if point is None:
            return None
        x, y = point
        hits = [i for i, (x0, y0, x1, y1) in enumerate(self._footprints)
                if x0 <= x <= x1 and y0 <= y <= y1]
        if self._selected in hits:
            return self._selected
        return hits[-1] if hits else None

    def mousePressEvent(self, e):
        self._last = e.position()
        point = self._plate_point(e.position())
        hit = self._object_at(point)
        if hit is not None and e.button() == Qt.MouseButton.LeftButton:
            self._selected = hit
            self.object_picked.emit(hit)
            self._drag = {"index": hit, "start": point, "offset": (0.0, 0.0), "moved": False}
            self.update()

    def mouseMoveEvent(self, e):
        if self._last is None:
            return
        if self._drag:
            point = self._plate_point(e.position())
            if point is not None:
                sx, sy = self._drag["start"]
                self._drag["offset"] = (point[0] - sx, point[1] - sy)
                self._drag["moved"] = True
                self.update()
            return
        d: QPointF = e.position() - self._last
        self._last = e.position()
        self._yaw += d.x() * 0.4
        self._pitch = max(5.0, min(89.0, self._pitch - d.y() * 0.4))
        self.update()

    def mouseReleaseEvent(self, e):
        self._last = None
        drag, self._drag = self._drag, None
        if drag and drag["moved"]:
            x0, y0, x1, y1 = self._footprints[drag["index"]]
            dx, dy = drag["offset"]
            self.object_moved.emit(drag["index"], (x0 + x1) / 2 + dx, (y0 + y1) / 2 + dy)
        self.update()

    def wheelEvent(self, e):
        self._dist = max(80.0, min(1500.0, self._dist * (0.9 if e.angleDelta().y() > 0 else 1.1)))
        self.update()

    def mouseDoubleClickEvent(self, e):
        if self._object_at(self._plate_point(e.position())) is not None:
            return
        self._yaw, self._pitch, self._dist = -30.0, 55.0, self._home_distance()
        self.update()
