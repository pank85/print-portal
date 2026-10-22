"""fit.py: the uniform shrink factor, its floor, and the refusals that explain themselves.

The arithmetic under test is deliberately dull — `min` over three ratios, floored to three
decimals — so the tests pin the two places it is easy to get wrong: Z carries no 16 mm
margin (`test_z_binds_without_margin`), and the factor is floored rather than rounded
(`test_scale_is_floored_never_rounded_up`), because rounding up would spend the margin the
margin exists to keep.

The refusals are user-facing text written verbatim into a job's `error`, so they are
asserted for content, not exact wording: the model's size, the bed it was judged against, the
factor, the floor, and the size to re-export.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import fit
import mesh
import profiles as prof

FIXTURES = Path(__file__).resolve().parent / "fixtures"

BED_220 = prof.BedLimits(0.0, 0.0, 220.0, 220.0, 250.0, "Test 220")
MACHINE_220 = "system/Custom/machine/generic-klipper-0.4.json"


def ascii_stl(sx: float, sy: float, sz: float) -> bytes:
    """An ASCII STL whose three vertices span ``sx`` by ``sy`` by ``sz`` mm."""
    vertices = ((0.0, 0.0, 0.0), (sx, sy, 0.0), (0.0, sy, sz))
    lines = ["solid test", "  facet normal 0 0 0", "    outer loop"]
    lines += [f"      vertex {x:g} {y:g} {z:g}" for x, y, z in vertices]
    lines += ["    endloop", "  endfacet", "endsolid test"]
    return ("\n".join(lines) + "\n").encode()


def write_model(directory: Path, name: str, sx: float, sy: float, sz: float) -> Path:
    path = directory / name
    path.write_bytes(ascii_stl(sx, sy, sz))
    return path


def write_machine(directory: Path, name: str, bed: float, height: float = 250.0) -> Path:
    """A machine preset with a square ``bed`` mm printable area, as the datadir holds it."""
    path = directory / f"{name.replace(' ', '_')}.json"
    path.write_text(json.dumps({
        "type": "machine",
        "name": name,
        "printable_area": [f"0x0", f"{bed:g}x0", f"{bed:g}x{bed:g}", f"0x{bed:g}"],
        "printable_height": f"{height:g}",
    }))
    return path


def test_exact_fit_leaves_the_scale_at_one(datadir: Path) -> None:
    bbox = mesh.BoundingBox(0.0, 0.0, 0.0, 20.0, 20.0, 20.0)
    assert fit.scale_factor(bbox, BED_220, margin=0) == 1.0

    plan = fit.plan_fit(FIXTURES / "cube.stl", datadir / MACHINE_220, shrink_to_fit=True)
    assert plan.scale == 1.0
    assert plan.bbox.size == (20.0, 20.0, 20.0)
    assert plan.bed.machine == "Generic Klipper 0.4 nozzle"


def test_one_axis_binds() -> None:
    bbox = mesh.BoundingBox(0.0, 0.0, 0.0, 300.0, 100.0, 50.0)
    k = fit.scale_factor(bbox, BED_220)

    assert k == 0.68                       # (220 - 16) / 300, floored
    assert (220.0 - fit.FIT_MARGIN_MM) / 100.0 > k      # Y would have allowed 2.04
    assert 250.0 / 50.0 > k                             # Z would have allowed 5.0


def test_z_binds_without_margin() -> None:
    bbox = mesh.BoundingBox(0.0, 0.0, 0.0, 20.0, 20.0, 300.0)

    assert fit.scale_factor(bbox, BED_220) == 0.833     # 250 / 300, no 16 mm margin


def test_the_floor_refuses_and_names_everything(tmp_path: Path) -> None:
    model = write_model(tmp_path, "huge.stl", 1000.0, 1000.0, 1000.0)
    machine = write_machine(tmp_path, "Portal 220", 220.0)

    with pytest.raises(fit.FitError) as excinfo:
        fit.plan_fit(model, machine, shrink_to_fit=True)

    message = str(excinfo.value)
    assert "huge.stl" in message
    assert "1000x1000x1000" in message              # the model's size
    assert "0.20" in message                        # the factor needed (0.204 floored)
    assert "220x220x250" in message                 # the bed it was judged against
    assert "'Portal 220'" in message
    assert "0.25" in message                        # the floor
    assert "250x250x250" in message                 # what to re-export


def test_shrink_off_refuses_a_model_that_cannot_fit(tmp_path: Path) -> None:
    big = write_model(tmp_path, "big.stl", 300.0, 300.0, 300.0)
    machine = write_machine(tmp_path, "Portal 220", 220.0)

    with pytest.raises(fit.FitError) as excinfo:
        fit.plan_fit(big, machine, shrink_to_fit=False)

    message = str(excinfo.value)
    assert "300x300x300" in message and "220x220x250" in message

    # 210 mm spans fit the 220 mm bed raw, so the 16 mm margin is not consulted here
    snug = write_model(tmp_path, "snug.stl", 210.0, 210.0, 210.0)
    assert fit.plan_fit(snug, machine, shrink_to_fit=False).scale == 1.0


def test_degenerate_geometry_is_refused(tmp_path: Path) -> None:
    flat = write_model(tmp_path, "flat.stl", 50.0, 50.0, 0.0)

    with pytest.raises(fit.FitError) as excinfo:
        fit.plan_fit(flat, FIXTURES / "preset-datadir" / MACHINE_220, shrink_to_fit=True)
    assert "flat.stl" in str(excinfo.value)

    thin_bed = prof.BedLimits(0.0, 0.0, 10.0, 10.0, 250.0, "Tiny")
    with pytest.raises(fit.FitError):
        fit.scale_factor(mesh.BoundingBox(0.0, 0.0, 0.0, 5.0, 5.0, 5.0), thin_bed)


def test_bed_numbers_come_from_the_preset(tmp_path: Path) -> None:
    model = write_model(tmp_path, "cube.stl", 300.0, 300.0, 300.0)

    roomy = write_machine(tmp_path, "Roomy", 350.0, 350.0)
    plan = fit.plan_fit(model, roomy, shrink_to_fit=True)
    assert plan.scale == 1.0 and plan.bed.size == (350.0, 350.0, 350.0)

    tight = write_machine(tmp_path, "Tight", 220.0)
    assert fit.plan_fit(model, tight, shrink_to_fit=True).scale == 0.68

    # the low bed binds on Z, which carries no margin: 60 / 300 = 0.20, under the floor
    cramped = write_machine(tmp_path, "Cramped", 100.0, 60.0)
    with pytest.raises(fit.FitError) as excinfo:
        fit.plan_fit(model, cramped, shrink_to_fit=True)
    message = str(excinfo.value)
    assert "0.20" in message and "0.25" in message and "75x75x75" in message


def test_scale_is_floored_never_rounded_up() -> None:
    bbox = mesh.BoundingBox(0.0, 0.0, 0.0, 299.56, 100.0, 100.0)

    assert fit.scale_factor(bbox, BED_220) == 0.68      # 0.6809987… floors, never 0.681


def test_plan_fit_propagates_mesh_errors(tmp_path: Path) -> None:
    project = tmp_path / "part.3mf"
    project.write_bytes(b"<not read: the suffix alone is refused>")

    with pytest.raises(mesh.MeshError):
        fit.plan_fit(project, FIXTURES / "preset-datadir" / MACHINE_220, shrink_to_fit=False)
