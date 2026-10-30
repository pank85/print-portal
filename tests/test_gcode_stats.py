"""Offline tests for app/gcode.py::parse_gcode_stats."""
from __future__ import annotations

from pathlib import Path

import gcode


def test_orca_header_shape(tmp_path: Path) -> None:
    p = tmp_path / "a.gcode"
    p.write_text(
        "; total filament used [g] = 3.94\n"
        "; estimated printing time (normal mode) = 1m 2s\n"
        "; total layers count = 100\n"
        "; layer_height = 0.2\n",
        encoding="utf-8",
    )
    assert gcode.parse_gcode_stats(p) == {
        "grams": 3.94,
        "time": "1m 2s",
        "layers": 100,
        "layer_height": "0.2",
    }


def test_prusa_shape(tmp_path: Path) -> None:
    p = tmp_path / "b.gcode"
    p.write_text(
        ";TIME:125\n;Filament used:1.5m\n;Layer height:0.2\n",
        encoding="utf-8",
    )
    stats = gcode.parse_gcode_stats(p)
    assert stats["time"] == "2m 5s"
    assert stats["mm"] == 1500.0
    assert stats["grams"] != 0
    assert stats["layer_height"] == "0.2"


def test_summary_far_into_file(tmp_path: Path) -> None:
    p = tmp_path / "far.gcode"
    chunk_lines = 10_000
    with p.open("wb") as fh:
        for _ in range(20):  # 200_000 filler lines
            fh.write(b"G1 X0 Y0\n" * chunk_lines)
        fh.write(b"; total filament used [g] = 7.77\n")
        fh.write(b"; estimated printing time (normal mode) = 3m 4s\n")
        fh.write(b"; total layers count = 42\n")
        fh.write(b"; layer_height = 0.12\n")
    stats = gcode.parse_gcode_stats(p)
    assert stats["grams"] == 7.77
    assert stats["layers"] == 42


def test_cm3_only_fallback(tmp_path: Path) -> None:
    p = tmp_path / "cm3.gcode"
    p.write_text("; filament used [cm3] = 10\n", encoding="utf-8")
    assert gcode.parse_gcode_stats(p)["grams"] == 12.4


def test_mm_only_orca(tmp_path: Path) -> None:
    p = tmp_path / "mm.gcode"
    p.write_text("; filament used [mm] = 1000\n", encoding="utf-8")
    stats = gcode.parse_gcode_stats(p)
    assert stats["mm"] == 1000.0
    assert stats["grams"] == 0.0


def test_prusa_mm_only_grams_fallback(tmp_path: Path) -> None:
    p = tmp_path / "prusa_mm.gcode"
    p.write_text(";Filament used:1.0m\n", encoding="utf-8")
    stats = gcode.parse_gcode_stats(p)
    assert stats["mm"] == 1000.0
    assert stats["grams"] == 0.29


def test_missing_everything(tmp_path: Path) -> None:
    p = tmp_path / "empty.gcode"
    p.write_text("G1 X0\nG1 Y0\n", encoding="utf-8")
    stats = gcode.parse_gcode_stats(p)
    assert stats == {"grams": 0.0}


def test_missing_file(tmp_path: Path) -> None:
    assert gcode.parse_gcode_stats(tmp_path / "nope.gcode") == {}


def test_crlf_input(tmp_path: Path) -> None:
    p = tmp_path / "crlf.gcode"
    p.write_bytes(b"; total filament used [g] = 2.5\r\n; total layers count = 5\r\n")
    stats = gcode.parse_gcode_stats(p)
    assert stats["grams"] == 2.5
    assert stats["layers"] == 5


def test_binary_junk_does_not_raise(tmp_path: Path) -> None:
    p = tmp_path / "junk.gcode"
    p.write_bytes(b"\x00\xff\xfe; total layers count = 3\n\x80\x81garbage")
    stats = gcode.parse_gcode_stats(p)
    assert isinstance(stats, dict)


def test_match_split_across_64k_boundary(tmp_path: Path) -> None:
    p = tmp_path / "split.gcode"
    s = 64 * 1024 - 5
    filler = b"A" * (s - 1) + b"\n"
    p.write_bytes(filler + b"; total filament used [g] = 3.94\n")
    stats = gcode.parse_gcode_stats(p)
    assert stats["grams"] == 3.94


