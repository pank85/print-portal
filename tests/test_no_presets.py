"""A fresh install has no presets: reads answer 200, the three actions that need a machine 409.

The shipped seed carries no preset at all, so every endpoint that used to assume a machine
slot has to answer without one. Reads render the empty state (`imported: false`, null paths,
`options` still populated); the actions that would need a machine to slice — `/api/upload`,
`/api/profile/select`, a printer pairing — refuse with 409 `no_printer_profile` *before* they
write anything, so a refused request leaves no job dir, no printer row and no profile.json.

``fresh_install`` points the app at ``empty_datadir`` (the real seed) and at a
``profile.json`` that does not exist yet, which is exactly the state a new container is in.
"""
from __future__ import annotations

import dataclasses
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import profiles as prof

IMPORTED_MACHINE_REL = "system/Custom/machine/Portal_Test_Printer.json"


@pytest.fixture(autouse=True)
def fresh_install(empty_datadir: Path, tmp_path: Path,
                  monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = dataclasses.replace(main.CONFIG, profile_file=tmp_path / "profile.json",
                              orca_datadir=empty_datadir)
    monkeypatch.setattr(main, "CONFIG", cfg)
    return cfg.profile_file


def write_preset(datadir: Path, kind: str, filename: str, name: str) -> str:
    """One preset file in the datadir, as an import would write it; returns its relative path.

    Exists so the 409 can be reached at all: a selection or pairing naming a path that is
    not a datadir preset is a 400 raised earlier, in ``_profile_rel``. A process-only
    datadir is the fresh-install state that can still *offer* a choice.
    """
    path = datadir / "system" / "Custom" / kind / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": kind, "name": name, "instantiation": "true"}))
    return path.relative_to(datadir).as_posix()


def printer_only_bundle(bundle_bytes: bytes) -> bytes:
    """The committed bundle with its process and filament members dropped.

    A Printer-only bundle is the documented minimum import (README, "First run"), so this
    is the state an operator reaches by doing exactly what the docs say.
    """
    src = zipfile.ZipFile(io.BytesIO(bundle_bytes))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for info in src.infolist():
            if info.filename.split("/", 1)[0] in ("process", "filament"):
                continue
            out.writestr(info, src.read(info.filename))
    return buf.getvalue()


def snapshot(datadir: Path) -> dict[str, int]:
    """Relative path → size, the "did a read write anything" oracle."""
    return {path.relative_to(datadir).as_posix(): path.stat().st_size
            for path in datadir.rglob("*") if path.is_file()}


def no_printer_detail(response) -> dict:
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "no_printer_profile"
    assert detail["detail"] == prof.NO_MACHINE_MESSAGE
    assert "machine" in detail["missing"]
    assert detail["hint"]
    return detail


def test_profiles_endpoint_is_three_empty_lists(client: TestClient):
    response = client.get("/api/profiles")

    assert response.status_code == 200
    assert response.json() == {"machine": [], "process": [], "filament": []}


def test_profiles_endpoint_with_a_machine_filter_stays_200(client: TestClient):
    """A filter naming a preset that is not there degrades to "no filter", never a 500."""
    response = client.get("/api/profiles",
                          params={"machine": "system/Custom/machine/absent.json"})

    assert response.status_code == 200
    assert response.json() == {"machine": [], "process": [], "filament": []}


def test_profile_endpoint_is_200_with_an_empty_selection(client: TestClient):
    response = client.get("/api/profile")

    assert response.status_code == 200
    body = response.json()
    assert body["imported"] is False
    assert body["source"] == "none"
    assert body["machine_label"] is None
    assert body["paths"] == {"machine": None, "process": None, "filament": None}
    assert body["presets"] == {"machine": None, "process": None, "filament": None}
    assert body["options"] == prof.DEFAULT_OPTIONS        # still the stored (default) ones
    assert body["effective"] is None
    assert body["derived"] is None


def test_a_fresh_install_reads_are_200_and_create_no_files(
        client: TestClient, empty_datadir: Path, fresh_install: Path):
    """The two reads the dashboard makes on load must not write a datadir or a profile."""
    before = snapshot(empty_datadir)

    assert client.get("/api/profiles").status_code == 200
    assert client.get("/api/profile").status_code == 200

    assert snapshot(empty_datadir) == before
    assert not (empty_datadir / prof.PORTAL_PROCESS_REL).exists()
    assert not fresh_install.exists()


def test_upload_is_a_409_and_leaves_no_job_dir(client: TestClient, cube_stl: bytes,
                                              jobs_dir: Path):
    response = client.post("/api/upload", files={"file": ("cube.stl", cube_stl)})

    no_printer_detail(response)
    assert list(jobs_dir.iterdir()) == []


def test_profile_select_is_a_409(client: TestClient, empty_datadir: Path,
                                 fresh_install: Path):
    rel = write_preset(empty_datadir, "process", "only-process.json", "Only Process")

    response = client.post("/api/profile/select", json={"process": rel})

    no_printer_detail(response)
    assert not fresh_install.exists()


def test_adding_a_printer_without_a_pairing_still_works(client: TestClient):
    """A connection is not a preset: adding one needs no machine, only a URL and a key."""
    response = client.post("/api/printers", json={"name": "No Pairing",
                                                 "url": "http://elsewhere.invalid:7125",
                                                 "api_key": "k-0123456789abcdef"})

    assert response.status_code == 201, response.text
    assert response.json()["printer"]["name"] == "No Pairing"


def test_adding_a_printer_with_a_pairing_is_a_409(client: TestClient, empty_datadir: Path):
    rel = write_preset(empty_datadir, "process", "only-process.json", "Only Process")

    response = client.post("/api/printers", json={
        "name": "Paired", "url": "http://elsewhere.invalid:7125",
        "api_key": "k-0123456789abcdef", "profiles": {"process": rel}})

    no_printer_detail(response)
    assert "Paired" not in [row["name"] for row in client.get("/api/printers").json()["printers"]]


def test_option_save_does_not_write_the_derived_preset(client: TestClient,
                                                       empty_datadir: Path):
    response = client.post("/api/profile/options", json={"supports": True})

    assert response.status_code == 200
    assert response.json()["options"] == {**prof.DEFAULT_OPTIONS, "supports": True}
    assert not (empty_datadir / prof.PORTAL_PROCESS_REL).exists()


def test_import_through_the_api_completes_the_selection(client: TestClient,
                                                        bundle_bytes: bytes):
    response = client.post("/api/profile/upload",
                           files={"file": ("portal-import.orca_printer", bundle_bytes)})

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    body = client.get("/api/profile").json()
    assert body["imported"] is True
    assert body["paths"]["machine"] == IMPORTED_MACHINE_REL
    assert all(body["paths"].values())          # every slot an import just wrote is selected


def test_a_printer_only_import_leaves_process_and_filament_unselected(
        client: TestClient, cube_stl: bytes, jobs_dir: Path, bundle_bytes: bytes):
    """The documented minimum import must not select presets this install does not have.

    A Printer-only bundle carries no process and no filament, so the selection names neither:
    the profile card renders them as unselected instead of as an unreadable
    `generic-0.20-standard.json` that ships nowhere, and the upload that needs one to slice
    refuses with the same 409 a fresh install gets.
    """
    response = client.post("/api/profile/upload",
                           files={"file": ("printer-only.orca_printer",
                                           printer_only_bundle(bundle_bytes))})
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True

    body = client.get("/api/profile").json()
    assert body["imported"] is True
    assert body["paths"]["machine"] == IMPORTED_MACHINE_REL
    assert body["paths"]["process"] is None
    assert body["presets"]["process"] is None
    assert body["effective"] is None

    refused = client.post("/api/upload", files={"file": ("cube.stl", cube_stl)})
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "no_printer_profile"
    assert "process" in detail["missing"]
    assert detail["detail"] == "no process preset imported yet, no filament preset imported yet"
    assert list(jobs_dir.iterdir()) == []
