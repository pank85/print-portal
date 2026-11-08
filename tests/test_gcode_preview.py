"""Unit tests for app/gcode_preview.py: thumbnail extraction, toolpath rasterization, caching."""
from __future__ import annotations

import base64
import os
import struct
import time
import zlib
from pathlib import Path

import pytest

import gcode_preview

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"


def _make_dummy_png(w: int, h: int) -> bytes:
    """Helper to generate minimal valid PNG bytes with specified dimensions."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\x12\x16\x1c" * w for _ in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return PNG_HEADER + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b"")


def _parse_png_ihdr(data: bytes) -> tuple[int, int, int, int]:
    """Parse width, height, bit_depth, color_type from PNG IHDR chunk."""
    assert data[:8] == PNG_HEADER, "Invalid PNG magic header"
    length = struct.unpack(">I", data[8:12])[0]
    assert length == 13, f"Unexpected IHDR length: {length}"
    assert data[12:16] == b"IHDR", "First chunk must be IHDR"
    w, h, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
    return w, h, bit_depth, color_type


def test_extract_thumbnail_single_png(tmp_path: Path) -> None:
    png_bytes = _make_dummy_png(32, 32)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    lines = [
        "; header comments",
        f"; thumbnail begin 32x32 {len(png_bytes)}",
        f"; {b64[:30]}",
        f"; {b64[30:]}",
        "; thumbnail end",
        "G28",
    ]
    p = tmp_path / "single.gcode"
    p.write_text("\n".join(lines), encoding="utf-8")

    result = gcode_preview.extract_thumbnail(p)
    assert result == png_bytes


def test_extract_thumbnail_multi_resolution(tmp_path: Path) -> None:
    png_small = _make_dummy_png(16, 16)
    png_large = _make_dummy_png(300, 300)
    png_mid = _make_dummy_png(64, 64)

    b64_small = base64.b64encode(png_small).decode("ascii")
    b64_large = base64.b64encode(png_large).decode("ascii")
    b64_mid = base64.b64encode(png_mid).decode("ascii")

    lines = [
        f"; thumbnail begin 16x16 {len(png_small)}",
        f"; {b64_small}",
        "; thumbnail end",
        f"; thumbnail begin 300x300 {len(png_large)}",
        f"; {b64_large}",
        "; thumbnail end",
        f"; thumbnail begin 64x64 {len(png_mid)}",
        f"; {b64_mid}",
        "; thumbnail end",
    ]
    p = tmp_path / "multi.gcode"
    p.write_text("\n".join(lines), encoding="utf-8")

    result = gcode_preview.extract_thumbnail(p)
    assert result == png_large


def test_extract_thumbnail_jpeg(tmp_path: Path) -> None:
    jpeg_bytes = JPEG_HEADER + b"\x00" * 32
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    lines = [
        f"; thumbnail_JPG begin 200x200 {len(jpeg_bytes)}",
        f"; {b64}",
        "; thumbnail_JPG end",
    ]
    p = tmp_path / "jpeg.gcode"
    p.write_text("\n".join(lines), encoding="utf-8")

    result = gcode_preview.extract_thumbnail(p)
    assert result == jpeg_bytes


def test_extract_thumbnail_malformed_base64(tmp_path: Path) -> None:
    lines = [
        "; thumbnail begin 64x64 500",
        "; !@#$%^&*()_+ NOT BASE64",
        "; thumbnail end",
    ]
    p = tmp_path / "malformed.gcode"
    p.write_text("\n".join(lines), encoding="utf-8")

    assert gcode_preview.extract_thumbnail(p) is None


def test_extract_thumbnail_non_image_payload(tmp_path: Path) -> None:
    text_bytes = b"Hello, this is just plain text, not a PNG or JPEG!"
    b64 = base64.b64encode(text_bytes).decode("ascii")
    lines = [
        "; thumbnail begin 64x64 100",
        f"; {b64}",
        "; thumbnail end",
    ]
    p = tmp_path / "text.gcode"
    p.write_text("\n".join(lines), encoding="utf-8")

    assert gcode_preview.extract_thumbnail(p) is None


def test_extract_thumbnail_no_thumbnail(tmp_path: Path) -> None:
    p = tmp_path / "plain.gcode"
    p.write_text("G28\nG1 X10 Y10 Z0.2 E1.0\n", encoding="utf-8")
    assert gcode_preview.extract_thumbnail(p) is None


def test_extract_thumbnail_missing_file(tmp_path: Path) -> None:
    assert gcode_preview.extract_thumbnail(tmp_path / "missing.gcode") is None


def test_render_toolpath_preview_absolute_e(tmp_path: Path) -> None:
    lines = [
        "M82 ; absolute extrusion",
        "G90 ; absolute coordinates",
        "G28 ; home",
        "G0 X50 Y50 Z0.2 ; travel to start",
        "G1 X100 Y50 E2.0 F1200 ; layer 1",
        "G1 X100 Y100 E4.0",
        "G1 X50 Y100 E6.0",
        "G1 X50 Y50 E8.0",
        "G92 E0 ; reset extruder",
        "G0 Z0.4 ; layer change",
        "G1 X100 Y50 E2.0",
        "G1 X100 Y100 E4.0",
        "G1 X50 Y100 E6.0",
        "G1 X50 Y50 E8.0",
    ]
    p = tmp_path / "box_abs.gcode"
    p.write_text("\n".join(lines), encoding="utf-8")

    png = gcode_preview.render_toolpath_preview(p, size=400)
    assert png.startswith(PNG_HEADER)
    w, h, bit_depth, color_type = _parse_png_ihdr(png)
    assert w == 400
    assert h == 400
    assert bit_depth == 8
    assert color_type == 2


def test_render_toolpath_preview_relative_e(tmp_path: Path) -> None:
    lines = [
        "M83 ; relative extrusion",
        "G90",
        "G0 X10 Y10 Z0.2",
        "G1 X50 Y10 E1.5",
        "G1 X50 Y50 E1.5",
        "G1 X10 Y50 E1.5",
        "G1 X10 Y10 E1.5",
    ]
    p = tmp_path / "box_rel.gcode"
    p.write_text("\n".join(lines), encoding="utf-8")

    png = gcode_preview.render_toolpath_preview(p, size=600)
    assert png.startswith(PNG_HEADER)
    w, h, bit_depth, color_type = _parse_png_ihdr(png)
    assert w == 600
    assert h == 600


def test_render_toolpath_preview_empty_and_travel_only(tmp_path: Path) -> None:
    empty = tmp_path / "empty.gcode"
    empty.write_text("; only comments\n; nothing here\n", encoding="utf-8")
    png_empty = gcode_preview.render_toolpath_preview(empty, size=200)
    assert png_empty.startswith(PNG_HEADER)
    assert _parse_png_ihdr(png_empty)[:2] == (200, 200)

    travel = tmp_path / "travel.gcode"
    travel.write_text("G0 X100 Y100 Z10\nG0 X200 Y200 Z20\n", encoding="utf-8")
    png_travel = gcode_preview.render_toolpath_preview(travel, size=200)
    assert png_travel.startswith(PNG_HEADER)
    assert _parse_png_ihdr(png_travel)[:2] == (200, 200)


def test_render_toolpath_preview_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        gcode_preview.render_toolpath_preview(tmp_path / "nonexistent.gcode")


def test_render_toolpath_preview_decimation(tmp_path: Path) -> None:
    # Generate 70,000 small segments exceeding MAX_SEGMENTS
    p = tmp_path / "dense.gcode"
    with p.open("w", encoding="utf-8") as fh:
        fh.write("M83\nG90\nG0 X0 Y0 Z0.2\n")
        for i in range(70_000):
            fh.write(f"G1 X{i % 200:.2f} Y{(i // 200) % 200:.2f} E0.01\n")

    start = time.perf_counter()
    png = gcode_preview.render_toolpath_preview(p, size=400)
    duration = time.perf_counter() - start

    assert png.startswith(PNG_HEADER)
    assert duration < 2.0, f"Decimated rendering took too long: {duration:.2f}s"


def test_get_or_render_preview_embedded_thumbnail(tmp_path: Path) -> None:
    png_bytes = _make_dummy_png(48, 48)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    p = tmp_path / "thumb_job.gcode"
    p.write_text(
        f"; thumbnail begin 48x48 {len(png_bytes)}\n; {b64}\n; thumbnail end\nG1 X10 Y10 E1\n",
        encoding="utf-8",
    )

    out = gcode_preview.get_or_render_preview(p)
    assert out == p.with_suffix(".png")
    assert out.is_file()
    assert out.read_bytes() == png_bytes


def test_get_or_render_preview_fallback(tmp_path: Path) -> None:
    p = tmp_path / "no_thumb.gcode"
    p.write_text("M82\nG90\nG0 X10 Y10 Z0.2\nG1 X20 Y10 E1.0\n", encoding="utf-8")

    out = gcode_preview.get_or_render_preview(p)
    assert out == p.with_suffix(".png")
    assert out.is_file()
    data = out.read_bytes()
    assert data.startswith(PNG_HEADER)
    w, h, _, _ = _parse_png_ihdr(data)
    assert w == 800 and h == 800


def test_get_or_render_preview_cache_reuse_and_invalidation(tmp_path: Path) -> None:
    p = tmp_path / "caching.gcode"
    p.write_text("M82\nG90\nG0 X10 Y10 Z0.2\nG1 X20 Y10 E1.0\n", encoding="utf-8")

    out = gcode_preview.get_or_render_preview(p)
    assert out.is_file()

    # Poison cache to verify it is reused without regenerating
    poisoned = b"CUSTOM_CACHED_CONTENT"
    out.write_bytes(poisoned)
    # Ensure cache mtime >= gcode mtime
    os.utime(out, (time.time() + 10, time.time() + 10))

    hit = gcode_preview.get_or_render_preview(p)
    assert hit == out
    assert hit.read_bytes() == poisoned

    # Touch G-code with future timestamp to invalidate cache
    time.sleep(0.01)
    os.utime(p, (time.time() + 20, time.time() + 20))

    regen = gcode_preview.get_or_render_preview(p)
    assert regen == out
    # Stale poisoned content was replaced by regenerated PNG
    assert regen.read_bytes().startswith(PNG_HEADER)


def test_get_or_render_preview_custom_cache_path(tmp_path: Path) -> None:
    p = tmp_path / "custom.gcode"
    p.write_text("M83\nG90\nG0 X0 Y0 Z0.2\nG1 X10 Y10 E0.5\n", encoding="utf-8")
    custom = tmp_path / "previews" / "custom_preview.png"

    out = gcode_preview.get_or_render_preview(p, cache_path=custom)
    assert out == custom
    assert custom.is_file()
    assert custom.read_bytes().startswith(PNG_HEADER)


def test_get_or_render_preview_missing_gcode(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        gcode_preview.get_or_render_preview(tmp_path / "does_not_exist.gcode")
