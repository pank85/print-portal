"""Tests verifying raw G-code delivery with media_type="text/plain" and removal of legacy preview endpoints."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


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
G1 X10 Y20 E0.5
;LAYER_CHANGE
;Z:0.68
;HEIGHT:0.2
BEFORE_LAYER_CHANGE LAYER=2
G1 E-1.2 F2400
G1 X10 Y10 F9000
G1 Z0.68
G1 E1.2 F1800
G1 X20 Y10 E0.5
;LAYER_CHANGE
;Z:0.88
;HEIGHT:0.2
BEFORE_LAYER_CHANGE LAYER=3
G1 E-1.2 F2400
G1 X20 Y20 F9000
G1 Z0.88
G1 E1.2 F1800
G1 X10 Y20 E0.5
"""


def _create_sliced_job(
    jobs_dir: Path,
    sliced_dir: Path,
    jid: str = "feedface1234",
    filename: str = "cube.stl",
    gcode_text: str = "; gcode sample\nG1 X10 Y10 E1.0\n",
) -> Path:
    job = jobs_dir / jid
    job.mkdir(parents=True, exist_ok=True)
    stem = Path(filename).stem
    gcode_name = f"{stem}.gcode"
    job_sliced = sliced_dir / jid
    job_sliced.mkdir(parents=True, exist_ok=True)
    (job_sliced / gcode_name).write_text(gcode_text, encoding="utf-8")
    (job / "job.json").write_text(
        json.dumps({
            "id": jid,
            "status": "sliced",
            "filename": filename,
            "output_name": stem,
            "gcode": gcode_name,
            "gcode_dir": "sliced",
            "stats": {"grams": 3.94},
        }),
        encoding="utf-8",
    )
    return job


def test_gcode_preview_module_is_removed() -> None:
    """Verify app.gcode_preview and gcode_preview modules cannot be imported."""
    with pytest.raises(ImportError):
        import gcode_preview  # noqa: F401
    with pytest.raises(ImportError):
        import app.gcode_preview  # noqa: F401


def test_gcode_endpoint_returns_raw_gcode_text_plain(client: TestClient, jobs_dir: Path, sliced_dir: Path) -> None:
    """GET /api/jobs/{jid}/gcode returns raw G-code with media_type='text/plain'."""
    jid = "feedface1234"
    raw_gcode = "; total filament used [g] = 3.94\nG1 X10 Y10 E1.0\n"
    _create_sliced_job(jobs_dir, sliced_dir, jid=jid, filename="cube.stl", gcode_text=raw_gcode)

    res = client.get(f"/api/jobs/{jid}/gcode")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert res.text == raw_gcode
    assert 'filename="cube.gcode"' in res.headers.get("content-disposition", "")


def test_gcode_endpoint_rebased_returns_raw_gcode_text_plain(client: TestClient, jobs_dir: Path, sliced_dir: Path) -> None:
    """GET /api/jobs/{jid}/gcode with layer rebasing returns media_type='text/plain'."""
    jid = "c0ffee123456"
    _create_sliced_job(jobs_dir, sliced_dir, jid=jid, filename="box.stl", gcode_text=SAMPLE_MULTI_LAYER_GCODE)

    res = client.get(f"/api/jobs/{jid}/gcode?from_layer=2")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert 'filename="box_from_layer2_on_bed.gcode"' in res.headers.get("content-disposition", "")
    assert "; total layer number: 2" in res.text
    assert "BEFORE_LAYER_CHANGE LAYER=0" in res.text


def test_gcode_endpoint_unsliced_returns_409(client: TestClient, jobs_dir: Path) -> None:
    """GET /api/jobs/{jid}/gcode for an unsliced job returns 409."""
    jid = "baadf00d1234"
    job = jobs_dir / jid
    job.mkdir(parents=True, exist_ok=True)
    (job / "job.json").write_text(
        json.dumps({
            "id": jid,
            "status": "queued",
            "filename": "model.stl",
        }),
        encoding="utf-8",
    )

    res = client.get(f"/api/jobs/{jid}/gcode")
    assert res.status_code == 409
    assert "not sliced" in res.json().get("detail", "")


def test_legacy_gcode_preview_endpoint_returns_404(client: TestClient, jobs_dir: Path, sliced_dir: Path) -> None:
    """GET /api/jobs/{jid}/gcode-preview returns 404 for sliced and unsliced jobs."""
    jid = "feedface1234"
    _create_sliced_job(jobs_dir, sliced_dir, jid=jid)

    # Removed endpoint must return 404 for existing sliced job
    res = client.get(f"/api/jobs/{jid}/gcode-preview")
    assert res.status_code == 404

    # Removed endpoint must return 404 for unknown job
    unknown_res = client.get("/api/jobs/000000000000/gcode-preview")
    assert unknown_res.status_code == 404


def test_legacy_thumbnail_endpoint_returns_404(client: TestClient, jobs_dir: Path, sliced_dir: Path) -> None:
    """GET /api/jobs/{jid}/thumbnail returns 404 for sliced and unsliced jobs."""
    jid = "feedface1234"
    _create_sliced_job(jobs_dir, sliced_dir, jid=jid)

    # Removed endpoint must return 404 for existing sliced job
    res = client.get(f"/api/jobs/{jid}/thumbnail")
    assert res.status_code == 404

    # Removed endpoint must return 404 for unknown job
    unknown_res = client.get("/api/jobs/000000000000/thumbnail")
    assert unknown_res.status_code == 404
