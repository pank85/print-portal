"""main.py API: health without a printer, upload limits, job storage, error mapping.

The client fixture is a plain ``TestClient(main.app)`` (no ``with``), so the lifespan never
starts the slicing worker and an uploaded job stays ``queued``. Printer answers are faked
either by monkeypatching ``urllib.request.urlopen`` (real client, real error mapping) or by
swapping ``main.current_client`` for a stub (so the endpoint logic sees a chosen state).
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

import config
import main
import moonraker
import preview
import supportfins


class StubPrinter:
    """Stand-in for main.current_client: answers from a canned status or raises a canned error."""

    def __init__(self, status: dict | None = None, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.uploaded: list[tuple[Path, str | None]] = []
        self.started: list[str] = []

    def printer_status(self) -> dict:
        if self.error is not None:
            raise self.error
        return self.status

    def upload(self, path: Path, remote_name: str | None = None) -> dict:
        self.uploaded.append((Path(path), remote_name))
        return {"ok": True}

    def start_print(self, filename: str) -> dict:
        self.started.append(filename)
        return {"ok": True}


def sliced_job(jobs_dir: Path, jid: str = "decafbad1234", filename: str = "cube.stl") -> Path:
    """A finished job on disk, as run_slice() would have left it.

    ``jid`` has the 12-hex shape the API accepts, so the URL routes reach the job at all.
    """
    job = jobs_dir / jid
    (job / "out").mkdir(parents=True)
    (job / "out" / "cube.gcode").write_text("; total filament used [g] = 3.94\n")
    (job / "job.json").write_text(json.dumps({
        "id": jid, "status": "sliced", "filename": filename,
        "gcode": "cube.gcode", "stats": {"grams": 3.94},
    }))
    return job


def read_job_file(jobs_dir: Path, jid: str) -> dict:
    return json.loads((jobs_dir / jid / "job.json").read_text())


# ---------------------------------------------------------------- health
def test_health_answers_without_touching_the_printer(client, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("health must not talk to the printer")

    monkeypatch.setattr(moonraker.MoonrakerClient, "printer_status", refuse)
    monkeypatch.setattr(moonraker.MoonrakerClient, "server_info", refuse)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok", "app": "print-portal", "version": config.VERSION}


# ---------------------------------------------------------------- upload
def test_upload_stores_the_model_and_queues_a_job(client, jobs_dir, cube_stl):
    response = client.post(
        "/api/upload", files={"file": ("cube.stl", cube_stl, "model/stl")})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["queue_position"] >= 1
    jid = body["id"]

    assert (jobs_dir / jid / "upload" / "cube.stl").read_bytes() == cube_stl
    job = read_job_file(jobs_dir, jid)
    assert job["status"] == "queued"          # no lifespan -> the worker never ran
    assert job["filename"] == "cube.stl"
    assert job["upload_bytes"] == len(cube_stl)
    assert not (jobs_dir / jid / "out").exists()   # no slicer subprocess was spawned


def test_upload_rejects_an_unsupported_extension(client, jobs_dir):
    response = client.post(
        "/api/upload", files={"file": ("notes.txt", b"hello", "text/plain")})

    assert response.status_code == 400
    assert ".txt" in response.json()["detail"]
    assert list(jobs_dir.iterdir()) == []


def test_upload_rejects_an_oversize_body(client, jobs_dir):
    assert main.CONFIG.max_upload_bytes == 1024 * 1024     # conftest: MAX_UPLOAD_MB=1
    oversize = b"x" * (main.CONFIG.max_upload_bytes + 64 * 1024)

    response = client.post(
        "/api/upload", files={"file": ("big.stl", oversize, "model/stl")})

    assert response.status_code == 413
    assert "1 MB" in response.json()["detail"]
    assert list(jobs_dir.iterdir()) == []                  # the partial job dir is removed


# ---------------------------------------------------------------- jobs
def test_jobs_are_listed_newest_first(client, cube_stl):
    first = client.post(
        "/api/upload", files={"file": ("one.stl", cube_stl, "model/stl")}).json()["id"]
    second = client.post(
        "/api/upload", files={"file": ("two.stl", cube_stl, "model/stl")}).json()["id"]

    rows = client.get("/api/jobs").json()

    assert [row["id"] for row in rows] == [second, first]
    assert {row["filename"] for row in rows} == {"one.stl", "two.stl"}


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/deadbeef").status_code == 404


def test_gcode_of_an_unsliced_job_is_409(client, cube_stl):
    jid = client.post(
        "/api/upload", files={"file": ("cube.stl", cube_stl, "model/stl")}).json()["id"]

    response = client.get(f"/api/jobs/{jid}/gcode")

    assert response.status_code == 409
    assert "not sliced" in response.json()["detail"]


# ---------------------------------------------------------------- delete
@pytest.mark.parametrize("jid", ["%2E%2E", "%2E", "zzzzzzzzzzzz", "ABCDEF123456", "decafbad12345"])
def test_delete_refuses_an_id_the_app_did_not_generate(client, jobs_dir, data_dir, jid):
    """`..`, `.` and any other non-12-hex id must not reach the filesystem at all.

    The percent-encoded spellings are what a raw `curl --path-as-is` sends: bare dot
    segments are collapsed (to `/api` and `/api/jobs`) before a normal client sends them.
    """
    marker = jobs_dir / "keep-me.txt"
    marker.write_text("precious")
    before = sorted(p.name for p in data_dir.iterdir())

    response = client.delete(f"/api/jobs/{jid}")

    assert response.status_code == 404
    assert response.json()["detail"] == "job not found"
    assert marker.exists()                              # nothing inside JOBS was removed
    assert sorted(p.name for p in data_dir.iterdir()) == before
    assert data_dir.is_dir()


@pytest.mark.parametrize("jid", ["..", ".", "", "zzzzzzzzzzzz", "ABCDEF123456"])
def test_job_dir_checks_the_id_shape_before_the_filesystem(jid):
    """The guard sits in job_dir(), so every caller inherits it — not just DELETE."""
    with pytest.raises(HTTPException) as excinfo:
        main.job_dir(jid)

    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------- prune
def job_with_status(jobs_dir: Path, jid: str, status: str, mtime: float) -> Path:
    job = jobs_dir / jid
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"id": jid, "status": status}))
    os.utime(job, (mtime, mtime))
    return job


def test_prune_keeps_active_jobs_and_survives_a_corrupt_job_json(jobs_dir, monkeypatch):
    monkeypatch.setattr(main, "JOBS", jobs_dir)
    now = time.time()
    finished = [job_with_status(jobs_dir, f"{index:012x}", "sliced", now - 100 - index)
                for index in range(7)]                       # CONFIG.keep_jobs is 5
    queued = job_with_status(jobs_dir, "aaaaaaaaaa01", "queued", now - 900)
    slicing = job_with_status(jobs_dir, "aaaaaaaaaa02", "slicing", now - 900)
    corrupt = jobs_dir / "aaaaaaaaaa03"
    corrupt.mkdir()
    (corrupt / "job.json").write_text("{not json")
    os.utime(corrupt, (now - 1000, now - 1000))
    staged = job_with_status(jobs_dir, "aaaaaaaaaa04", "awaiting_confirm", now - 950)

    main.prune()

    assert queued.is_dir() and slicing.is_dir()             # never pruned, however old
    assert staged.is_dir()                                  # staged waits for the operator
    assert not corrupt.is_dir()                             # unreadable job.json is prunable
    assert [d for d in finished if d.is_dir()] == finished[:5]
    assert [d for d in finished[5:] if d.is_dir()] == []


# ---------------------------------------------------------------- profiles
_BUNDLE_PRESET = {"name": "vendor 0.28", "inherits": "Vendor Profile", "layer_height": "0.28"}


def _bundle_zip(preset: dict = _BUNDLE_PRESET) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("process/vendor.json", json.dumps(preset))
    return buf.getvalue()


@pytest.mark.parametrize("name, blob", [
    ("garbage.orca_printer", b"this is not a zip file at all"),
    ("truncated.orca_printer", _bundle_zip()[: 60]),
    ("broken.json", b'{"name": "half a preset"'),
])
def test_profile_upload_of_an_unreadable_bundle_is_400(client, name, blob):
    response = client.post(
        "/api/profile/upload", files={"file": (name, blob, "application/octet-stream")})

    assert response.status_code == 400
    assert "not a readable bundle" in response.json()["detail"]


# ---------------------------------------------------------------- printer (real client)
def test_printer_offline_is_a_200_with_an_actionable_error(client, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    response = client.get("/api/printer")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["state"] == "offline"
    assert "unreachable" in body["error"]
    assert body["host"] == main.CONFIG.printer_url


def test_printer_reports_the_embedded_ui_url_from_config(client, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    # the UI URL rides along on every answer, online or not: the Printer tab needs it to build
    # the frame, and it must not depend on Moonraker being reachable
    monkeypatch.setattr(
        main, "CONFIG",
        dataclasses.replace(main.CONFIG, printer_ui_url="http://printer.local/fluidd"),
    )

    body = client.get("/api/printer").json()

    assert body["ui_url"] == "http://printer.local/fluidd"


def test_printer_omits_no_ui_url_when_the_variable_is_unset(client, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    body = client.get("/api/printer").json()

    # an empty string, never a missing key: the page reads one shape and shows the empty state
    assert body["ui_url"] == ""


def test_printer_with_a_rejected_key_is_a_200_with_the_api_key_hint(client, monkeypatch):
    def unauthorized(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "http://moonraker.invalid:7125/server/info", 401, "Unauthorized", {},
            io.BytesIO(b'{"error": {"code": 401, "message": "Unauthorized"}}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", unauthorized)

    body = client.get("/api/printer").json()

    assert body["ok"] is False
    assert body["state"] == "offline"
    assert "API key" in body["error"]


def test_printer_status_maps_a_live_answer(client, monkeypatch):
    status = {
        "print_stats": {"state": "printing", "filename": "cube.gcode",
                        "print_duration": 120.0, "message": ""},
        "webhooks": {"state": "ready"},
        "virtual_sdcard": {"progress": 0.5, "file_path": "/gcodes/cube.gcode"},
        "extruder": {"temperature": 210.44, "target": 210.0},
        "heater_bed": {"temperature": 60.04, "target": 60.0},
        "toolhead": {"homed_axes": "xyz"},
    }

    monkeypatch.setattr(
        moonraker.MoonrakerClient, "server_info", lambda self: {"klippy_state": "ready"})
    monkeypatch.setattr(moonraker.MoonrakerClient, "query_objects",
                        lambda self, objects=None: status)

    body = client.get("/api/printer").json()

    assert body["ok"] is True
    assert body["state"] == "printing"
    assert body["webhooks"] == "ready"
    assert body["file"] == "cube.gcode"
    assert body["progress"] == 50.0
    assert body["duration_min"] == 2.0
    assert body["extruder"] == {"temp": 210.4, "target": 210.0}
    assert body["bed"] == {"temp": 60.0, "target": 60.0}
    assert body["homed"] == "xyz"
    assert body["mcu_version"] is None


# ---------------------------------------------------------------- print gate
def test_print_requires_a_sliced_job(client, jobs_dir, cube_stl):
    jid = client.post(
        "/api/upload", files={"file": ("cube.stl", cube_stl, "model/stl")}).json()["id"]

    response = client.post(f"/api/jobs/{jid}/print")

    assert response.status_code == 409
    assert "not sliced" in response.json()["detail"]


def test_print_maps_an_unreachable_printer_to_503(client, jobs_dir, monkeypatch):
    sliced_job(jobs_dir)
    monkeypatch.setattr(main, "current_client", lambda: StubPrinter(
        error=moonraker.MoonrakerUnreachable(
            "printer unreachable at http://moonraker.invalid:7125")))

    response = client.post("/api/jobs/decafbad1234/print")

    assert response.status_code == 503
    assert "unreachable" in response.json()["detail"]


class FakeMoonrakerWire:
    """Real ``MoonrakerClient`` over a fake urlopen: GETs answer, the gcode POST gets a 401."""

    class Response:
        def __init__(self, payload: dict) -> None:
            self._body = json.dumps(payload).encode()

        def __enter__(self) -> "FakeMoonrakerWire.Response":
            return self

        def __exit__(self, *_exc) -> bool:
            return False

        def read(self) -> bytes:
            return self._body

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), urllib.parse.urlsplit(req.full_url).path))
        if req.get_method() == "POST":
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {},
                io.BytesIO(b'{"error": {"code": 401, "message": "Unauthorized"}}'))
        return self.Response({"result": {"status": {
            "webhooks": {"state": "ready"}, "print_stats": {"state": "standby"}}}})


def test_print_maps_a_rejected_api_key_to_502(client, jobs_dir, monkeypatch):
    """A 401 on the gcode upload surfaces as 502 with the client's own hint text."""
    sliced_job(jobs_dir)
    wire = FakeMoonrakerWire()
    monkeypatch.setattr(urllib.request, "urlopen", wire)

    response = client.post("/api/jobs/decafbad1234/print")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail.startswith("Moonraker rejected the API key (HTTP 401)")
    assert "docs/printer-setup.md" in detail           # produced by moonraker.py, not by the test
    assert ("POST", "/server/files/upload") in wire.calls   # the upload is what was rejected
    job = read_job_file(jobs_dir, "decafbad1234")
    assert job["print_started"] is False
    assert "HTTP 401" in job["print_error"]


