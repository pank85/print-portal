"""Pure-Python G-code toolpath preview rasterizer and embedded thumbnail extractor.

Zero external dependencies: uses standard library struct, zlib, re, base64, math, pathlib.
Extracts embedded thumbnails from sliced files or renders an axonometric 3D toolpath
preview of extrusion moves onto an RGB canvas compressed to PNG.
"""
from __future__ import annotations

import base64
import math
import os
import re
import struct
import zlib
from pathlib import Path

MAX_SEGMENTS = 60_000
DEFAULT_SIZE = 800

_BG = (18, 22, 28)
_BED_BORDER = (45, 55, 72)
_C_BASE = (20, 184, 166)  # Teal / cyan at bed level
_C_TOP = (52, 211, 153)   # Vibrant emerald at top layer

# 30° isometric projection vectors matching app/shot.py
_THETA = math.radians(30)
_C_TH = math.cos(_THETA)
_S_TH = math.sin(_THETA)
_INV_SQRT2 = 1.0 / math.sqrt(2.0)
_R_VEC = (_INV_SQRT2, -_INV_SQRT2, 0.0)
_U_VEC = (-_INV_SQRT2 * _S_TH, -_INV_SQRT2 * _S_TH, _C_TH)

_PARAM_RE = re.compile(r"([A-Za-z])([-+]?[0-9]*\.?[0-9]+)")
_THUMBNAIL_BEGIN_RE = re.compile(
    r"^\s*;\s*thumbnail(?:_[A-Za-z0-9]+)?\s+begin\s+(\d+)\s*[xX]\s*(\d+)(?:\s+(\d+))?",
    re.IGNORECASE,
)
_THUMBNAIL_END_RE = re.compile(
    r"^\s*;\s*thumbnail(?:_[A-Za-z0-9]+)?\s+end",
    re.IGNORECASE,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def _png(rgb: bytes, width: int, height: int) -> bytes:
    """Encode raw RGB scanlines into a valid PNG byte string."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(
        b"\x00" + rgb[y * width * 3 : (y + 1) * width * 3] for y in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_MAGIC
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 1))
        + chunk(b"IEND", b"")
    )


def extract_thumbnail(gcode_path: Path) -> bytes | None:
    """Scan for embedded thumbnail blocks, decode base64 payload, and return largest valid image bytes.

    Looks for blocks delimited by `; thumbnail begin <w>x<h> <len>` and `; thumbnail end`
    (or `thumbnail_JPG`). If multiple resolutions exist, selects the largest by dimensions (w*h)
    or byte length. Validates PNG or JPEG magic headers. Returns None if no valid thumbnail found.
    """
    gcode_path = Path(gcode_path)
    if not gcode_path.is_file():
        return None

    thumbnails: list[tuple[int, int, int, bytes]] = []  # (w, h, len, payload)
    in_thumbnail = False
    current_w = 0
    current_h = 0
    chunks: list[str] = []

    try:
        with gcode_path.open("r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line_str = line.strip()
                if not in_thumbnail:
                    match = _THUMBNAIL_BEGIN_RE.match(line_str)
                    if match:
                        in_thumbnail = True
                        current_w = int(match.group(1))
                        current_h = int(match.group(2))
                        chunks = []
                else:
                    if _THUMBNAIL_END_RE.match(line_str):
                        in_thumbnail = False
                        b64_str = "".join(chunks)
                        try:
                            payload = base64.b64decode(b64_str)
                        except Exception:
                            payload = b""
                        if payload.startswith(PNG_MAGIC) or payload.startswith(JPEG_MAGIC):
                            thumbnails.append((current_w, current_h, len(payload), payload))
                        chunks = []
                    elif _THUMBNAIL_BEGIN_RE.match(line_str):
                        # Unterminated previous thumbnail, reset to new begin
                        match = _THUMBNAIL_BEGIN_RE.match(line_str)
                        current_w = int(match.group(1))
                        current_h = int(match.group(2))
                        chunks = []
                    else:
                        content = line_str.lstrip(";").strip()
                        if content:
                            chunks.append(content)
    except OSError:
        return None

    if not thumbnails:
        return None

    # Largest thumbnail by resolution (w * h) then payload length
    thumbnails.sort(key=lambda t: (t[0] * t[1], t[2]), reverse=True)
    return thumbnails[0][3]


def render_toolpath_preview(gcode_path: Path, size: int = DEFAULT_SIZE) -> bytes:
    """Stream G-code, extract extrusion moves, and render an isometric toolpath preview PNG."""
    gcode_path = Path(gcode_path)
    if not gcode_path.is_file():
        raise FileNotFoundError(f"G-code file not found: {gcode_path}")

    curr_x = curr_y = curr_z = curr_e = 0.0
    relative_pos = False
    relative_e = False
    segments: list[tuple[float, float, float, float, float, float]] = []

    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")

    with gcode_path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.split(";", 1)[0].strip()
            if not line:
                continue
            line = re.sub(r"^[Nn]\d+\s*", "", line)
            if "*" in line:
                line = line.split("*", 1)[0].strip()

            cmd = None
            params: dict[str, float] = {}
            for match in _PARAM_RE.finditer(line):
                letter = match.group(1).upper()
                val = match.group(2)
                if cmd is None and letter in ("G", "M"):
                    cmd = f"{letter}{int(float(val))}"
                else:
                    try:
                        params[letter] = float(val)
                    except ValueError:
                        pass

            if cmd == "M82":
                relative_e = False
            elif cmd == "M83":
                relative_e = True
            elif cmd == "G90":
                relative_pos = False
            elif cmd == "G91":
                relative_pos = True
            elif cmd == "G92":
                if "E" in params:
                    curr_e = params["E"]
                if "X" in params:
                    curr_x = params["X"]
                if "Y" in params:
                    curr_y = params["Y"]
                if "Z" in params:
                    curr_z = params["Z"]
            elif cmd in ("G0", "G1", "G2", "G3"):
                next_x = (
                    curr_x + params["X"]
                    if relative_pos and "X" in params
                    else params.get("X", curr_x)
                )
                next_y = (
                    curr_y + params["Y"]
                    if relative_pos and "Y" in params
                    else params.get("Y", curr_y)
                )
                next_z = (
                    curr_z + params["Z"]
                    if relative_pos and "Z" in params
                    else params.get("Z", curr_z)
                )

                is_extruding = False
                if "E" in params:
                    e_val = params["E"]
                    if relative_e:
                        is_extruding = e_val > 1e-6
                        curr_e += e_val
                    else:
                        is_extruding = e_val > curr_e + 1e-6
                        curr_e = e_val

                if is_extruding and (
                    next_x != curr_x or next_y != curr_y or next_z != curr_z
                ):
                    segments.append((curr_x, curr_y, curr_z, next_x, next_y, next_z))
                    min_x = min(min_x, curr_x, next_x)
                    max_x = max(max_x, curr_x, next_x)
                    min_y = min(min_y, curr_y, next_y)
                    max_y = max(max_y, curr_y, next_y)
                    min_z = min(min_z, curr_z, next_z)
                    max_z = max(max_z, curr_z, next_z)

                curr_x, curr_y, curr_z = next_x, next_y, next_z

    if not segments:
        return _png(bytes(bytearray(bytes(_BG) * (size * size))), size, size)

    if len(segments) > MAX_SEGMENTS:
        step = math.ceil(len(segments) / MAX_SEGMENTS)
        segments = segments[::step]

    bed_z = min(0.0, min_z)
    margin = max(4.0, 0.08 * max(max_x - min_x, max_y - min_y, 1.0))
    bed_pts = [
        (min_x - margin, min_y - margin, bed_z),
        (max_x + margin, min_y - margin, bed_z),
        (max_x + margin, max_y + margin, bed_z),
        (min_x - margin, max_y + margin, bed_z),
    ]

    def proj(x: float, y: float, z: float) -> tuple[float, float]:
        return (
            _R_VEC[0] * x + _R_VEC[1] * y + _R_VEC[2] * z,
            _U_VEC[0] * x + _U_VEC[1] * y + _U_VEC[2] * z,
        )

    box_corners = [
        (x, y, z)
        for x in (min_x, max_x)
        for y in (min_y, max_y)
        for z in (min_z, max_z)
    ]
    all_bounds = box_corners + bed_pts
    us = [_R_VEC[0] * x + _R_VEC[1] * y + _R_VEC[2] * z for x, y, z in all_bounds]
    vs = [_U_VEC[0] * x + _U_VEC[1] * y + _U_VEC[2] * z for x, y, z in all_bounds]

    span_u = max(us) - min(us)
    span_v = max(vs) - min(vs)
    span = max(span_u, span_v, 1.0) * 1.10
    mid_u = (max(us) + min(us)) / 2.0
    mid_v = (max(vs) + min(vs)) / 2.0

    def px(u: float, v: float) -> tuple[int, int]:
        x = int(round(((u - mid_u) / span + 0.5) * (size - 1)))
        y = int(round((0.5 - (v - mid_v) / span) * (size - 1)))
        return min(size - 1, max(0, x)), min(size - 1, max(0, y))

    img = bytearray(bytes(_BG) * (size * size))

    def draw_line(
        x0: int, y0: int, x1: int, y1: int, r: int, g: int, b: int
    ) -> None:
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        while True:
            if 0 <= x0 < size and 0 <= y0 < size:
                idx = (y0 * size + x0) * 3
                img[idx] = r
                img[idx + 1] = g
                img[idx + 2] = b
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    # Bed outline
    bed_px = [px(*proj(*p)) for p in bed_pts]
    for i in range(4):
        p_start = bed_px[i]
        p_end = bed_px[(i + 1) % 4]
        draw_line(p_start[0], p_start[1], p_end[0], p_end[1], *_BED_BORDER)

    # Toolpath segments
    z_range = max_z - min_z
    for x0, y0, z0, x1, y1, z1 in segments:
        if z_range > 1e-4:
            mid_z = (z0 + z1) / 2.0
            t = (mid_z - min_z) / z_range
            t = max(0.0, min(1.0, t))
        else:
            t = 0.0
        r = int(round(_C_BASE[0] + (_C_TOP[0] - _C_BASE[0]) * t))
        g = int(round(_C_BASE[1] + (_C_TOP[1] - _C_BASE[1]) * t))
        b = int(round(_C_BASE[2] + (_C_TOP[2] - _C_BASE[2]) * t))

        sx0, sy0 = px(*proj(x0, y0, z0))
        sx1, sy1 = px(*proj(x1, y1, z1))
        draw_line(sx0, sy0, sx1, sy1, r, g, b)

    return _png(bytes(img), size, size)


def get_or_render_preview(
    gcode_path: Path, cache_path: Path | None = None
) -> Path:
    """Return path to preview image, using disk cache or generating on demand.

    Checks if cached preview exists and is newer than the G-code file.
    If not, attempts embedded thumbnail extraction, falling back to 3D toolpath rendering.
    Writes preview bytes to cache path and returns it.
    """
    gcode_path = Path(gcode_path)
    if not gcode_path.is_file():
        raise FileNotFoundError(f"G-code file not found: {gcode_path}")

    target_cache = (
        Path(cache_path) if cache_path is not None else gcode_path.with_suffix(".png")
    )

    if (
        target_cache.is_file()
        and target_cache.stat().st_size > 0
        and target_cache.stat().st_mtime >= gcode_path.stat().st_mtime
    ):
        return target_cache

    payload = extract_thumbnail(gcode_path)
    if not payload:
        payload = render_toolpath_preview(gcode_path)

    target_cache.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_cache.with_name(f".{target_cache.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_bytes(payload)
        tmp_path.replace(target_cache)
    except Exception:
        if tmp_path.is_file():
            tmp_path.unlink(missing_ok=True)
        target_cache.write_bytes(payload)

    return target_cache
