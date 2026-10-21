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