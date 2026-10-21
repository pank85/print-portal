"""main.py API: health without a printer, upload limits, job storage, error mapping.

The client fixture is a plain ``TestClient(main.app)`` (no ``with``), so the lifespan never
starts the slicing worker and an uploaded job stays ``queued``. Printer answers are faked
either by monkeypatching ``urllib.request.urlopen`` (real client, real error mapping) or by
swapping ``main.current_client`` for a stub (so the endpoint logic sees a chosen state).
"""
from __future__ import annotations

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

    main.prune()

    assert queued.is_dir() and slicing.is_dir()             # never pruned, however old
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
