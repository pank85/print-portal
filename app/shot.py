"""Still pictures of a finned mesh, for files the interactive preview will not draw.

The browser budget is 200k triangles. A finned Grinch is about a million. Sending that
JSON freezes the tab, and Orca headless does not emit a thumbnail. This module paints
two orthographic PNGs instead: the part as one pixel per vertex, the support fins as
edges so thin walls stay visible. The slice still uses the original file.
"""
from __future__ import annotations

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
_V = re.compile(br'x="([^"]+)" y="([^"]+)" z="([^"]+)"')
_T = re.compile(br'v1="(\d+)" v2="(\d+)" v3="(\d+)"')
_OBJ = re.compile(br'<object\b[^>]*\bname="([^"]*)"')


class ShotError(RuntimeError):
    """The still picture cannot be painted; the message names the file and the reason."""


def render(path: Path, dest: Path) -> dict[str, Path]:
    """Write ``front.png`` and ``side.png`` under ``dest``. Returns those paths."""
    if path.stat().st_size > SHOT_MAX_BYTES:
        raise ShotError(f"{path.name} is {path.stat().st_size} bytes; the picture cap is {SHOT_MAX_BYTES}")
    part, supports, support_tris = _meshes(path)
    if part is None and supports is None:
        raise ShotError(f"{path.name} has no mesh to photograph")
    dest.mkdir(parents=True, exist_ok=True)
    out = {
        "front": dest / "front.png",
        "side": dest / "side.png",
    }
    _paint(part, supports, support_tris, 0, 2, out["front"])
    _paint(part, supports, support_tris, 1, 2, out["side"])
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


def _paint(part, supports, support_tris, axis_u: int, axis_v: int, path: Path) -> None:
    img = bytearray(bytes(_BG) * (SIZE * SIZE))
    clouds = []
    if part:
        clouds.append(part)
    if supports:
        clouds.append(supports)
    us, vs = [], []
    for cloud in clouds:
        us.extend(p[axis_u] for p in cloud)
        vs.extend(p[axis_v] for p in cloud)
    span_u = max(us) - min(us) or 1.0
    span_v = max(vs) - min(vs) or 1.0
    span = max(span_u, span_v) * 1.06
    mid_u = (max(us) + min(us)) / 2
    mid_v = (max(vs) + min(vs)) / 2

    def px(u: float, v: float) -> tuple[int, int]:
        x = int(round(((u - mid_u) / span + 0.5) * (SIZE - 1)))
        y = int(round((0.5 - (v - mid_v) / span) * (SIZE - 1)))
        return min(SIZE - 1, max(0, x)), min(SIZE - 1, max(0, y))

    def put(x: int, y: int, colour: tuple[int, int, int]) -> None:
        i = (y * SIZE + x) * 3
        img[i:i + 3] = bytes(colour)

    if part:
        for p in part:
            put(*px(p[axis_u], p[axis_v]), _PART)
    if supports and support_tris:
        for a, b, c in support_tris:
            pts = [px(supports[i][axis_u], supports[i][axis_v]) for i in (a, b, c)]
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
