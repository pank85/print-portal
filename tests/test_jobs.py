"""Jobs pipeline: output-name normalisation, the sliced dir, gcode reads, bulk delete.

``storage`` gives every test its own ``JOBS``/``SLICED`` pair inside ``tmp_path`` by
monkeypatching ``main.JOBS``/``main.SLICED``; the printer is stubbed through
``main.current_client`` where a print would otherwise leave the process. ``TestClient`` is
built without its context manager, so the lifespan never starts the slicing worker.
"""
from __future__ import annotations

import dataclasses
import io
import json
import queue
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
import profiles as prof


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


@pytest.fixture
def switchable(tmp_path: Path, datadir: Path, printers_file: Path,
               monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Private datadir + profile.json + printer store for the upload switch pipeline.

    Uploading a 3MF writes ``profile.json`` and may change the selected printer, so these
    tests must not share either with the rest of the suite (or with ``presets/``).
    """
    cfg = dataclasses.replace(main.CONFIG, orca_datadir=datadir,
                              profile_file=tmp_path / "profile.json")
    monkeypatch.setattr(main, "CONFIG", cfg)
    monkeypatch.setattr(main, "SEED", None)
    return SimpleNamespace(datadir=datadir, profile_file=cfg.profile_file, store=main.STORE)


def selected_id(client: TestClient) -> str | None:
    return client.get("/api/printers").json()["selected_id"]


def add_printer(client: TestClient, name: str, profiles: dict | None = None) -> str:
    """Add a printer and return its id; ``profiles=None`` leaves it unpaired."""
    body: dict = {"name": name, "url": f"http://{len(name)}.invalid:7125",
                  "api_key": "k-0123456789abcdef"}
    if profiles is not None:
        body["profiles"] = profiles
    response = client.post("/api/printers", json=body)
    assert response.status_code == 201, response.text
    return response.json()["printer"]["id"]


def add_process_preset(datadir: Path, filename: str, name: str,
                       compatible: list[str] | None = None) -> str:
    """A process preset in the private datadir; returns its datadir-relative path."""
    path = datadir / "system" / "Custom" / "process" / filename
    fields: dict = {"name": name, "type": "process", "instantiation": "true"}
    if compatible is not None:
        fields["compatible_printers"] = compatible
    path.write_text(json.dumps(fields, indent="\t"))
    return path.relative_to(datadir).as_posix()


def permits(datadir: Path, machine_rel: str, process_rel: str) -> bool:
    """Whether the preset picker would offer ``process_rel`` for ``machine_rel``."""
    machine = json.loads((datadir / machine_rel).read_text())
    process = json.loads((datadir / process_rel).read_text())
    return any(row["rel"] == process_rel
               for row in prof.filter_process_by_machine([{**process, "rel": process_rel}],
                                                        machine))


def three_mf_member(blob: bytes, member: str = "Metadata/project_settings.config") -> dict:
    """The JSON identity member of a 3MF, read back for assertions."""
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return json.loads(archive.read(member))


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


# ---------------------------------------------------------------- zip upload
MACHINE_REL = prof.DEFAULT_PROFILE["machine"]
PROCESS_REL = prof.DEFAULT_PROFILE["process"]
FILAMENT_REL = prof.DEFAULT_PROFILE["filament"]
OTHER_MACHINE_REL = "system/Custom/machine/generic-klipper.json"


def stored_profile(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def test_upload_zip_queues_the_extracted_model(storage, api_client, switchable, cube_stl,
                                              zip_bytes):
    jobs, _sliced = storage
    archive = zip_bytes({"models/cube.stl": cube_stl, "readme.txt": b"print me\n"})

    response = upload(api_client, archive, filename="parts.zip")

    assert response.status_code == 200, response.text
    jid = response.json()["id"]
    job = read_job(jobs, jid)
    assert job["status"] == "queued"
    assert job["filename"] == "cube.stl"            # the member, not the archive
    assert job["output_name"] == "cube"             # the member's stem
    assert job["upload_bytes"] == len(archive)      # the archive size, for display
    assert job["model_bytes"] == len(cube_stl)
    assert (jobs / jid / "upload" / "parts.zip").is_file()      # kept for provenance
    assert (jobs / jid / "upload" / "cube.stl").read_bytes() == cube_stl


def test_upload_zip_reports_candidates_and_skipped(storage, api_client, switchable, cube_stl,
                                                   zip_bytes):
    jobs, _sliced = storage
    archive = zip_bytes({"a/cube.stl": cube_stl, "b/part.obj": b"o part\n",
                         "notes.txt": b"nothing to slice\n"})

    body = upload(api_client, archive, filename="parts.zip").json()

    record = read_job(jobs, body["id"])["archive"]
    assert record["name"] == "parts.zip"
    assert record["member"] == "a/cube.stl"
    assert record["candidates"] == ["a/cube.stl", "b/part.obj"]
    assert record["skipped"] == ["notes.txt", "b/part.obj"]     # shallowest first
    assert record["members"] == 3
    assert body["archive"] == record


def test_upload_zip_traversal_is_a_400_without_a_job_dir(storage, api_client, switchable,
                                                         cube_stl, zip_bytes_with):
    jobs, _sliced = storage
    archive = zip_bytes_with({"cube.stl": cube_stl}, traversal_name="../evil.stl")

    response = upload(api_client, archive, filename="evil.zip")

    assert response.status_code == 400
    assert "not safe" in response.json()["detail"]
    assert list(jobs.iterdir()) == []


def test_failed_3mf_preparation_leaves_no_job_dir(storage, api_client, switchable,
                                                  three_mf_bytes, monkeypatch):
    """The job dir exists before `prepare_3mf` runs, so a failure there must remove it."""
    jobs, _sliced = storage

    def explode(_path: Path) -> tuple[dict, str | None]:
        raise MemoryError("no room for the model")

    monkeypatch.setattr(main, "prepare_3mf", explode)

    with pytest.raises(MemoryError):
        upload(api_client, three_mf_bytes(), filename="benchy.3mf")

    assert list(jobs.iterdir()) == []


# ---------------------------------------------------------------- 3mf upload
def test_upload_zip_with_3mf_runs_the_switch_pipeline(storage, api_client, switchable,
                                                    three_mf_bytes, zip_bytes):
    jobs, _sliced = storage
    klipper = add_printer(api_client, "Generic Klipper 0.4 nozzle")
    archive = zip_bytes({"models/benchy.3mf": three_mf_bytes()})

    response = upload(api_client, archive, filename="benchy-pack.zip")

    assert response.status_code == 200, response.text
    jid = response.json()["id"]
    job = read_job(jobs, jid)
    assert job["archive"]["member"] == "models/benchy.3mf"
    assert job["filename"] == "benchy.3mf"
    assert job["output_name"] == "benchy"
    assert job["threemf"]["printer"] == klipper
    assert job["threemf"]["rewrite"] == "identity"
    assert job["slicer_input"] == "slice-benchy.3mf"
    assert (jobs / jid / "upload" / "slice-benchy.3mf").is_file()
    assert stored_profile(switchable.profile_file)["machine"] == MACHINE_REL
    assert selected_id(api_client) == klipper


def test_upload_3mf_switches_printer_and_process(storage, api_client, switchable,
                                                 three_mf_bytes):
    jobs, _sliced = storage
    voron = add_printer(api_client, "Voron 2.4")
    klipper = add_printer(api_client, "Generic Klipper 0.4 nozzle")
    api_client.post(f"/api/printers/{voron}/select")
    switchable.profile_file.write_text(json.dumps({"machine": OTHER_MACHINE_REL,
                                                  "machine_label": "Generic Klipper Printer"}))

    response = upload(api_client, three_mf_bytes(), filename="benchy.3mf")

    assert response.status_code == 200, response.text
    assert selected_id(api_client) == klipper
    profile = stored_profile(switchable.profile_file)
    assert profile["machine"] == MACHINE_REL
    assert profile["process"] == PROCESS_REL
    assert profile["machine_label"] == "Generic Klipper 0.4 nozzle"
    job = read_job(jobs, response.json()["id"])
    assert job["threemf"]["printer"] == klipper
    assert job["threemf"]["process"] == PROCESS_REL


def test_upload_3mf_falls_back_to_the_machines_default_process(storage, api_client, switchable,
                                                              three_mf_bytes):
    """A file naming a local machine but a foreign process still gets a local process.

    The fixture machine preset declares ``default_print_profile``; when the
    3MF's `print_settings_id` matches no preset at all, that default is what the portal
    slices with, instead of keeping the foreign name and failing with `-17`.
    """
    jobs, _sliced = storage
    klipper = add_printer(api_client, "Generic Klipper 0.4 nozzle")
    foreign_process = three_mf_bytes(process="0.30mm Draft @Voron")

    response = upload(api_client, foreign_process, filename="voron-export.3mf")

    assert response.status_code == 200, response.text
    job = read_job(jobs, response.json()["id"])
    profile = stored_profile(switchable.profile_file)
    assert job["threemf"]["process"] == PROCESS_REL
    assert job["threemf"]["printer"] == klipper
    assert profile["machine"] == MACHINE_REL
    assert profile["process"] == PROCESS_REL
    assert not [note for note in job["threemf"]["notes"] if "not configured here" in note]


def test_upload_3mf_with_unmatched_machine_keeps_the_process(storage, api_client, switchable,
                                                             three_mf_bytes):
    """A matched process with an unmatched machine must not be switched to on its own.

    `match_process` scores by process name even when the machine is unknown, so the half
    match used to write a process for whatever machine profile.json happened to hold —
    poisoning this upload and every later STL upload. The printer is left alone instead.
    """
    jobs, _sliced = storage
    add_printer(api_client, "Voron 2.4")
    universal = add_process_preset(switchable.datadir, "universal.json", "0.20mm Universal")
    switchable.profile_file.write_text(json.dumps({"machine": MACHINE_REL,
                                                   "machine_label": "Generic Klipper 0.4 nozzle",
                                                   "process": PROCESS_REL},
                                                  indent="\t"))
    before = switchable.profile_file.read_bytes()
    foreign = three_mf_bytes(printer="Bambu X1 Carbon", printer_model="Bambu X1",
                             process="0.20mm Universal")

    response = upload(api_client, foreign, filename="bambu.3mf")

    assert response.status_code == 200, response.text
    job = read_job(jobs, response.json()["id"])
    assert switchable.profile_file.read_bytes() == before      # "left alone", byte for byte
    assert job["threemf"]["process"] == universal             # reported, not persisted
    assert [note for note in job["threemf"]["notes"]
            if "0.20mm Universal" in note and "not applied" in note]


def test_upload_3mf_never_persists_an_incompatible_process(storage, api_client, switchable,
                                                           three_mf_bytes):
    """A local process that names another machine is replaced by one the machine permits."""
    jobs, _sliced = storage
    add_printer(api_client, "Generic Klipper 0.4 nozzle")
    voron_only = add_process_preset(switchable.datadir, "voron-only.json", "0.20mm Voron",
                                    compatible=["Voron 2.4 350"])
    assert not permits(switchable.datadir, MACHINE_REL, voron_only)
    blob = three_mf_bytes(process="0.20mm Voron")

    response = upload(api_client, blob, filename="voron.3mf")

    assert response.status_code == 200, response.text
    job = read_job(jobs, response.json()["id"])
    profile = stored_profile(switchable.profile_file)
    assert profile["machine"] == MACHINE_REL
    assert profile["process"] == PROCESS_REL
    assert permits(switchable.datadir, profile["machine"], profile["process"])
    assert [note for note in job["threemf"]["notes"] if "not compatible" in note]


def test_upload_3mf_rewrites_identity_to_the_persisted_process(storage, api_client, switchable,
                                                             three_mf_bytes):
    """The job-local copy must name the process the slicer will load, not the 3MF's.

    The 3MF's own process is matched by name but that machine does not permit it; stamping
    it into the copy would bring back the `-17` the rewrite exists to remove.
    """
    jobs, _sliced = storage
    add_printer(api_client, "Generic Klipper 0.4 nozzle")
    add_process_preset(switchable.datadir, "voron-only.json", "0.20mm Voron",
                       compatible=["Voron 2.4 350"])
    blob = three_mf_bytes(process="0.20mm Voron")

    jid = upload(api_client, blob, filename="voron.3mf").json()["id"]

    job = read_job(jobs, jid)
    assert stored_profile(switchable.profile_file)["process"] == PROCESS_REL
    identity = three_mf_member((jobs / jid / "upload" / job["slicer_input"]).read_bytes())
    assert identity["print_settings_id"] == ["0.20mm Standard @Generic Klipper"]
    assert "0.20mm Voron" not in identity["print_settings_id"]
    assert identity["printer_settings_id"] == ["Generic Klipper 0.4 nozzle"]


def test_upload_3mf_without_a_permitted_process_stamps_no_process_identity(
        storage, api_client, switchable, three_mf_bytes):
    """Machine matched, nothing permitted: no copy may claim a process profile.json lacks."""
    jobs, _sliced = storage
    add_printer(api_client, "Generic Klipper 0.4 nozzle")
    add_process_preset(switchable.datadir, "voron-only.json", "0.20mm Voron",
                       compatible=["Voron 2.4 350"])
    # the stored process preset is gone, so neither the 3MF's nor the current process is
    # permitted by the matched machine
    switchable.profile_file.write_text(json.dumps({"machine": MACHINE_REL,
                                                  "machine_label": "Generic Klipper 0.4 nozzle",
                                                  "process": "system/Custom/process/gone.json"}))
    before = switchable.profile_file.read_bytes()
    blob = three_mf_bytes(process="0.20mm Voron")

    jid = upload(api_client, blob, filename="voron.3mf").json()["id"]

    job = read_job(jobs, jid)
    assert switchable.profile_file.read_bytes() == before    # the stored pairing is kept
    assert job["threemf"]["rewrite"] == "none"
    assert "slicer_input" not in job
    assert list((jobs / jid / "upload").glob("slice-*.3mf")) == []
    uploaded = three_mf_member((jobs / jid / "upload" / "voron.3mf").read_bytes())
    assert uploaded["print_settings_id"] == ["0.20mm Voron"]     # the 3MF's own, unstamped
    assert [note for note in job["threemf"]["notes"] if "kept the stored pairing" in note]


def test_upload_3mf_survives_an_unreadable_stored_process(storage, api_client, switchable,
                                                          three_mf_bytes):
    """A stored process the datadir cannot parse must fall back, not fail the upload.

    `summarise` describes such a file as ``{"name": ..., "error": ...}``: no ``rel`` to
    persist, and — having no ``compatible_printers`` either — a row the machine filter
    calls universal. Treating it as permitted made `prepare_3mf` persist it and raise
    `KeyError` on the missing ``rel``, turning the upload into a 500.
    """
    jobs, _sliced = storage
    add_printer(api_client, "Generic Klipper 0.4 nozzle")
    (switchable.datadir / PROCESS_REL).write_text("{not json")      # the stored process
    switchable.profile_file.write_text(json.dumps({"machine": MACHINE_REL,
                                                  "machine_label": "Generic Klipper 0.4 nozzle",
                                                  "process": PROCESS_REL}))
    before = switchable.profile_file.read_bytes()

    response = upload(api_client, three_mf_bytes(), filename="benchy.3mf")

    assert response.status_code == 200, response.text
    assert switchable.profile_file.read_bytes() == before    # the stored pairing is kept
    job = read_job(jobs, response.json()["id"])
    assert [note for note in job["threemf"]["notes"] if "kept the stored pairing" in note]


def test_upload_3mf_prefers_the_printer_paired_with_the_machine(storage, api_client,
                                                                switchable, three_mf_bytes):
    _jobs, _sliced = storage
    fuzzy = add_printer(api_client, "Generic Klipper Farm")      # shares two name tokens
    paired = add_printer(api_client, "Shop Floor Voron", {"machine": MACHINE_REL})

    upload(api_client, three_mf_bytes(), filename="benchy.3mf")

    assert selected_id(api_client) == paired
    assert fuzzy != paired


def test_upload_3mf_with_unknown_printer_keeps_the_selection_and_notes_it(
        storage, api_client, switchable, three_mf_bytes):
    jobs, _sliced = storage
    voron = add_printer(api_client, "Voron 2.4")
    api_client.post(f"/api/printers/{voron}/select")
    foreign = three_mf_bytes(printer="Bambu X1 Carbon", printer_model="Bambu X1",
                             process="0.30mm Draft @Bambu X1C",
                             filaments=("Bambu PLA Basic",))

    response = upload(api_client, foreign, filename="bambu.3mf")

    assert response.status_code == 200, response.text
    assert selected_id(api_client) == voron
    job = read_job(jobs, response.json()["id"])
    assert job["threemf"]["printer"] is None
    assert "Bambu X1 Carbon" in job["threemf"]["notes"][0]
    assert "not configured here" in job["threemf"]["notes"][0]
    assert job["filename"] == "bambu.3mf"          # nothing to rewrite: the upload is sliced
    assert "slicer_input" not in job
    assert not switchable.profile_file.exists()    # no local preset matched, nothing switched


def test_upload_3mf_writes_an_identity_rewritten_slicer_input(storage, api_client, switchable,
                                                              three_mf_bytes):
    jobs, _sliced = storage
    add_printer(api_client, "Generic Klipper 0.4 nozzle")
    blob = three_mf_bytes()

    jid = upload(api_client, blob, filename="benchy.3mf").json()["id"]

    job = read_job(jobs, jid)
    assert job["threemf"]["rewrite"] == "identity"
    assert job["slicer_input"] == "slice-benchy.3mf"
    sliced_input = jobs / jid / "upload" / "slice-benchy.3mf"
    identity = three_mf_member(sliced_input.read_bytes())
    assert identity["printer_settings_id"] == ["Generic Klipper 0.4 nozzle"]
    assert identity["print_settings_id"] == ["0.20mm Standard @Generic Klipper"]
    assert identity["compatible_printers"] == ["Generic Klipper 0.4 nozzle"]
    assert (jobs / jid / "upload" / "benchy.3mf").read_bytes() == blob   # original untouched


def test_finning_happens_before_prepare_and_drops_the_rewritten_copy(
        storage, api_client, switchable, monkeypatch, three_mf_bytes):
    """The CLI's 3MF carries no slicer metadata, so it must be finned *before* prepare.

    Preparing the finned copy could never name a printer/process, and keeping the copy
    would leave a ``slice-*.3mf`` that ``run_slice`` prefers over the finned file.
    """
    jobs, _sliced = storage
    add_printer(api_client, "Generic Klipper 0.4 nozzle")
    seen: dict = {}

    def fake_fin_model(src: Path, dest: Path, timeout: float | None = None) -> dict:
        seen["src"] = Path(src)
        Path(dest).write_bytes(b"PK\x03\x04finned")
        return {"finTriangles": 0}

    monkeypatch.setattr(main.supportfins, "fin_model", fake_fin_model)

    response = api_client.post(
        "/api/upload", data={"support_fins": "true"},
        files={"file": ("benchy.3mf", three_mf_bytes(), "model/3mf")})

    assert response.status_code == 200, response.text
    jid = response.json()["id"]
    assert seen["src"].name == "benchy.3mf"        # the untouched upload, not a prepared copy
    job = read_job(jobs, jid)
    assert job["filename"] == "fins-benchy.3mf"
    assert job["threemf"]["rewrite"] == "discarded"
    assert any("support fins rebuilt the 3MF" in note for note in job["threemf"]["notes"])
    assert "slicer_input" not in job               # nothing stale for run_slice to prefer
    assert sorted(p.name for p in (jobs / jid / "upload").iterdir()) == [
        "benchy.3mf", "fins-benchy.3mf"]


def test_upload_3mf_records_a_profile_snapshot(storage, api_client, switchable,
                                               three_mf_bytes):
    jobs, _sliced = storage
    add_printer(api_client, "Generic Klipper 0.4 nozzle")

    jid = upload(api_client, three_mf_bytes(), filename="benchy.3mf").json()["id"]

    assert read_job(jobs, jid)["profile_snapshot"] == {
        "machine": MACHINE_REL, "process": PROCESS_REL, "filament": FILAMENT_REL,
        "options": {"supports": False, "brim": True, "hide_profiles": False,
                    "auto_orient": True, "shrink_to_fit": False},
    }


# ---------------------------------------------------------------- slicing a snapshot
def test_run_slice_prefers_the_snapshot_and_slicer_input(storage, switchable, monkeypatch):
    jobs, _sliced = storage
    datadir = switchable.datadir
    jid = "decafbad1234"
    (jobs / jid / "upload").mkdir(parents=True)
    (jobs / jid / "upload" / "cube.stl").write_text("solid cube\n")
    (jobs / jid / "upload" / "slice-benchy.3mf").write_bytes(b"PK\x03\x04rewritten")
    (jobs / jid / "job.json").write_text(json.dumps({
        "id": jid, "status": "queued", "filename": "cube.stl", "output_name": "cube",
        "slicer_input": "slice-benchy.3mf",
        "profile_snapshot": {"machine": MACHINE_REL, "process": PROCESS_REL,
                             "filament": FILAMENT_REL,
                             "options": {"supports": True, "brim": False,
                                         "hide_profiles": False}},
    }))
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        (Path(kwargs["cwd"]) / "out" / "cube.gcode").write_text("; total filament used [g] = 1\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main.run_slice(jid)

    cmd = seen["cmd"]
    assert cmd[-1] == str(jobs / jid / "upload" / "slice-benchy.3mf")
    machine, process = cmd[cmd.index("--load-settings") + 1].split(";")
    assert machine == str((datadir / MACHINE_REL).resolve())
    assert process == str(datadir / prof.PORTAL_PROCESS_REL)
    assert cmd[cmd.index("--load-filaments") + 1] == str((datadir / FILAMENT_REL).resolve())
    derived = json.loads((datadir / prof.PORTAL_PROCESS_REL).read_text())
    assert derived["enable_support"] == "1"        # the snapshot's options, not the live ones
    assert derived["brim_width"] == "0"
    assert read_job(jobs, jid)["status"] == "sliced"


def test_run_slice_holds_the_derived_lock_across_the_launch(storage, switchable, monkeypatch):
    """An option save must not rewrite the derived preset while the slicer is launching.

    `run_slice` writes the shared derived preset, builds the argv around it and runs the
    slicer that loads it; `regenerate_derived` takes the same lock for its own rewrite. A
    save landing inside that window would slice with the live options instead of this
    job's snapshot, so the second thread here has to stay outside until the launch ends.
    """
    jobs, _sliced = storage
    datadir = switchable.datadir
    jid = "decafbad1234"
    (jobs / jid / "upload").mkdir(parents=True)
    (jobs / jid / "upload" / "cube.stl").write_text("solid cube\n")
    (jobs / jid / "job.json").write_text(json.dumps({
        "id": jid, "status": "queued", "filename": "cube.stl", "output_name": "cube",
        "profile_snapshot": {"machine": MACHINE_REL, "process": PROCESS_REL,
                             "filament": FILAMENT_REL,
                             "options": {"supports": False, "brim": True,
                                         "hide_profiles": False}},
    }))
    attempting = threading.Event()     # the save has reached the critical section
    applied = threading.Event()        # ... and has rewritten the shared preset
    from_saver = {"active": False}
    launch: dict = {}

    def fake_write_derived(*_args, **_kwargs):
        if from_saver["active"]:
            applied.set()
        return str(datadir / prof.PORTAL_PROCESS_REL)

    def concurrent_save() -> None:
        from_saver["active"] = True
        attempting.set()
        main.regenerate_derived({"supports": True})

    saver = threading.Thread(target=concurrent_save, daemon=True)

    def fake_run(cmd, **kwargs):
        # observed rather than asserted: `run_slice` turns anything the slicer raises into
        # a failed job, which would hide the reason
        launch["lock_held"] = main.DERIVED_LOCK.locked()
        saver.start()
        launch["attempted"] = attempting.wait(5)
        launch["entered_early"] = applied.wait(0.2)
        (Path(kwargs["cwd"]) / "out" / "cube.gcode").write_text(
            "; total filament used [g] = 1\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(prof, "write_derived_process", fake_write_derived)
    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main.run_slice(jid)

    assert launch, "the slicer was never launched"
    assert launch["lock_held"], "the slicer launch runs outside DERIVED_LOCK"
    assert launch["attempted"], "the concurrent save never reached the lock"
    assert not launch["entered_early"], "an option save rewrote the preset mid-launch"
    saver.join(5)
    assert not saver.is_alive()
    assert applied.is_set()               # released with the launch, not leaked
    assert read_job(jobs, jid)["status"] == "sliced"


# ---------------------------------------------------------------- fitting the bed
FIT_SNAPSHOT_OPTIONS = {"supports": False, "brim": True, "hide_profiles": False}


def orientation(cmd: list[str]) -> str | None:
    """The value after ``--orient``, or None when the flag is absent."""
    return cmd[cmd.index("--orient") + 1] if "--orient" in cmd else None


def scale_of(cmd: list[str]) -> str | None:
    """The value after ``--scale``, or None when the flag is absent."""
    return cmd[cmd.index("--scale") + 1] if "--scale" in cmd else None


def queued_job(jobs: Path, blob: bytes, *, filename: str = "cube.stl",
               slicer_input: str | None = None, options: dict | None = None,
               jid: str = "decafbad1234") -> str:
    """A queued job holding ``blob`` and a snapshot of the private datadir's presets."""
    upload_dir = jobs / jid / "upload"
    upload_dir.mkdir(parents=True)
    (upload_dir / (slicer_input or filename)).write_bytes(blob)
    meta = {"id": jid, "status": "queued", "filename": filename,
            "output_name": Path(filename).stem,
            "profile_snapshot": {"machine": MACHINE_REL, "process": PROCESS_REL,
                                 "filament": FILAMENT_REL,
                                 "options": {**FIT_SNAPSHOT_OPTIONS, **(options or {})}}}
    if slicer_input is not None:
        meta["slicer_input"] = slicer_input
    (jobs / jid / "job.json").write_text(json.dumps(meta))
    return jid


def scaled_cube(cube: bytes, factor: float) -> bytes:
    """The 20 mm cube fixture with every vertex scaled: ``factor=15`` is 300 mm a side."""
    lines = []
    for line in cube.decode().splitlines():
        parts = line.split()
        if parts[:1] == ["vertex"]:
            line = "      vertex " + " ".join(f"{float(v) * factor:g}" for v in parts[1:4])
        lines.append(line)
    return ("\n".join(lines) + "\n").encode()


def record_slice(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]]) -> None:
    """Stand in for the CLI: record every argv and leave the gcode it would have written."""
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        (Path(kwargs["cwd"]) / "out" / "cube.gcode").write_text(
            "; total filament used [g] = 1\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)


def test_a_plain_job_adds_no_new_flags(storage, switchable, monkeypatch, cube_stl):
    """Defaults — orient on, shrink off — and a model that fits: the argv keeps its shape."""
    jobs, _sliced = storage
    jid = queued_job(jobs, cube_stl)
    calls: list[list[str]] = []
    record_slice(monkeypatch, calls)

    main.run_slice(jid)

    cmd = calls[0]
    assert orientation(cmd) == "1"
    assert scale_of(cmd) is None
    assert cmd[-1] == str(jobs / jid / "upload" / "cube.stl")
    job = read_job(jobs, jid)
    assert job["auto_orient"] is True
    assert job["auto_orient_used"] is True
    assert job["shrink_to_fit"] is False
    assert job["fit_scale"] == 1.0
    assert job["model_size"] == {"x": 20.0, "y": 20.0, "z": 20.0}


def test_auto_orient_off_passes_no_orient_flag(storage, switchable, monkeypatch, cube_stl):
    jobs, _sliced = storage
    jid = queued_job(jobs, cube_stl, options={"auto_orient": False})
    calls: list[list[str]] = []
    record_slice(monkeypatch, calls)

    main.run_slice(jid)

    cmd = calls[0]
    assert "--orient" not in cmd
    assert "--scale" not in cmd
    assert cmd[-1] == str(jobs / jid / "upload" / "cube.stl")
    assert read_job(jobs, jid)["auto_orient_used"] is False


def test_shrink_off_refuses_a_model_too_big_for_the_bed(storage, switchable, monkeypatch,
                                                       cube_stl):
    jobs, _sliced = storage
    jid = queued_job(jobs, scaled_cube(cube_stl, 15))
    calls: list[list[str]] = []
    record_slice(monkeypatch, calls)

    main.run_slice(jid)

    assert calls == []                       # refused before the CLI is ever launched
    job = read_job(jobs, jid)
    assert job["status"] == "failed"
    assert "300x300x300" in job["error"]
    assert "220x220x250" in job["error"]


def test_shrink_on_scales_and_forces_orient(storage, switchable, monkeypatch, cube_stl):
    """A shrink forces orient: ``--scale`` without ``--orient 1`` does not slice at all."""
    jobs, _sliced = storage
    jid = queued_job(jobs, scaled_cube(cube_stl, 15),
                     options={"shrink_to_fit": True, "auto_orient": False})
    calls: list[list[str]] = []
    record_slice(monkeypatch, calls)

    main.run_slice(jid)

    cmd = calls[0]
    assert cmd[cmd.index("--orient"):] == [
        "--orient", "1", "--scale", "0.68", "--outputdir",
        str(jobs / jid / "out"), str(jobs / jid / "upload" / "cube.stl")]
    job = read_job(jobs, jid)
    assert job["fit_scale"] == 0.68
    assert job["auto_orient"] is False
    assert job["auto_orient_used"] is True


@pytest.mark.parametrize("auto_orient, orient_flag", [(True, "1"), (False, None)])
def test_shrink_on_a_model_that_fits_passes_no_scale(storage, switchable, monkeypatch,
                                                     cube_stl, auto_orient, orient_flag):
    jobs, _sliced = storage
    jid = queued_job(jobs, cube_stl,
                     options={"shrink_to_fit": True, "auto_orient": auto_orient})
    calls: list[list[str]] = []
    record_slice(monkeypatch, calls)

    main.run_slice(jid)

    cmd = calls[0]
    assert scale_of(cmd) is None             # k == 1: no --scale 1 either
    assert orientation(cmd) == orient_flag
    assert read_job(jobs, jid)["fit_scale"] == 1.0


def test_a_3mf_with_shrink_off_slices_as_before(storage, switchable, monkeypatch,
                                               three_mf_bytes):
    """An unmeasurable 3MF stays advisory: OrcaSlicer orients it without a bbox from us."""
    jobs, _sliced = storage
    jid = queued_job(jobs, three_mf_bytes(), filename="benchy.3mf",
                     slicer_input="slice-benchy.3mf")
    calls: list[list[str]] = []
    record_slice(monkeypatch, calls)

    main.run_slice(jid)

    cmd = calls[0]
    assert orientation(cmd) == "1"
    assert scale_of(cmd) is None
    assert cmd[-1] == str(jobs / jid / "upload" / "slice-benchy.3mf")
    assert read_job(jobs, jid)["status"] == "sliced"


def test_a_3mf_with_shrink_on_fails_with_both_escapes(storage, switchable, monkeypatch,
                                                     three_mf_bytes):
    jobs, _sliced = storage
    jid = queued_job(jobs, three_mf_bytes(), filename="benchy.3mf",
                     slicer_input="slice-benchy.3mf", options={"shrink_to_fit": True})
    calls: list[list[str]] = []
    record_slice(monkeypatch, calls)

    main.run_slice(jid)

    assert calls == []
    job = read_job(jobs, jid)
    assert job["status"] == "failed"
    assert "3D/3dmodel.model" in job["error"]
    assert "turn shrink to fit off" in job["error"]
    assert "STL" in job["error"]
    assert "import a preset" not in job["error"]        # a geometry problem, not a preset one


def test_run_slice_refuses_a_finned_job_needing_shrink(storage, switchable, monkeypatch, cube_stl):
    jobs, _sliced = storage
    jid = "beefcafe1234"
    upload_dir = jobs / jid / "upload"
    upload_dir.mkdir(parents=True)
    (upload_dir / "cube.stl").write_bytes(scaled_cube(cube_stl, 15))
    (jobs / jid / "job.json").write_text(json.dumps({
        "id": jid, "status": "queued", "filename": "cube.stl",
        "output_name": "cube", "support_fins": True,
        "profile_snapshot": {"machine": MACHINE_REL, "process": PROCESS_REL,
                             "filament": FILAMENT_REL,
                             "options": {"auto_orient": True, "shrink_to_fit": True,
                                         "supports": False, "brim": False,
                                         "hide_profiles": False}},
    }))
    calls: list[list[str]] = []
    record_slice(monkeypatch, calls)

    main.run_slice(jid)

    assert calls == []
    job = read_job(jobs, jid)
    assert job["status"] == "failed"
    assert job["auto_orient_used"] is False
    assert "cannot be auto-oriented safely" in job["error"]
    assert "Disable shrink to fit" in job["error"]
    assert "scale the model" in job["error"]
    assert "manually" in job["error"]


def test_a_preset_without_printable_area_still_slices_when_shrink_is_off(
        storage, switchable, monkeypatch, cube_stl):
    """A preset predating ``printable_area`` cannot be measured; without shrink that is advisory."""
    jobs, _sliced = storage
    preset = switchable.datadir / MACHINE_REL
    machine = json.loads(preset.read_text())
    del machine["printable_area"]
    preset.write_text(json.dumps(machine))
    calls: list[list[str]] = []
    record_slice(monkeypatch, calls)

    main.run_slice(queued_job(jobs, cube_stl))

    assert orientation(calls[0]) == "1"
    assert read_job(jobs, "decafbad1234")["status"] == "sliced"

    launched = len(calls)
    jid = queued_job(jobs, cube_stl, jid="beefcafe1234", options={"shrink_to_fit": True})
    main.run_slice(jid)

    assert len(calls) == launched            # a shrink needs the bed, so it refuses
    job = read_job(jobs, jid)
    assert job["status"] == "failed"
    assert "no printable_area" in job["error"]
    assert "Generic Klipper 0.4 nozzle" in job["error"]
    # a preset problem: the escapes name the preset, not the upload format
    assert "import a preset with printable_area" in job["error"]
    assert "export the model as STL" not in job["error"]


def test_upload_zip_oversize_member_is_a_413_without_a_job_dir(storage, api_client,
                                                               switchable, zip_bytes):
    jobs, _sliced = storage
    # compresses to a few KB, expands past MAX_UPLOAD_MB=1: the member cap is on *written*
    archive = zip_bytes({"huge.stl": b"\0" * (2 * 1024 * 1024)})

    response = upload(api_client, archive, filename="huge.zip")

    assert response.status_code == 413
    assert "expands to more than" in response.json()["detail"]
    assert list(jobs.iterdir()) == []


# ---------------------------------------------------------------- staged (support fins) jobs
@pytest.fixture
def queue_watch(monkeypatch: pytest.MonkeyPatch) -> "queue.Queue[str]":
    """A private ``main.job_queue`` so these ids never reach the shared worker."""
    watched: "queue.Queue[str]" = queue.Queue()
    monkeypatch.setattr(main, "job_queue", watched)
    return watched


def staged_job(jobs: Path, jid: str = "fadedcab1234") -> str:
    """A job the fins pipeline left in ``awaiting_confirm``, as ``api_upload`` writes it."""
    upload_dir = jobs / jid / "upload"
    upload_dir.mkdir(parents=True)
    (upload_dir / "cube.stl").write_text("solid cube\n")
    (upload_dir / "fins-cube.3mf").write_bytes(b"PK\x03\x04finned")
    (jobs / jid / "job.json").write_text(json.dumps({
        "id": jid, "status": "awaiting_confirm", "filename": "fins-cube.3mf",
        "output_name": "cube", "original": "cube.stl", "support_fins": True,
        "fins": {"report": "r", "stats": {"finTriangles": 12}},
        "profile_snapshot": {"machine": MACHINE_REL, "process": PROCESS_REL,
                             "filament": FILAMENT_REL,
                             "options": {"supports": False, "brim": False,
                                         "hide_profiles": False}},
    }))
    return jid


def test_a_fins_upload_is_staged_not_queued(storage, api_client, queue_watch, monkeypatch,
                                            cube_stl):
    jobs, _sliced = storage

    def fake_fin_model(src: Path, dest: Path, timeout: float | None = None) -> dict:
        Path(dest).write_bytes(cube_stl)
        return {"report": "r", "stats": {"finTriangles": 12}}

    monkeypatch.setattr(main.supportfins, "fin_model", fake_fin_model)

    response = api_client.post("/api/upload", data={"support_fins": "true"},
                               files={"file": ("cube.stl", cube_stl, "model/stl")})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "awaiting_confirm"
    assert "queue_position" not in body      # staged: the dialog's *Slice it* is the entry
    assert queue_watch.empty()
    assert read_job(jobs, body["id"])["status"] == "awaiting_confirm"


def test_confirming_a_staged_job_queues_it_exactly_once(storage, api_client, queue_watch):
    jobs, _sliced = storage
    jid = staged_job(jobs)

    first = api_client.post(f"/api/jobs/{jid}/slice")

    assert first.status_code == 200, first.text
    assert first.json() == {"id": jid, "status": "queued", "queue_position": 1}
    assert queue_watch.qsize() == 1
    assert read_job(jobs, jid)["status"] == "queued"

    again = api_client.post(f"/api/jobs/{jid}/slice")

    assert again.status_code == 409
    assert "not awaiting confirmation" in again.json()["detail"]
    assert queue_watch.qsize() == 1          # a double click is not a second slice


def test_confirm_refuses_a_job_that_was_never_staged(storage, api_client, queue_watch):
    jobs, _sliced = storage
    make_job(jobs, "aaaabbbb0002", "queued")

    response = api_client.post("/api/jobs/aaaabbbb0002/slice")

    assert response.status_code == 409
    assert "job is 'queued', not awaiting confirmation" in response.json()["detail"]
    assert queue_watch.empty()


def test_run_slice_slices_the_finned_3mf(storage, switchable, monkeypatch):
    jobs, _sliced = storage
    jid = staged_job(jobs)
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        (Path(kwargs["cwd"]) / "out" / "cube.gcode").write_text(
            "; total filament used [g] = 1\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main.run_slice(jid)

    # the finned copy is the slicer input; the untouched upload is left alone
    assert seen["cmd"][-1] == str(jobs / jid / "upload" / "fins-cube.3mf")
    assert "--orient" not in seen["cmd"]
    assert read_job(jobs, jid)["status"] == "sliced"
    assert (jobs / jid / "upload" / "cube.stl").read_text() == "solid cube\n"
