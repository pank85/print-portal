"""The real OrcaSlicer CLI: an imported bundle slices, and a `from: "user"` machine does not.

The unit tests assert what the portal writes into the datadir; only a real slice proves the
CLI accepts it. Both import tests import the committed fixture bundle into a throwaway copy of
the empty seed and run the binary over the three files that import wrote, so a regression in
``normalise()`` — a machine that is not ``from: "system"``, a process that does not name its
machine — fails here with the CLI's own return code instead of only on a printer.

Absolute preset paths are required: with relative ones the CLI answers rc 253 /
``return_code -3`` "The input files to the slicer are not found."

The command-line half (`--orient`, `--scale`) is measured the same way, on a box of a known
size, because the CLI is the only place those two flags can be wrong in a way no unit test
sees: whether an oriented model really lies down, and whether a scaled one really lands
inside the bed, is a property of the gcode. `test_fit.py` covers the refusal half.

Skipped when no binary is available: ``ORCA_BIN`` when set (and only that path, so a
deliberately wrong value proves the skip), otherwise ``/opt/orca/bin/orca-slicer`` then
``/tmp/opt-orca/bin/orca-slicer``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import fit
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
              tmp_path: Path, extra_args: tuple[str, ...] = ()) -> tuple[dict, Path]:
    """Slice ``stl`` with those presets; returns the CLI's ``result.json`` and its output dir.

    ``extra_args`` goes where `main.run_slice` puts the option flags — after ``--slice 0`` and
    before ``--outputdir`` — so a test can pass ``--orient``/``--scale`` in production order.

    The offscreen environment is what ``scripts/smoke_test.py`` uses: the AppImage's Qt build
    has no display in CI, and the CLI otherwise waits on a platform plugin that is missing.
    """
    stl_path = tmp_path / "cube.stl"
    tmp_path.mkdir(parents=True, exist_ok=True)
    stl_path.write_bytes(stl)
    out = tmp_path / "slice"
    (out / "home").mkdir(parents=True)
    env = {**os.environ, "HOME": str(out / "home"), "QT_QPA_PLATFORM": "offscreen",
           "DISPLAY": "", "LC_ALL": "C"}
    argv = [orca, "--datadir", str(datadir),
            "--load-settings", f"{presets['machine']};{presets['process']}",
            "--load-filaments", str(presets["filament"]),
            "--slice", "0", *extra_args, "--outputdir", str(out), str(stl_path)]

    proc = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=tmp_path,
                          timeout=SLICE_TIMEOUT)

    result_path = out / "result.json"
    assert result_path.is_file(), (proc.returncode, proc.stdout, proc.stderr)
    return json.loads(result_path.read_text()), out


def scaled_cube_stl(cube_stl: Path, dx: float, dy: float, dz: float) -> Path:
    """A ``dx`` x ``dy`` x ``dz`` box, written beside ``cube_stl`` and returned.

    The fixture is ASCII and every vertex sits on 0 or 20, so scaling each coordinate by
    ``dx/20``, ``dy/20``, ``dz/20`` yields an exact box with the fixture's own winding — no
    mesh library in the tests. The result is written beside the input, so callers hand over a
    copy in ``tmp_path`` and the committed fixture stays untouched.
    """
    out = cube_stl.with_name(f"cube-{dx:g}x{dy:g}x{dz:g}.stl")
    lines = []
    for line in cube_stl.read_text().splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] == "vertex":
            x, y, z = (float(value) for value in parts[1:])
            line = "      vertex %.6f %.6f %.6f" % (x * dx / 20, y * dy / 20, z * dz / 20)
        lines.append(line)
    out.write_text("\n".join(lines) + "\n")
    return out


def box_stl(cube_stl: bytes, tmp_path: Path, dx: float, dy: float, dz: float) -> bytes:
    """The committed cube scaled to ``dx`` x ``dy`` x ``dz``, as the bytes ``slice_stl`` uploads."""
    source = tmp_path / "cube.stl"
    source.write_bytes(cube_stl)
    return scaled_cube_stl(source, dx, dy, dz).read_bytes()


def max_z(gcode: Path) -> float:
    """The highest Z the gcode reaches: the top of the print, from layer comments and moves."""
    highest = 0.0
    for line in gcode.read_text().splitlines():
        line = line.strip()
        if line.startswith(";Z:"):
            highest = max(highest, float(line[3:]))
        elif line.startswith(("G1 ", "G0 ")):
            match = re.search(r"(?<![A-Z])Z(-?\d+(?:\.\d+)?)", line)
            if match:
                highest = max(highest, float(match.group(1)))
    return highest


def max_xy(gcode: Path) -> float:
    """The furthest either horizontal axis reaches — the footprint the bed has to hold."""
    highest = 0.0
    for line in gcode.read_text().splitlines():
        line = line.strip()
        if not line.startswith(("G1 ", "G0 ")):
            continue
        for axis in "XY":
            match = re.search(rf"(?<![A-Z]){axis}(-?\d+(?:\.\d+)?)", line)
            if match:
                highest = max(highest, float(match.group(1)))
    return highest


def sha12(gcode: Path) -> str:
    """A content hash of the gcode, with the CLI's wall-clock banner left out.

    That banner reads ``; generated by OrcaSlicer <version> on <date> at <time>`` to the
    second, so hashing the file verbatim would compare two clocks instead of two toolpaths.
    """
    body = "\n".join(line for line in gcode.read_text().splitlines()
                     if not line.startswith("; generated by OrcaSlicer"))
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def imported_presets(datadir: Path, bundle_bytes: bytes) -> dict[str, Path]:
    """Import the fixture bundle and return the three files it wrote, by preset type."""
    result = prof.import_into(datadir, bundle_bytes, "portal-import.orca_printer")
    assert result["ok"] is True
    return {row["type"]: datadir / row["path"] for row in result["imported"]}


def portal_presets(datadir: Path, presets: dict[str, Path]) -> dict[str, Path]:
    """The presets as a job slices them: the imported process replaced by the derived copy.

    `main.run_slice` never loads the process preset directly — it loads
    `prof.write_derived_process(...)` of it, which carries the portal options (brim on, 5 mm
    outer brim). The footprint assertions below are only meaningful on that copy: with the
    imported preset the CLI decides the brim itself and the same model ends up wider.
    """
    derived = prof.write_derived_process(presets["process"], datadir, prof.DEFAULT_OPTIONS)
    return {**presets, "process": derived}


def registered_preset(datadir: Path, index: dict, kind: str) -> Path:
    """The single preset of ``kind`` the import registered, as a path under ``datadir``."""
    rows = [row for row in index.get(prof.VENDOR_LISTS[kind], []) if isinstance(row, dict)]
    assert len(rows) == 1, rows
    return datadir / "system" / "Custom" / rows[0]["sub_path"]


def big_bed_presets(datadir: Path, name: str = "Big 350") -> dict[str, Path]:
    """Copy the imported machine as a 350 mm bed named ``name`` and retarget the process at it.

    Three separate facts are pinned here because each one alone answers ``-17`` "The selected
    printer is not compatible with the process preset": the copy must carry ``name`` as its own
    preset name (the file name does not matter), it must be registered in
    ``system/Custom.json``'s ``machine_list``, and the process's ``compatible_printers`` must be
    a one-element *list* of that name.
    """
    index = prof.read_vendor_index(datadir)
    paths = {kind: registered_preset(datadir, index, kind)
             for kind in ("machine", "process", "filament")}

    machine = json.loads(paths["machine"].read_text())
    machine.update({
        "name": name,
        "printable_area": ["0x0", "350x0", "350x350", "0x350"],
        "printable_height": "350",
    })
    big = prof.write_preset(datadir, "machine", machine)
    prof.register_preset(index, "machine", name, f"machine/{big.name}")
    prof.write_vendor_index(datadir, index)

    process = json.loads(paths["process"].read_text())
    process["compatible_printers"] = [name]
    paths["process"].write_text(json.dumps(process))
    return {**paths, "machine": big}


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


def test_a_300mm_cube_shrinks_to_fit(empty_datadir, bundle_bytes, cube_stl, tmp_path):
    """The scale handed to the CLI is `fit.plan_fit`'s own answer, measured here as 0.68.

    The margin takes 16 mm off each of the X and Y spans, so (220 - 16) / 300 floored is
    0.68 — computed from `app/fit.py` above rather than written as a literal, so the shipped
    arithmetic and this slice cannot drift apart. 204 mm of model plus the portal's 5 mm
    outer brim lands at 219.09 mm across, which is the margin doing its job — the bed is
    220 mm and the CLI refuses gcode outside it (rc -102).
    """
    orca = orca_binary()
    presets = portal_presets(empty_datadir, imported_presets(empty_datadir, bundle_bytes))

    cube300 = box_stl(cube_stl, tmp_path, 300, 300, 300)
    model = tmp_path / "cube300.stl"
    model.write_bytes(cube300)
    k = fit.plan_fit(model, presets["machine"], shrink_to_fit=True).scale
    assert k == 0.68, k
    result, out = slice_stl(orca, empty_datadir, presets, cube300, tmp_path,
                            ("--orient", "1", "--scale", f"{k:g}"))

    assert result["return_code"] == 0, result
    assert result["error_string"] == "Success."
    gcode = out / "plate_1.gcode"
    assert gcode.is_file()
    assert max_xy(gcode) <= 220
    assert max_z(gcode) <= 250


def test_a_300mm_cube_without_shrink_does_not_slice(empty_datadir, bundle_bytes, cube_stl,
                                                    tmp_path):
    """The CLI's own refusal names no dimension, so the portal has to refuse earlier.

    Measured rc -50, "One of the plate is empty or has no object fully inside it": nothing in
    that text says the model is 300 mm on a 220 mm bed, which is why `fit.plan_fit` produces
    the sizing message and fails the job before the slicer runs (covered in `test_fit.py`).
    """
    orca = orca_binary()
    presets = portal_presets(empty_datadir, imported_presets(empty_datadir, bundle_bytes))

    cube300 = box_stl(cube_stl, tmp_path, 300, 300, 300)
    result, out = slice_stl(orca, empty_datadir, presets, cube300, tmp_path, ("--orient", "1"))

    assert result["return_code"] != 0, result
    assert not (out / "plate_1.gcode").exists()
    message = result["error_string"]
    assert "plate is empty" in message or "unprintable" in message, message


def test_orient_changes_a_model_lying_on_its_side(empty_datadir, bundle_bytes, cube_stl,
                                                  tmp_path):
    """A 40x20x200 box prints upright with ``--orient 0`` and lying down with ``--orient 1``.

    Measured: 200.40 mm tall against 20.40 mm, with different gcode. This is the pointed-at
    metric for the flag — an assertion on a differing hash, not a skip, because a slicer that
    ignored ``--orient`` would make every other orient test in this file vacuous.
    """
    orca = orca_binary()
    presets = portal_presets(empty_datadir, imported_presets(empty_datadir, bundle_bytes))
    box = box_stl(cube_stl, tmp_path, 40, 20, 200)

    off, off_out = slice_stl(orca, empty_datadir, presets, box, tmp_path / "off", ("--orient", "0"))
    on, on_out = slice_stl(orca, empty_datadir, presets, box, tmp_path / "on", ("--orient", "1"))

    assert off["return_code"] == 0, off
    assert on["return_code"] == 0, on
    upright = off_out / "plate_1.gcode"
    lying = on_out / "plate_1.gcode"
    assert max_z(upright) > 100
    assert max_z(lying) < 40
    assert sha12(upright) != sha12(lying)


def test_a_model_that_already_fits_needs_no_scale(empty_datadir, bundle_bytes, cube_stl,
                                                  tmp_path):
    """``--scale 1`` is a no-op, which is why `main.run_slice` passes nothing when k == 1.0.

    Same gcode either way, so a 20 mm cube on a 220 mm bed keeps the toolpath the user would
    have got without the shrink feature at all.
    """
    orca = orca_binary()
    presets = portal_presets(empty_datadir, imported_presets(empty_datadir, bundle_bytes))

    plain, plain_out = slice_stl(orca, empty_datadir, presets, cube_stl, tmp_path / "plain")
    scaled, scaled_out = slice_stl(orca, empty_datadir, presets, cube_stl, tmp_path / "scaled",
                                   ("--scale", "1"))

    assert plain["return_code"] == 0, plain
    assert scaled["return_code"] == 0, scaled
    assert sha12(plain_out / "plate_1.gcode") == sha12(scaled_out / "plate_1.gcode")


def test_orient_is_a_no_op_for_a_flat_cube(empty_datadir, bundle_bytes, cube_stl, tmp_path):
    """A cube already sitting flat is not turned over by ``--orient 1`` — the default stays free.

    Measured: byte-identical gcode with and without the flag, so defaulting auto-orient on
    cannot change what a model that needs no help prints.
    """
    orca = orca_binary()
    presets = portal_presets(empty_datadir, imported_presets(empty_datadir, bundle_bytes))

    plain, plain_out = slice_stl(orca, empty_datadir, presets, cube_stl, tmp_path / "plain")
    oriented, oriented_out = slice_stl(orca, empty_datadir, presets, cube_stl,
                                       tmp_path / "oriented", ("--orient", "1"))

    assert plain["return_code"] == 0, plain
    assert oriented["return_code"] == 0, oriented
    assert sha12(plain_out / "plate_1.gcode") == sha12(oriented_out / "plate_1.gcode")


def test_a_350mm_bed_needs_no_scale(empty_datadir, bundle_bytes, cube_stl, tmp_path):
    """On a 350 mm bed the same 300 mm cube needs k == 1.0, so no ``--scale`` is passed.

    The margin takes 16 mm off each axis span (8 mm at each edge), leaving 350 - 16 = 334 mm
    usable: `fit.plan_fit` caps the factor at 1.0 and the CLI must slice the model as
    uploaded; a factor below 1 would show up as a model that is no longer 300 mm.
    """
    orca = orca_binary()
    imported_presets(empty_datadir, bundle_bytes)
    presets = portal_presets(empty_datadir, big_bed_presets(empty_datadir))

    cube300 = box_stl(cube_stl, tmp_path, 300, 300, 300)
    model = tmp_path / "cube300.stl"
    model.write_bytes(cube300)
    k = fit.plan_fit(model, presets["machine"], shrink_to_fit=True).scale
    assert k == 1.0, k
    result, out = slice_stl(orca, empty_datadir, presets, cube300, tmp_path, ("--orient", "1"))

    assert result["return_code"] == 0, result
    gcode = out / "plate_1.gcode"
    assert gcode.is_file() and gcode.stat().st_size > 0


def test_a_350mm_bed_output_is_identical_with_and_without_orient(empty_datadir, bundle_bytes,
                                                                 cube_stl, tmp_path):
    """The orient default does not depend on the bed: a flat cube prints the same on 350 mm."""
    orca = orca_binary()
    imported_presets(empty_datadir, bundle_bytes)
    presets = portal_presets(empty_datadir, big_bed_presets(empty_datadir))

    plain, plain_out = slice_stl(orca, empty_datadir, presets, cube_stl, tmp_path / "plain")
    oriented, oriented_out = slice_stl(orca, empty_datadir, presets, cube_stl,
                                       tmp_path / "oriented", ("--orient", "1"))

    assert plain["return_code"] == 0, plain
    assert oriented["return_code"] == 0, oriented
    assert sha12(plain_out / "plate_1.gcode") == sha12(oriented_out / "plate_1.gcode")