def test_print_refuses_while_klipper_is_not_ready(client, jobs_dir, monkeypatch):
    sliced_job(jobs_dir)
    monkeypatch.setattr(main, "current_client", lambda: StubPrinter(
        status={"webhooks": "startup", "state": "standby"}))

    response = client.post("/api/jobs/decafbad1234/print")

    assert response.status_code == 409
    assert "klipper not ready: startup" in response.json()["detail"]


def test_print_refuses_while_the_printer_is_busy(client, jobs_dir, monkeypatch):
    sliced_job(jobs_dir)
    monkeypatch.setattr(main, "current_client", lambda: StubPrinter(
        status={"webhooks": "ready", "state": "printing"}))

    response = client.post("/api/jobs/decafbad1234/print")

    assert response.status_code == 409
    assert "printer busy: state=printing" in response.json()["detail"]


def test_print_uploads_then_starts_the_gcode(client, jobs_dir, monkeypatch):
    sliced_job(jobs_dir)
    stub = StubPrinter(status={"webhooks": "ready", "state": "standby"})
    monkeypatch.setattr(main, "current_client", lambda: stub)

    response = client.post("/api/jobs/decafbad1234/print")

    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    remote_name = body["remote_file"]
    assert remote_name == "portal_decafbad1234_cube.gcode"
    assert stub.uploaded == [(jobs_dir / "decafbad1234" / "out" / "cube.gcode", remote_name)]
    assert stub.started == [remote_name]

    job = read_job_file(jobs_dir, "decafbad1234")
    assert job["print_started"] is True
    assert job["remote_file"] == remote_name
    assert job["print_error"] is None


