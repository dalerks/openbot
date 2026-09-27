"""Render the small PNG previews a .makerbot carries (55x40, 110x80, 320x200).

The printer's LCD and MakerBot's own tools show these. OrcaSlicer's CLI doesn't
emit thumbnails, so we draw a top-down view of the extrusion paths ourselves,
shaded by height. Pure stdlib (zlib + struct), no imaging library needed.
"""

import struct
import zlib

SIZES = [(55, 40), (110, 80), (320, 200)]
BACKGROUND = (38, 38, 42)
LOW = (40, 150, 200)      # first layers
HIGH = (240, 245, 250)    # top layers


def _png(width, height, rows):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def render(segments, bbox, width, height, margin=0.1):
    """segments: [(x0, y0, x1, y1, z)] in mm. Returns PNG bytes."""
    rows = [bytearray(BACKGROUND * width) for _ in range(height)]
    if segments:
        w_mm = max(bbox["x_max"] - bbox["x_min"], 1e-3)
        h_mm = max(bbox["y_max"] - bbox["y_min"], 1e-3)
        scale = min(width * (1 - 2 * margin) / w_mm, height * (1 - 2 * margin) / h_mm)
        ox = (width - w_mm * scale) / 2
        oy = (height - h_mm * scale) / 2
        z0, z1 = bbox["z_min"], max(bbox["z_max"], bbox["z_min"] + 1e-3)

        def px(xm, ym):
            return (int(ox + (xm - bbox["x_min"]) * scale),
                    int(height - 1 - (oy + (ym - bbox["y_min"]) * scale)))

        for x0, y0, x1, y1, z in sorted(segments, key=lambda s: s[4]):
            t = (z - z0) / (z1 - z0)
            color = bytes(int(LOW[i] + (HIGH[i] - LOW[i]) * t) for i in range(3))
            (ax, ay), (bx, by) = px(x0, y0), px(x1, y1)
            steps = max(abs(bx - ax), abs(by - ay), 1)
            for s in range(steps + 1):
                cx = ax + (bx - ax) * s // steps
                cy = ay + (by - ay) * s // steps
                if 0 <= cx < width and 0 <= cy < height:
                    rows[cy][cx * 3:cx * 3 + 3] = color
    return _png(width, height, rows)


def render_all(segments, bbox):
    return {f"thumbnail_{w}x{h}.png": render(segments, bbox, w, h) for w, h in SIZES}
