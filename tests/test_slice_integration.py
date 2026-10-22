"""The real OrcaSlicer CLI: an imported bundle slices, and a `from: "user"` machine does not.

The unit tests assert what the portal writes into the datadir; only a real slice proves the
CLI accepts it. Both tests import the committed fixture bundle into a throwaway copy of the
empty seed and run the binary over the three files that import wrote, so a regression in
``normalise()`` — a machine that is not ``from: "system"``, a process that does not name its
machine — fails here with the CLI's own return code instead of only on a printer.

Absolute preset paths are required: with relative ones the CLI answers rc 253 /
``return_code -3`` "The input files to the slicer are not found."

Skipped when no binary is available: ``ORCA_BIN`` when set (and only that path, so a
deliberately wrong value proves the skip), otherwise ``/opt/orca/bin/orca-slicer`` then
``/tmp/opt-orca/bin/orca-slicer``.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import profiles as prof

CANDIDATE_BINARIES = ("/opt/orca/bin/orca-slicer", "/tmp/opt-orca/bin/orca-slicer")
SLICE_TIMEOUT = 180


def orca_binary() -> str:
    """An executable OrcaSlicer, or a skip: an explicit ``ORCA_BIN`` is never second-guessed."""
    explicit = os.environ.get("ORCA_BIN")
    candidates = (explicit,) if explicit else CANDIDATE_BINARIES
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return candidate
    pytest.skip("no OrcaSlicer binary; set ORCA_BIN to an extracted orca-slicer")


def slice_stl(orca: str, datadir: Path, presets: dict[str, Path], stl: bytes,
              tmp_path: Path) -> tuple[dict, Path]:
    """Slice ``stl`` with those presets; returns the CLI's ``result.json`` and its output dir.

    The offscreen environment is what ``scripts/smoke_test.py`` uses: the AppImage's Qt build
    has no display in CI, and the CLI otherwise waits on a platform plugin that is missing.
    """
    stl_path = tmp_path / "cube.stl"
    stl_path.write_bytes(stl)
    out = tmp_path / "slice"
    (out / "home").mkdir(parents=True)
    env = {**os.environ, "HOME": str(out / "home"), "QT_QPA_PLATFORM": "offscreen",
           "DISPLAY": "", "LC_ALL": "C"}
    argv = [orca, "--datadir", str(datadir),
            "--load-settings", f"{presets['machine']};{presets['process']}",
            "--load-filaments", str(presets["filament"]),
            "--slice", "0", "--outputdir", str(out), str(stl_path)]

    proc = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=tmp_path,
                          timeout=SLICE_TIMEOUT)

    result_path = out / "result.json"
    assert result_path.is_file(), (proc.returncode, proc.stdout, proc.stderr)
    return json.loads(result_path.read_text()), out


def imported_presets(datadir: Path, bundle_bytes: bytes) -> dict[str, Path]:
    """Import the fixture bundle and return the three files it wrote, by preset type."""
    result = prof.import_into(datadir, bundle_bytes, "portal-import.orca_printer")
    assert result["ok"] is True
    return {row["type"]: datadir / row["path"] for row in result["imported"]}


def test_import_then_the_real_slicer_produces_gcode(empty_datadir, bundle_bytes, cube_stl,
                                                    tmp_path):
    orca = orca_binary()
    presets = imported_presets(empty_datadir, bundle_bytes)

    result, out = slice_stl(orca, empty_datadir, presets, cube_stl, tmp_path)

    assert result["return_code"] == 0, result
    assert result["error_string"] == "Success."
    gcode = out / "plate_1.gcode"
    assert gcode.is_file() and gcode.stat().st_size > 0


def test_the_real_slicer_rejects_a_user_sourced_machine(empty_datadir, bundle_bytes, cube_stl,
                                                        tmp_path):
    """The measured fact behind the rewrite: ``from: "user"`` is -17, not a warning.

    Pinned executably because it is the reason a machine preset is not just copied in
    verbatim — the CLI refuses the process as incompatible with the machine.
    """
    orca = orca_binary()
    presets = imported_presets(empty_datadir, bundle_bytes)
    machine = json.loads(presets["machine"].read_text())
    presets["machine"].write_text(json.dumps({**machine, "from": "user"}))

    result, out = slice_stl(orca, empty_datadir, presets, cube_stl, tmp_path)

    assert result["return_code"] == -17, result
    assert not (out / "plate_1.gcode").exists()