def test_print_failure_after_the_upload_is_recorded_on_the_job(client, jobs_dir, monkeypatch):
    sliced_job(jobs_dir)
    stub = StubPrinter(status={"webhooks": "ready", "state": "standby"})

    def missing(filename: str) -> dict:
        raise moonraker.MoonrakerFileMissing(
            f"file did not land in the 'gcodes' root: {filename}")

    stub.start_print = missing                # type: ignore[method-assign]
    monkeypatch.setattr(main, "current_client", lambda: stub)

    response = client.post("/api/jobs/decafbad1234/print")

    assert response.status_code == 502
    assert "did not land" in response.json()["detail"]
    job = read_job_file(jobs_dir, "decafbad1234")
    assert job["print_started"] is False
    assert "did not land" in job["print_error"]


def test_print_on_an_unknown_job_is_404(client):
    assert client.post("/api/jobs/deadbeef/print").status_code == 404


# ---------------------------------------------------------------- ui tests (F4)
def test_env_seed_bootstraps_the_first_printer(client):
    r = client.get("/api/printers")
    assert r.status_code == 200
    body = r.json()
    printers = body["printers"]
    env_rows = [p for p in printers if p["id"] == "env"]
    assert len(env_rows) == 1
    p = env_rows[0]
    assert p["source"] == "env"
    assert p["api_key_masked"] is not None
    assert "•" in p["api_key_masked"]
    assert main.current_target().id == "env"