SAMPLE_MULTI_LAYER_GCODE = """\
;FLAVOR:Klipper
;Layer height:0.2
; HEADER_BLOCK_START
; total layer number: 4
; max_z_height: 0.88
; HEADER_BLOCK_END
PRINT_START BED=55 EXTRUDER=210
G90
G21
M83
M106 S0
;LAYER_CHANGE
;Z:0.28
;HEIGHT:0.28
BEFORE_LAYER_CHANGE LAYER=0
G1 E-1.2 F2400
G1 X10 Y10 F9000
G1 Z0.68
G1 Z0.28
G1 E1.2 F1800
G1 X20 Y10 E0.5
;LAYER_CHANGE
;Z:0.48
;HEIGHT:0.2
BEFORE_LAYER_CHANGE LAYER=1
G1 E-1.2 F2400
G1 X20 Y20 Z0.88
G1 Z0.48
G1 E1.2 F1800
G1 X30 Y20 E0.5
;LAYER_CHANGE
;Z:0.68
;HEIGHT:0.2
BEFORE_LAYER_CHANGE LAYER=2
G1 E-1.0 F2400
;WIPE_START
G1 X29 Y20 E-0.2
;WIPE_END
;_SET_FAN_SPEED_CHANGING_LAYER
; printing object model.stl id:1 copy 0
EXCLUDE_OBJECT_START NAME=model_stl
G1 F9000
G1 X28 Y20 Z0.74
G1 X27 Y20 Z1.08
G1 X50 Y60 Z1.08
G1 Z0.68
G1 E1.2 F1800
;TYPE:Outer wall
G1 F3600
G1 X60 Y60 E0.4
M106 S255
G1 X70 Y60 E0.4
EXCLUDE_OBJECT_END NAME=model_stl
M106 S227
;LAYER_CHANGE
;Z:0.88
;HEIGHT:0.2
BEFORE_LAYER_CHANGE LAYER=3
G1 E-1.2 F2400
G1 X70 Y60 Z1.28
G1 X80 Y80 Z1.28
G1 Z0.88
G1 E1.2 F1800
G1 X90 Y80 E0.4
PRINT_END
; EXECUTABLE_BLOCK_END
; total layers count = 4
"""


def test_inspect_gcode_layers_parses_orca_layers(tmp_path: Path) -> None:
    p = tmp_path / "sample.gcode"
    p.write_text(SAMPLE_MULTI_LAYER_GCODE, encoding="utf-8")
    layers = gcode.inspect_gcode_layers(p)
    assert [entry["layer"] for entry in layers] == [0, 1, 2, 3]
    assert [entry["z"] for entry in layers] == [0.28, 0.48, 0.68, 0.88]
    assert [entry["height"] for entry in layers] == [0.28, 0.2, 0.2, 0.2]


def test_rebase_gcode_to_bed_shifts_z_and_cleans_first_layer(tmp_path: Path) -> None:
    src = tmp_path / "src.gcode"
    dst = tmp_path / "dst.gcode"
    src.write_text(SAMPLE_MULTI_LAYER_GCODE, encoding="utf-8")

    meta = gcode.rebase_gcode_to_bed(src, dst, start_layer=2)
    assert meta == {
        "start_layer": 2,
        "total_layers": 4,
        "remaining_layers": 2,
        "original_z": 0.68,
        "bed_z": 0.2,
        "z_offset": 0.48,
        "max_z_height": 0.4,
    }
    text = dst.read_text(encoding="utf-8")
    assert "; total layer number: 2" in text
    assert "; max_z_height: 0.4" in text
    assert "; total layers count = 2" in text
    assert "BEFORE_LAYER_CHANGE LAYER=0" in text
    assert "BEFORE_LAYER_CHANGE LAYER=1" in text
    assert "BEFORE_LAYER_CHANGE LAYER=2" not in text
    # Clean first-layer entry to X50 Y60 at Z0.6 -> Z0.2 (no wipe at X29 Y20)
    assert "G1 X50 Y60 F9000\nG1 Z0.6\nG1 Z0.2\nG1 E1.2 F1800" in text
    assert "G1 X29 Y20 E-0.2" not in text
    # Mid-layer M106 S255 on the first bed layer is suppressed, end-of-layer M106 S227 kept
    assert "; M106 S255 ; suppressed on first bed layer" in text
    assert "\nM106 S227\n" in text
    # Layer 3 (now LAYER=1) Z=0.88 shifted by -0.48 -> Z=0.4, hop Z=1.28 -> Z=0.8
    assert ";Z:0.4\n;HEIGHT:0.2\nBEFORE_LAYER_CHANGE LAYER=1" in text
    assert "G1 X80 Y80 Z0.8\nG1 Z0.4" in text


def test_rebase_gcode_to_bed_by_start_z_and_invalid_bounds(tmp_path: Path) -> None:
    src = tmp_path / "src.gcode"
    dst = tmp_path / "dst.gcode"
    src.write_text(SAMPLE_MULTI_LAYER_GCODE, encoding="utf-8")

    meta = gcode.rebase_gcode_to_bed(src, dst, start_z=0.65)
    assert meta["start_layer"] == 2
    assert meta["bed_z"] == 0.2

    import pytest
    with pytest.raises(ValueError, match="start_layer must be between 1 and 3"):
        gcode.rebase_gcode_to_bed(src, dst, start_layer=0)
    with pytest.raises(ValueError, match="start_layer must be between 1 and 3"):
        gcode.rebase_gcode_to_bed(src, dst, start_layer=99)