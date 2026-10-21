"""Jobs pipeline: output-name normalisation, the sliced dir, gcode reads, bulk delete.

``storage`` gives every test its own ``JOBS``/``SLICED`` pair inside ``tmp_path`` by
monkeypatching ``main.JOBS``/``main.SLICED``; the printer is stubbed through
``main.current_client`` where a print would otherwise leave the process. ``TestClient`` is
built without its context manager, so the lifespan never starts the slicing worker.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    jobs = tmp_path / "jobs"
    sliced = tmp_path / "sliced"
    jobs.mkdir()
    sliced.mkdir()
    monkeypatch.setattr(main, "JOBS", jobs)
    monkeypatch.setattr(main, "SLICED", sliced)
    return jobs, sliced


@pytest.fixture
def api_client(storage) -> TestClient:
    return TestClient(main.app)


def upload(client: TestClient, blob: bytes, filename: str = "cube.stl",
           output_name: str | None = None):
    data = {} if output_name is None else {"output_name": output_name}
    return client.post("/api/upload",
                       files={"file": (filename, blob, "model/stl")}, data=data)


def read_job(jobs: Path, jid: str) -> dict:
    return json.loads((jobs / jid / "job.json").read_text())


def make_job(jobs: Path, jid: str, status: str, output_name: str = "cube") -> Path:
    """A job directory the delete-all endpoint has to classify, nothing more."""
    directory = jobs / jid
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(json.dumps({
        "id": jid, "status": status, "filename": "cube.stl", "output_name": output_name,
    }))
    return directory


def sliced_job(jobs: Path, sliced: Path, jid: str = "decafbad1234",
               output_name: str = "Benchy v2") -> Path:
    """A finished job whose gcode lives in the sliced dir, as run_slice leaves it."""
    make_job(jobs, jid, "sliced", output_name)
    (sliced / jid).mkdir(parents=True)
    (sliced / jid / f"{output_name}.gcode").write_text("; total filament used [g] = 3.94\n")
    (jobs / jid / "job.json").write_text(json.dumps({
        "id": jid, "status": "sliced", "filename": "cube.stl", "output_name": output_name,
        "gcode": f"{output_name}.gcode", "gcode_dir": "sliced",
    }))
    return jobs / jid


class StubClient:
    """Stand-in for ``main.current_client()``: records the upload and the start call."""

    def __init__(self) -> None:
        self.uploaded: list[tuple[Path, str | None]] = []
        self.started: list[str] = []

    def printer_status(self) -> dict:
        return {"webhooks": "ready", "state": "standby"}

    def upload(self, path: Path, remote_name: str | None = None) -> dict:
        self.uploaded.append((Path(path), remote_name))
        return {"ok": True}

    def start_print(self, filename: str) -> dict:
        self.started.append(filename)
        return {"ok": True}


# ---------------------------------------------------------------- output name
def test_upload_stores_the_output_name(storage, api_client, cube_stl):
    jobs, _sliced = storage

    jid = upload(api_client, cube_stl, output_name="  Benchy v2  ").json()["id"]

    job = read_job(jobs, jid)
    assert job["output_name"] == "Benchy v2"
    assert job["filename"] == "cube.stl"          # the upload name is kept for display
    assert job["status"] == "queued"


def test_upload_without_output_name_uses_the_model_stem(storage, api_client, cube_stl):
    jobs, _sliced = storage

    jid = upload(api_client, cube_stl, filename="dragon.3mf").json()["id"]

    assert read_job(jobs, jid)["output_name"] == "dragon"


def test_upload_strips_a_gcode_suffix(storage, api_client, cube_stl):
    jobs, _sliced = storage

    jid = upload(api_client, cube_stl, output_name="cube.GCODE").json()["id"]

    assert read_job(jobs, jid)["output_name"] == "cube"


@pytest.mark.parametrize("name", ["../evil", "a/b", "a\\b", "..", ".hidden", "CON",
                                 "x" * 61])
def test_upload_rejects_separators_and_overlong_names(storage, api_client, cube_stl, name):
    jobs, _sliced = storage

    response = upload(api_client, cube_stl, output_name=name)

    assert response.status_code == 400
    assert response.json()["detail"]
    assert list(jobs.iterdir()) == []          # nothing half-created


def test_upload_rejects_unknown_characters(storage, api_client, cube_stl):
    jobs, _sliced = storage

    response = upload(api_client, cube_stl, output_name="benchy:2")

    assert response.status_code == 400
    assert "':'" in response.json()["detail"]
    assert list(jobs.iterdir()) == []


def test_upload_accepts_the_maximum_length(storage, api_client, cube_stl):
    jobs, _sliced = storage

    jid = upload(api_client, cube_stl, output_name="a" * main.OUTPUT_NAME_MAX).json()["id"]

    assert read_job(jobs, jid)["output_name"] == "a" * main.OUTPUT_NAME_MAX


# ---------------------------------------------------------------- slicing
def test_sliced_output_moves_into_the_sliced_dir(storage, api_client, cube_stl, datadir,
                                                 monkeypatch):
    jobs, sliced = storage
    jid = upload(api_client, cube_stl, output_name="Benchy v2").json()["id"]

    # run_slice writes the derived preset into the datadir: keep it out of the session copy.
    monkeypatch.setattr(main, "CONFIG",
                        dataclasses.replace(main.CONFIG, orca_datadir=datadir))

    def fake_run(cmd, **kwargs):
        out = Path(kwargs["cwd"]) / "out"
        (out / "cube.gcode").write_text("; total filament used [g] = 3.94\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main.run_slice(jid)

    job = read_job(jobs, jid)
    assert job["status"] == "sliced"
    assert job["gcode"] == "Benchy v2.gcode"
    assert job["gcode_dir"] == "sliced"
    assert job["gcode_bytes"] == (sliced / jid / "Benchy v2.gcode").stat().st_size
    assert (sliced / jid / "Benchy v2.gcode").read_text().startswith("; total filament")
    assert list((jobs / jid / "out").glob("*.gcode")) == []     # moved, not copied


# ---------------------------------------------------------------- gcode reads
def test_gcode_download_reads_the_sliced_dir(storage, api_client):
    _jobs, sliced = storage
    sliced_job(*storage, jid="decafbad1234", output_name="Benchy v2")

    response = api_client.get("/api/jobs/decafbad1234/gcode")

    assert response.status_code == 200
    assert response.text.startswith("; total filament")
    assert "Benchy%20v2.gcode" in response.headers["content-disposition"]
    assert (sliced / "decafbad1234" / "Benchy v2.gcode").is_file()


def test_gcode_download_falls_back_to_the_legacy_job_dir(storage, api_client):
    jobs, _sliced = storage
    directory = make_job(jobs, "decafbad1234", "sliced")
    (directory / "out").mkdir()
    (directory / "out" / "cube.gcode").write_text("G28\n")
    (directory / "job.json").write_text(json.dumps({
        "id": "decafbad1234", "status": "sliced", "filename": "cube.stl",
        "gcode": "cube.gcode"}))

    response = api_client.get("/api/jobs/decafbad1234/gcode")

    assert response.status_code == 200
    assert response.text == "G28\n"
    assert "cube.gcode" in response.headers["content-disposition"]   # stem of filename


def test_gcode_path_ignores_a_traversal_name(storage):
    _jobs, sliced = storage
    (sliced / "decafbad1234").mkdir()
    (sliced / "decafbad1234" / "passwd").write_text("secret\n")

    path = main.gcode_path("decafbad1234", {"gcode": "../../../etc/passwd"})

    assert path == sliced / "decafbad1234" / "passwd"
    assert path.parent == sliced / "decafbad1234"
    assert main.gcode_path("decafbad1234", {"gcode": "missing.gcode"}) is None
    assert main.gcode_path("decafbad1234", {}) is None


# ---------------------------------------------------------------- delete
def test_delete_job_removes_the_sliced_output(storage, api_client):
    jobs, sliced = storage
    sliced_job(jobs, sliced, jid="decafbad1234")

    response = api_client.delete("/api/jobs/decafbad1234")

    assert response.status_code == 200
    assert response.json() == {"deleted": "decafbad1234"}
    assert not (jobs / "decafbad1234").exists()
    assert not (sliced / "decafbad1234").exists()


def test_delete_all_removes_finished_jobs_and_skips_active(storage, api_client):
    jobs, sliced = storage
    sliced_job(jobs, sliced, jid="cafebabe0001", output_name="cube")
    make_job(jobs, "cafebabe0002", "printed")
    (sliced / "cafebabe0002").mkdir()
    make_job(jobs, "cafebabe0003", "queued")
    make_job(jobs, "cafebabe0004", "slicing")

    response = api_client.delete("/api/jobs")

    assert response.status_code == 200
    assert sorted(p.name for p in jobs.iterdir()) == ["cafebabe0003", "cafebabe0004"]
    assert not (sliced / "cafebabe0001").exists()
    assert not (sliced / "cafebabe0002").exists()


def test_delete_all_returns_counts(storage, api_client):
    jobs, sliced = storage
    sliced_job(jobs, sliced, jid="cafebabe0001", output_name="cube")
    make_job(jobs, "cafebabe0002", "printed")
    make_job(jobs, "cafebabe0003", "queued")
    make_job(jobs, "cafebabe0004", "slicing")

    body = api_client.delete("/api/jobs").json()

    assert set(body) == {"deleted", "skipped", "deleted_ids", "skipped_ids"}
    assert body["deleted"] == 2
    assert body["skipped"] == 2
    assert body["deleted_ids"] == ["cafebabe0001", "cafebabe0002"]
    assert body["skipped_ids"] == ["cafebabe0003", "cafebabe0004"]


def test_delete_all_leaves_the_data_dir_root_untouched(storage, api_client):
    jobs, sliced = storage
    sliced_job(jobs, sliced, jid="cafebabe0001")
    (jobs / "notes.txt").write_text("hand-written\n")
    (jobs / "uploads").mkdir()
    (sliced / "keep.gcode").write_text("G28\n")

    api_client.delete("/api/jobs")

    assert jobs.is_dir() and sliced.is_dir()
    assert (jobs / "notes.txt").is_file()
    assert (jobs / "uploads").is_dir()
    assert (sliced / "keep.gcode").is_file()
    assert sorted(p.name for p in jobs.iterdir()) == ["notes.txt", "uploads"]


# ---------------------------------------------------------------- remote name
def test_remote_name_uses_the_output_name(storage, api_client, monkeypatch):
    jobs, sliced = storage
    sliced_job(jobs, sliced, jid="decafbad1234", output_name="Benchy v2")
    stub = StubClient()
    monkeypatch.setattr(main, "current_client", lambda: stub)

    response = api_client.post("/api/jobs/decafbad1234/print")

    assert response.status_code == 200
    remote_name = response.json()["remote_file"]
    assert remote_name == "portal_decafbad1234_Benchy v2.gcode"
    assert len(remote_name) <= 90
    assert stub.uploaded == [(sliced / "decafbad1234" / "Benchy v2.gcode", remote_name)]
    assert stub.started == [remote_name]
    assert read_job(jobs, "decafbad1234")["remote_file"] == remote_name


def test_remote_name_falls_back_to_the_upload_stem(storage, api_client, monkeypatch):
    jobs, sliced = storage
    directory = make_job(jobs, "decafbad1234", "sliced")
    (directory / "out").mkdir()
    (directory / "out" / "cube.gcode").write_text("G28\n")
    (directory / "job.json").write_text(json.dumps({
        "id": "decafbad1234", "status": "sliced", "filename": "cube.stl",
        "gcode": "cube.gcode"}))
    stub = StubClient()
    monkeypatch.setattr(main, "current_client", lambda: stub)

    body = api_client.post("/api/jobs/decafbad1234/print").json()

    assert body["remote_file"] == "portal_decafbad1234_cube.gcode"
    assert stub.uploaded[0][0] == jobs / "decafbad1234" / "out" / "cube.gcode"
    assert sliced == main.SLICED        # the legacy fallback never copied anything