def test_store_printer_takes_over_from_the_env_seed(client, monkeypatch):
    r = client.post("/api/printers", json={"name": "store1", "url": "http://store.invalid", "api_key": "store-key"})
    assert r.status_code == 201
    pid = r.json()["printer"]["id"]
    client.post(f"/api/printers/{pid}/select")
    body = client.get("/api/printers").json()
    assert body["selected_id"] == pid
    ids = [p["id"] for p in body["printers"]]
    assert "env" in ids
    # current_target uses store printer
    assert main.current_target().url == "http://store.invalid"


def test_index_page_has_no_external_dependencies(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    text = r.text
    for bad in ['src="http', 'href="http', "cdn.", "@import", "fonts.googleapis"]:
        assert bad not in text, bad


def test_settings_view_ids_are_present(client):
    text = client.get("/").text
    for vid in ["viewSettings", "printerForm", "btnDeleteAll", "outputName", "printerList",
                "pfMachine", "pfProcess"]:
        assert f'id="{vid}"' in text


def test_index_page_never_references_main_client_state(client):
    import main as m
    assert not hasattr(m, "client")


def test_ui_route_answers_without_a_printer_at_all(client, monkeypatch):
    """The embedded-UI URL must not ride on a payload that fails when nothing is configured.

    /api/printer is the natural home for it, but it answers 503 when no printer exists and
    when Moonraker is unreachable — and "printer offline" is precisely when the user opens
    the Printer tab. /api/ui exists so the tab can tell "not configured" apart from "cannot
    reach the printer".
    """
    monkeypatch.setattr(main, "active_target", lambda: None)
    monkeypatch.setattr(main, "CONFIG", dataclasses.replace(main.CONFIG,
        printer_ui_url="http://printer.local/fluidd"))

    response = client.get("/api/ui")

    assert response.status_code == 200
    assert response.json() == {"printer_ui_url": "http://printer.local/fluidd"}


def test_ui_route_reports_empty_when_unset(client):
    assert client.get("/api/ui").json() == {"printer_ui_url": main.CONFIG.printer_ui_url}


# ---------------------------------------------------------------- support fins staging
_FINS_MODEL = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<model unit="millimeter" '
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
    '<resources>'
    '<object id="1" type="model" name="cube"><mesh>'
    '<vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>'
    '<vertex x="0" y="10" z="0"/></vertices>'
    '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
    '<object id="2" type="model" name="overhang fin"><mesh>'
    '<vertices><vertex x="0" y="0" z="0"/><vertex x="4" y="0" z="0"/>'
    '<vertex x="0" y="4" z="0"/></vertices>'
    '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
    '</resources>'
    '<build><item objectid="1"/><item objectid="2"/></build></model>')


