"""Still pictures of a finned mesh, for files the interactive preview will not draw.

The browser budget is 200k triangles. A finned Grinch is about a million. Sending that
JSON freezes the tab, and Orca headless does not emit a thumbnail. This module paints
four perspective/axonometric screenshots from the four corners of the bed (plus legacy
orthographic views) instead: the part as one pixel per vertex, the support fins as
edges so thin walls stay visible, and a subtle bed base outline. The slice still uses the
original file.
"""
from __future__ import annotations

import math
import re
import struct
import zipfile
import zlib
from pathlib import Path

SHOT_MAX_BYTES = 150 * 1024 * 1024
"""Above the interactive preview cap, still small enough to paint. Matches the upload limit."""

SIZE = 900
_BG = (18, 22, 28)
_PART = (148, 163, 184)
_FINS = (34, 197, 94)
_BED_BORDER = (45, 55, 72)
_V = re.compile(br'x="([^"]+)" y="([^"]+)" z="([^"]+)"')
_T = re.compile(br'v1="(\d+)" v2="(\d+)" v3="(\d+)"')
_OBJ = re.compile(br'<object\b[^>]*\bname="([^"]*)"')

_THETA = math.radians(30)
_C_TH = math.cos(_THETA)
_S_TH = math.sin(_THETA)
_INV_SQRT2 = 1.0 / math.sqrt(2.0)

CORNERS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "front_left":  ((_INV_SQRT2, -_INV_SQRT2, 0.0), (-_INV_SQRT2 * _S_TH, -_INV_SQRT2 * _S_TH, _C_TH)),
    "front_right": ((_INV_SQRT2,  _INV_SQRT2, 0.0), ( _INV_SQRT2 * _S_TH, -_INV_SQRT2 * _S_TH, _C_TH)),
    "rear_right":  ((-_INV_SQRT2,  _INV_SQRT2, 0.0), ( _INV_SQRT2 * _S_TH,  _INV_SQRT2 * _S_TH, _C_TH)),
    "rear_left":   ((-_INV_SQRT2, -_INV_SQRT2, 0.0), (-_INV_SQRT2 * _S_TH,  _INV_SQRT2 * _S_TH, _C_TH)),
    "front":       ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "side":        ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
}


class ShotError(RuntimeError):
    """The still picture cannot be painted; the message names the file and the reason."""


def render(path: Path, dest: Path) -> dict[str, Path]:
    """Write corner and orthographic PNGs under ``dest``. Returns those paths."""
    if path.stat().st_size > SHOT_MAX_BYTES:
        raise ShotError(f"{path.name} is {path.stat().st_size} bytes; the picture cap is {SHOT_MAX_BYTES}")
    part, supports, support_tris = _meshes(path)
    if part is None and supports is None:
        raise ShotError(f"{path.name} has no mesh to photograph")
    dest.mkdir(parents=True, exist_ok=True)
    out = {
        "front_left": dest / "front_left.png",
        "front_right": dest / "front_right.png",
        "rear_right": dest / "rear_right.png",
        "rear_left": dest / "rear_left.png",
        "front": dest / "front.png",
        "side": dest / "side.png",
    }
    for view, (r_vec, u_vec) in CORNERS.items():
        _paint(part, supports, support_tris, r_vec, u_vec, out[view])

    # Alias back_right / back_left to rear_right / rear_left for compatibility
    out["back_right"] = dest / "back_right.png"
    out["back_left"] = dest / "back_left.png"
    if out["rear_right"].is_file():
        out["back_right"].write_bytes(out["rear_right"].read_bytes())
    if out["rear_left"].is_file():
        out["back_left"].write_bytes(out["rear_left"].read_bytes())

    return out


def _meshes(path: Path):
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("3D/3dmodel.model")
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ShotError(f"{path.name} is not a readable 3MF") from exc
    part = supports = None
    support_tris: list[tuple[int, int, int]] = []
    pos = 0
    while True:
        start = xml.find(b"<object ", pos)
        if start < 0:
            break
        end = xml.find(b"</object>", start)
        if end < 0:
            break
        chunk = xml[start:end]
        name = b""
        named = _OBJ.search(chunk)
        if named:
            name = named.group(1)
        vstart, vend = chunk.find(b"<vertices>"), chunk.find(b"</vertices>")
        tstart, tend = chunk.find(b"<triangles>"), chunk.find(b"</triangles>")
        pos = end + 9
        if vstart < 0 or tstart < 0:
            continue
        verts = [(float(x), float(y), float(z)) for x, y, z in _V.findall(chunk[vstart:vend])]
        if not verts:
            continue
        label = name.decode("utf-8", "replace").lower()
        if "support" in label or "fin" in label:
            supports = verts
            support_tris = [(int(a), int(b), int(c)) for a, b, c in _T.findall(chunk[tstart:tend])]
        elif part is None:
            part = verts
    return part, supports, support_tris


