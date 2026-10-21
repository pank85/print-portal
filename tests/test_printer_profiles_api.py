"""Printer ↔ preset pairing over HTTP: add/patch, selection, and the dashboard write-back.

Every test owns its storage (``printer_store``), its job dirs (``api_client``) and its
``profile.json`` plus datadir copy (``profile_file``), so no test writes into the session
DATA_DIR or into ``presets/``. ``TestClient`` is used without its context manager, so the
lifespan never starts the slicing worker.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
import printers
import profiles as prof

MACHINE_REL = prof.DEFAULT_PROFILE["machine"]
PROCESS_REL = prof.DEFAULT_PROFILE["process"]
FILAMENT_REL = prof.DEFAULT_PROFILE["filament"]
OTHER_MACHINE_REL = "system/Custom/machine/generic-klipper.json"
VIEW_KEYS = {"id", "name", "url", "api_key_masked", "has_api_key", "selected",
             "source", "editable", "profiles", "created", "updated"}


@pytest.fixture
def printer_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    path = tmp_path / "printers.json"
    store = printers.load_printers(path, tmp_path / "secret.key")
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "SEED", None)
    return SimpleNamespace(store=store, path=path, key=tmp_path / "secret.key")


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    jobs = tmp_path / "jobs"
    sliced = tmp_path / "sliced"
    jobs.mkdir()
    sliced.mkdir()
    monkeypatch.setattr(main, "JOBS", jobs)
    monkeypatch.setattr(main, "SLICED", sliced)
    return TestClient(main.app)


@pytest.fixture
def profile_file(tmp_path: Path, datadir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = dataclasses.replace(main.CONFIG, orca_datadir=datadir,
                              profile_file=tmp_path / "profile.json")
    monkeypatch.setattr(main, "CONFIG", cfg)
    return cfg.profile_file


@pytest.fixture
def env_seed(monkeypatch: pytest.MonkeyPatch) -> printers.SeededPrinter:
    seed = printers.SeededPrinter(id=printers.ENV_PRINTER_ID, name="Default (environment)",
                                  url="http://env.invalid:7125", api_key="env-secret-key")
    monkeypatch.setattr(main, "SEED", seed)
    return seed


def add_printer(client: TestClient, name: str = "Voron 2.4",
                profiles: dict | None = None) -> dict:
    body: dict = {"name": name, "url": "http://voron.invalid:7125",
                  "api_key": "k-0123456789abcdef"}
    if profiles is not None:
        body["profiles"] = profiles
    response = client.post("/api/printers", json=body)
    assert response.status_code == 201, response.text
    return response.json()["printer"]


def profiles_of(printer_store: SimpleNamespace, printer_id: str) -> dict[str, str] | None:
    """The pairing as it survives a reload from disk (not the in-memory copy)."""
    stored = printers.PrinterStore.open(printer_store.path, printer_store.key)
    return stored.get(printer_id).profiles


# ---------------------------------------------------------------- add / patch
def test_add_printer_with_profiles_persists_the_pairing(printer_store, api_client):
    view = add_printer(api_client, profiles={"machine": MACHINE_REL,
                                             "process": PROCESS_REL})

    assert set(view) == VIEW_KEYS
    assert view["profiles"] == {"machine": MACHINE_REL, "process": PROCESS_REL}
    assert profiles_of(printer_store, view["id"]) == {"machine": MACHINE_REL,
                                                      "process": PROCESS_REL}
    listing = api_client.get("/api/printers").json()
    assert set(listing) == {"printers", "selected_id", "env_seed", "warning"}
    assert listing["printers"][0]["profiles"] == {"machine": MACHINE_REL,
                                                  "process": PROCESS_REL}


def test_patch_printer_sets_and_clears_the_pairing(printer_store, api_client):
    view = add_printer(api_client)

    paired = api_client.patch(f"/api/printers/{view['id']}",
                              json={"profiles": {"machine": MACHINE_REL}})
    assert paired.status_code == 200, paired.text
    assert paired.json()["printer"]["profiles"] == {"machine": MACHINE_REL, "process": None}
    assert profiles_of(printer_store, view["id"]) == {"machine": MACHINE_REL}

    renamed = api_client.patch(f"/api/printers/{view['id']}", json={"name": "Shop Voron"})
    assert renamed.json()["printer"]["profiles"] == {"machine": MACHINE_REL, "process": None}

    cleared = api_client.patch(f"/api/printers/{view['id']}", json={"profiles": {}})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["printer"]["profiles"] == {"machine": None, "process": None}
    assert profiles_of(printer_store, view["id"]) is None


def test_pairing_outside_the_datadir_is_a_400(printer_store, api_client):
    response = api_client.post("/api/printers",
                               json={"name": "Voron 2.4", "url": "http://voron.invalid:7125",
                                     "api_key": "k-0123456789abcdef",
                                     "profiles": {"machine": "../outside.json"}})

    assert response.status_code == 400
    assert response.json()["detail"] == \
        "machine: '../outside.json' is not a preset inside the datadir"
    assert not printer_store.path.exists()          # the bad pairing never reached the store

    view = add_printer(api_client)
    patched = api_client.patch(f"/api/printers/{view['id']}",
                               json={"profiles": {"process": "system/Custom/process/nope.json"}})
    assert patched.status_code == 400
    assert "not a preset inside the datadir" in patched.json()["detail"]
    assert profiles_of(printer_store, view["id"]) is None


def test_pairing_with_a_wrong_typed_preset_is_a_400(printer_store, api_client):
    """Each slot only takes its own kind, so a process path cannot end up in `machine`."""
    response = api_client.post("/api/printers",
                               json={"name": "Voron 2.4", "url": "http://voron.invalid:7125",
                                     "api_key": "k-0123456789abcdef",
                                     "profiles": {"machine": PROCESS_REL}})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == \
        f"machine: '{PROCESS_REL}' is not a preset inside the datadir"
    assert not printer_store.path.exists()          # the bad pairing never reached the store

    view = add_printer(api_client)
    for slot, rel in (("machine", FILAMENT_REL), ("machine", OTHER_MACHINE_REL),
                      ("process", MACHINE_REL), ("process", FILAMENT_REL)):
        patched = api_client.patch(f"/api/printers/{view['id']}",
                                   json={"profiles": {slot: rel}})
        assert patched.status_code == 400, (slot, rel, patched.text)
        assert patched.json()["detail"] == \
            f"{slot}: '{rel}' is not a preset inside the datadir"
    assert profiles_of(printer_store, view["id"]) is None


def test_env_printer_cannot_be_paired(printer_store, api_client, env_seed):
    response = api_client.patch(f"/api/printers/{printers.ENV_PRINTER_ID}",
                                json={"profiles": {"machine": MACHINE_REL}})

    assert response.status_code == 409
    assert response.json()["detail"] == printers.READ_ONLY_ERROR
    assert printer_store.store.all() == []


# ---------------------------------------------------------------- selecting a printer
def test_select_printer_applies_its_pairing(printer_store, profile_file, api_client):
    profile_file.write_text(json.dumps({"machine": OTHER_MACHINE_REL,
                                        "machine_label": "Generic Klipper Printer",
                                        "process": PROCESS_REL,
                                        "filament": FILAMENT_REL,
                                        "options": {"supports": True}}))
    view = add_printer(api_client, profiles={"machine": MACHINE_REL, "process": PROCESS_REL})

    response = api_client.post(f"/api/printers/{view['id']}/select")

    assert response.status_code == 200, response.text
    assert response.json()["selected_id"] == view["id"]
    profile = json.loads(profile_file.read_text())
    assert profile["machine"] == MACHINE_REL
    assert profile["machine_label"] == "Generic Klipper 0.4 nozzle"
    assert profile["process"] == PROCESS_REL
    assert profile["filament"] == FILAMENT_REL          # untouched slots survive
    assert profile["options"] == {"supports": True}     # so do the options


def test_select_printer_without_pairing_keeps_the_global_profile(printer_store, profile_file,
                                                                api_client):
    original = json.dumps({"machine": OTHER_MACHINE_REL, "machine_label": "Hand written",
                           "process": PROCESS_REL}, indent="\t").encode()
    profile_file.write_bytes(original)
    view = add_printer(api_client)

    response = api_client.post(f"/api/printers/{view['id']}/select")

    assert response.status_code == 200, response.text
    assert profile_file.read_bytes() == original        # "inherit" writes nothing


# ---------------------------------------------------------------- dashboard write-back
def test_profile_select_writes_the_pairing_back(printer_store, profile_file, api_client):
    view = add_printer(api_client)

    response = api_client.post("/api/profile/select",
                               json={"machine": MACHINE_REL, "process": PROCESS_REL})

    assert response.status_code == 200, response.text
    assert response.json()["active"]["machine"] == MACHINE_REL
    assert profiles_of(printer_store, view["id"]) == {"machine": MACHINE_REL,
                                                      "process": PROCESS_REL}
    listing = api_client.get("/api/printers").json()
    assert listing["printers"][0]["profiles"] == {"machine": MACHINE_REL,
                                                  "process": PROCESS_REL}


def test_profile_select_skips_the_read_only_env_seed(printer_store, profile_file, api_client,
                                                     env_seed):
    response = api_client.post("/api/profile/select", json={"machine": MACHINE_REL})

    assert response.status_code == 200, response.text
    assert response.json()["active"]["machine"] == MACHINE_REL
    assert json.loads(profile_file.read_text())["machine"] == MACHINE_REL
    assert not printer_store.path.exists()          # nothing to write back onto