def finned_3mf_bytes() -> bytes:
    """What app/support_fins.mjs writes: a 3MF whose supports object is named for the role."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("3D/3dmodel.model", _FINS_MODEL)
    return buffer.getvalue()


@pytest.fixture
def fake_fins(monkeypatch):
    """Replace the node CLI with a recorder that writes a previewable 3MF."""
    calls: list[tuple[Path, Path, float | None]] = []

    def fin_model(src, dest, timeout=None):
        calls.append((Path(src), Path(dest), timeout))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(finned_3mf_bytes())
        return {"report": "1 fin wall, 2 tines", "stats": {"overhangRegions": 1, "finTriangles": 1,
                "braces": 1, "tines": 2}}

    monkeypatch.setattr(supportfins, "fin_model", fin_model)
    return calls


def stage(client, cube_stl) -> str:
    """Upload with the fins checkbox and return the staged job id."""
    response = client.post("/api/upload", data={"support_fins": "true"},
                           files={"file": ("cube.stl", cube_stl, "model/stl")})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_upload_with_fins_stages_the_job_instead_of_queuing_it(client, jobs_dir, cube_stl,
                                                              fake_fins):
    response = client.post("/api/upload", data={"support_fins": "true"},
                           files={"file": ("cube.stl", cube_stl, "model/stl")})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_confirm"     # the dialog's "Slice it" queues it
    assert "queue_position" not in body
    assert body["original"] == "cube.stl"
    assert body["fins"]["stats"]["tines"] == 2

    jid = body["id"]
    job = read_job_file(jobs_dir, jid)
    assert job["status"] == "awaiting_confirm"
    assert job["support_fins"] is True
    assert job["original"] == "cube.stl"
    assert job["filename"] == "fins-cube.3mf"       # the finned 3MF is the slicer input
    assert (jobs_dir / jid / "upload" / "fins-cube.3mf").read_bytes() == finned_3mf_bytes()
    assert fake_fins[0][2] == main.CONFIG.support_fins_timeout
    assert not (jobs_dir / jid / "out").exists()    # nothing was sliced yet


def test_the_fins_cli_receives_the_uploaded_model_and_the_timeout(client, cube_stl, fake_fins):
    stage(client, cube_stl)

    src, dest, timeout = fake_fins[0]
    assert src.name == "cube.stl"
    assert dest.name == "fins-cube.3mf"
    assert dest.parent == src.parent                 # the finned copy stays in the upload dir
    assert timeout == main.CONFIG.support_fins_timeout


def test_a_fins_failure_is_a_400_and_leaves_no_job_behind(client, jobs_dir, cube_stl,
                                                          monkeypatch):
    def explode(src, dest, timeout=None):
        raise supportfins.SupportFinsError("node exited 1: no overhangs")

    monkeypatch.setattr(supportfins, "fin_model", explode)

    response = client.post("/api/upload", data={"support_fins": "true"},
                           files={"file": ("cube.stl", cube_stl, "model/stl")})

    assert response.status_code == 400
    assert response.json()["detail"] == "support fins failed: node exited 1: no overhangs"
    assert list(jobs_dir.iterdir()) == []            # the half-made job dir was removed


def test_preview_of_a_staged_job_returns_the_scene(client, monkeypatch, cube_stl, fake_fins):
    jid = stage(client, cube_stl)
    monkeypatch.setattr(main, "JOBS", main.JOBS)     # the job dir is the fixture's

    response = client.get(f"/api/jobs/{jid}/preview")

    assert response.status_code == 200
    body = response.json()
    assert body["job"] == jid
    assert body["unit"] == "millimeter"
    assert {obj["role"] for obj in body["objects"]} == {"part", "supports"}
    assert body["original"] == "cube.stl"
    assert body["fins"]["stats"]["tines"] == 2
    assert body["bbox"]["size"] == [10.0, 10.0, 0.0]


def test_preview_of_a_queued_job_is_a_409(client, cube_stl):
    jid = client.post(
        "/api/upload", files={"file": ("cube.stl", cube_stl, "model/stl")}).json()["id"]

    response = client.get(f"/api/jobs/{jid}/preview")

    assert response.status_code == 409
    assert "has no support fins" in response.json()["detail"]


def test_preview_names_an_unreadable_finned_model(client, jobs_dir, cube_stl, fake_fins):
    jid = stage(client, cube_stl)
    (jobs_dir / jid / "upload" / "fins-cube.3mf").write_bytes(b"not a zip")

    response = client.get(f"/api/jobs/{jid}/preview")

    assert response.status_code == 409
    assert response.json()["detail"].startswith("preview unavailable: ")
    assert "is not a readable 3MF archive" in response.json()["detail"]


def test_an_oversized_finned_mesh_returns_pictures(client, jobs_dir, cube_stl, fake_fins, monkeypatch):
    monkeypatch.setattr(preview, "MAX_BYTES", 1)
    jid = stage(client, cube_stl)

    listed = client.get(f"/api/jobs/{jid}/preview")

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["mode"] == "shots"
    assert body["views"] == ["front", "side"]
    assert body["bytes"] == (jobs_dir / jid / "upload" / "fins-cube.3mf").stat().st_size

    picture = client.get(f"/api/jobs/{jid}/preview-shot?view=front")
    assert picture.status_code == 200
    assert picture.headers["content-type"].startswith("image/png")
    assert picture.content.startswith(b"\x89PNG")
    again = client.get(f"/api/jobs/{jid}/preview-shot?view=side")
    assert again.status_code == 200
    assert again.content.startswith(b"\x89PNG")


def test_preview_of_an_unknown_job_is_404(client):
    assert client.get("/api/jobs/aaaaaaaaaaaa/preview").status_code == 404


def test_confirm_slice_queues_a_staged_job_exactly_once(client, jobs_dir, cube_stl, fake_fins):
    jid = stage(client, cube_stl)

    first = client.post(f"/api/jobs/{jid}/slice")
    second = client.post(f"/api/jobs/{jid}/slice")

    assert first.status_code == 200
    assert first.json()["status"] == "queued"
    assert first.json()["queue_position"] >= 1
    assert read_job_file(jobs_dir, jid)["status"] == "queued"
    assert second.status_code == 409                # a double click is not a second entry
    assert "not awaiting confirmation" in second.json()["detail"]


def test_confirm_slice_refuses_a_job_that_was_never_staged(client, cube_stl):
    jid = client.post(
        "/api/upload", files={"file": ("cube.stl", cube_stl, "model/stl")}).json()["id"]

    response = client.post(f"/api/jobs/{jid}/slice")

    assert response.status_code == 409
    assert "job is 'queued'" in response.json()["detail"]


def test_delete_all_still_removes_an_abandoned_staged_job(client, jobs_dir, cube_stl, fake_fins):
    """The bulk cleanup is the escape hatch for a dialog the operator abandoned.

    `main.py:70-76` keeps `awaiting_confirm` out of ACTIVE_STATUSES on purpose: only
    prune() spares a staged job, so "delete finished" can still clear one.
    """
    jid = stage(client, cube_stl)

    body = client.delete("/api/jobs").json()

    assert body["deleted_ids"] == [jid]
    assert body["skipped_ids"] == []
    assert not (jobs_dir / jid).exists()


def test_the_support_fins_endpoint_reports_the_caps(client, monkeypatch):
    monkeypatch.setattr(supportfins, "available", lambda: False)
    monkeypatch.setattr(supportfins, "unavailable_reason",
                        lambda: "node runtime is not available")

    assert client.get("/api/support-fins").json() == {
        "available": False,
        "reason": "node runtime is not available",
        "timeout": main.CONFIG.support_fins_timeout,
        "max_triangles": preview.MAX_TRIANGLES,
    }