def _paint(part, supports, support_tris,
           proj_r: tuple[float, float, float] | int,
           proj_u: tuple[float, float, float] | int,
           path: Path) -> None:
    if isinstance(proj_r, int):
        r = (1.0 if proj_r == 0 else 0.0, 1.0 if proj_r == 1 else 0.0, 1.0 if proj_r == 2 else 0.0)
    else:
        r = proj_r

    if isinstance(proj_u, int):
        u_v = (1.0 if proj_u == 0 else 0.0, 1.0 if proj_u == 1 else 0.0, 1.0 if proj_u == 2 else 0.0)
    else:
        u_v = proj_u

    img = bytearray(bytes(_BG) * (SIZE * SIZE))
    clouds = []
    if part:
        clouds.append(part)
    if supports:
        clouds.append(supports)

    all_pts: list[tuple[float, float, float]] = []
    for cloud in clouds:
        all_pts.extend(cloud)

    if not all_pts:
        path.write_bytes(_png(bytes(img), SIZE, SIZE))
        return

    min_x = min(p[0] for p in all_pts)
    max_x = max(p[0] for p in all_pts)
    min_y = min(p[1] for p in all_pts)
    max_y = max(p[1] for p in all_pts)
    min_z = min(p[2] for p in all_pts)

    margin = max(4.0, 0.08 * max(max_x - min_x, max_y - min_y, 1.0))
    bed_pts = [
        (min_x - margin, min_y - margin, min_z),
        (max_x + margin, min_y - margin, min_z),
        (max_x + margin, max_y + margin, min_z),
        (min_x - margin, max_y + margin, min_z),
    ]

    us, vs = [], []
    for cloud in clouds:
        for p in cloud:
            us.append(r[0] * p[0] + r[1] * p[1] + r[2] * p[2])
            vs.append(u_v[0] * p[0] + u_v[1] * p[1] + u_v[2] * p[2])

    for p in bed_pts:
        us.append(r[0] * p[0] + r[1] * p[1] + r[2] * p[2])
        vs.append(u_v[0] * p[0] + u_v[1] * p[1] + u_v[2] * p[2])

    span_u = max(us) - min(us) or 1.0
    span_v = max(vs) - min(vs) or 1.0
    span = max(span_u, span_v) * 1.06
    mid_u = (max(us) + min(us)) / 2
    mid_v = (max(vs) + min(vs)) / 2

    def proj(p: tuple[float, float, float]) -> tuple[float, float]:
        return (r[0] * p[0] + r[1] * p[1] + r[2] * p[2],
                u_v[0] * p[0] + u_v[1] * p[1] + u_v[2] * p[2])

    def px(u: float, v: float) -> tuple[int, int]:
        x = int(round(((u - mid_u) / span + 0.5) * (SIZE - 1)))
        y = int(round((0.5 - (v - mid_v) / span) * (SIZE - 1)))
        return min(SIZE - 1, max(0, x)), min(SIZE - 1, max(0, y))

    def put(x: int, y: int, colour: tuple[int, int, int]) -> None:
        i = (y * SIZE + x) * 3
        img[i:i + 3] = bytes(colour)

    def draw_line(p0: tuple[int, int], p1: tuple[int, int], colour: tuple[int, int, int]) -> None:
        x0, y0 = p0
        x1, y1 = p1
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for step in range(steps + 1):
            t = step / steps
            put(int(x0 + (x1 - x0) * t), int(y0 + (y1 - y0) * t), colour)

    # 1. Subtle bed outline
    bed_px = [px(*proj(p)) for p in bed_pts]
    for i in range(4):
        draw_line(bed_px[i], bed_px[(i + 1) % 4], _BED_BORDER)

    # 2. Part points
    if part:
        for p in part:
            put(*px(*proj(p)), _PART)

    # 3. Support fin edges
    if supports and support_tris:
        for a, b, c in support_tris:
            pts = [px(*proj(supports[i])) for i in (a, b, c)]
            for (x0, y0), (x1, y1) in ((pts[0], pts[1]), (pts[1], pts[2]), (pts[2], pts[0])):
                steps = max(abs(x1 - x0), abs(y1 - y0), 1)
                for step in range(steps + 1):
                    t = step / steps
                    put(int(x0 + (x1 - x0) * t), int(y0 + (y1 - y0) * t), _FINS)

    path.write_bytes(_png(bytes(img), SIZE, SIZE))


def _png(rgb: bytes, width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b"")
