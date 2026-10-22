"""Printer ↔ preset pairing over HTTP: add/patch, selection, and the dashboard write-back.

Every test owns its storage (``printer_store``), its job dirs (``api_client``) and its
``profile.json`` plus datadir copy (``profile_file``), so no test writes into the session
DATA_DIR or into ``presets/``. ``TestClient`` is used without its context manager, so the
lifespan never starts the slicing worker.
"""
from __future__ import annotations

import dataclasses
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import main
import printers
import profiles as prof

MACHINE_REL = prof.DEFAULT_PROFILE["machine"]
PROCESS_REL = prof.DEFAULT_PROFILE["process"]
FILAMENT_REL = prof.DEFAULT_PROFILE["filament"]
OTHER_MACHINE_REL = "system/Custom/machine/generic-klipper.json"
PAIRING = {"machine": MACHINE_REL, "process": PROCESS_REL}
VIEW_KEYS = {"id", "name", "url", "api_key_masked", "has_api_key", "selected",
             "source", "editable", "pairing_editable", "profiles", "created", "updated"}


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


def env_row(client: TestClient) -> dict:
    listing = client.get("/api/printers").json()
    return next(row for row in listing["printers"]
                if row["id"] == printers.ENV_PRINTER_ID)


def profiles_of(printer_store: SimpleNamespace, printer_id: str) -> dict[str, str] | None:
    """The pairing as it survives a reload from disk (not the in-memory copy)."""
    stored = printers.PrinterStore.open(printer_store.path, printer_store.key)
    return stored.get(printer_id).profiles


def stored_document(printer_store: SimpleNamespace) -> dict:
    return json.loads(printer_store.path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- add / patch
def test_add_printer_with_profiles_persists_the_pairing(printer_store, profile_file,
                                                       api_client):
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


def test_patch_printer_sets_and_clears_the_pairing(printer_store, profile_file, api_client):
    """A pairing replaces, never merges."""
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


def test_env_printer_refuses_credentials_but_accepts_a_pairing(printer_store, profile_file,
                                                               api_client, env_seed):
    """The env row's name/url/key are deployment-owned; its pairing is a user preference.

    The seed is the active printer here (no store printer exists), so accepting a pairing
    also has to reach ``profile.json``: that file is what the slicer loads, and a pairing
    that only lands in the row leaves the two disagreeing.
    """
    paired = api_client.patch(f"/api/printers/{printers.ENV_PRINTER_ID}",
                              json={"profiles": PAIRING})

    assert paired.status_code == 200, paired.text
    view = paired.json()["printer"]
    assert view["id"] == printers.ENV_PRINTER_ID
    assert set(view) == VIEW_KEYS
    assert view["source"] == "env"
    assert view["editable"] is False
    assert view["pairing_editable"] is True
    assert view["profiles"] == PAIRING
    profile = json.loads(profile_file.read_text())
    assert profile["machine"] == MACHINE_REL
    assert profile["process"] == PROCESS_REL

    for field, value in (("name", "Renamed"), ("url", "http://other.invalid:7125"),
                         ("api_key", "k-fedcba9876543210")):
        refused = api_client.patch(f"/api/printers/{printers.ENV_PRINTER_ID}",
                                   json={field: value})
        assert refused.status_code == 409, (field, refused.text)
        assert refused.json()["detail"] == printers.READ_ONLY_ERROR

    assert env_row(api_client)["profiles"] == PAIRING


def test_patch_env_printer_persists_its_pairing(printer_store, profile_file, api_client,
                                                env_seed):
    """The env pairing lives in ``env_profiles`` in printers.json, not in memory only."""
    response = api_client.patch(f"/api/printers/{printers.ENV_PRINTER_ID}",
                                json={"profiles": PAIRING})

    assert response.status_code == 200, response.text
    assert stored_document(printer_store)["env_profiles"] == PAIRING
    assert stored_document(printer_store)["printers"] == []      # no printer record added
    reloaded = printers.PrinterStore.open(printer_store.path, printer_store.key)
    assert reloaded.views(env_seed)[-1]["profiles"] == PAIRING

    cleared = api_client.patch(f"/api/printers/{printers.ENV_PRINTER_ID}",
                               json={"profiles": {}})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["printer"]["profiles"] == {"machine": None, "process": None}
    assert stored_document(printer_store)["env_profiles"] is None


def test_patch_env_printer_without_a_profiles_key_is_a_400(printer_store, api_client,
                                                           env_seed):
    response = api_client.patch(f"/api/printers/{printers.ENV_PRINTER_ID}", json={})

    assert response.status_code == 400, response.text
    assert not printer_store.path.exists()          # nothing was written


def test_patch_env_printer_without_a_seed_is_a_404(printer_store, api_client):
    """No env seed, no env row: the id is unknown, exactly as it is for select."""
    response = api_client.patch(f"/api/printers/{printers.ENV_PRINTER_ID}",
                                json={"profiles": {"machine": MACHINE_REL}})

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"printer {printers.ENV_PRINTER_ID!r} not found"
    assert not printer_store.path.exists()


@pytest.mark.parametrize("env_profiles", [
    {"machine": "../outside.json"},
    {"machine": 5},
    {"slicer": MACHINE_REL},
    {"machine": ""},
    {"process": "/system/Custom/process/generic-0.20-standard.json"},
    [MACHINE_REL],
    "system/Custom/machine/generic-klipper.json",
])
def test_malformed_env_profiles_reads_as_no_pairing(printer_store, api_client, env_seed,
                                                    env_profiles):
    """A bad ``env_profiles`` is one unusable preference, never a corrupt store."""
    document = {"version": printers.PRINTERS_VERSION, "selected_id": None,
                "printers": [], "env_profiles": env_profiles}
    printer_store.path.write_text(json.dumps(document), encoding="utf-8")
    before = printer_store.path.read_bytes()

    listing = api_client.get("/api/printers").json()

    assert listing["warning"] is None                # not reported as a broken file
    row = next(item for item in listing["printers"]
               if item["id"] == printers.ENV_PRINTER_ID)
    assert row["profiles"] == {"machine": None, "process": None}
    assert printer_store.path.read_bytes() == before  # reading never rewrites the file


# ---------------------------------------------------------------- merging the env pairing
class CriticalSectionCounter:
    """Stand-in for the store's ``RLock``: it counts the sections it is entered for."""

    def __init__(self, inner: threading.RLock) -> None:
        self.inner = inner
        self.entries = 0

    def __enter__(self) -> None:
        self.inner.acquire()
        self.entries += 1

    def __exit__(self, *_exc_info: object) -> bool:
        self.inner.release()
        return False


def test_merge_env_pairing_keeps_the_slots_it_was_not_given(printer_store):
    """The dashboard write-back sends one slot; the stored one has to survive."""
    store = printer_store.store
    store.set_env_pairing({"machine": MACHINE_REL})

    merged = store.merge_env_pairing({"process": PROCESS_REL})

    assert merged == {"machine": MACHINE_REL, "process": PROCESS_REL}
    assert stored_document(printer_store)["env_profiles"] == {"machine": MACHINE_REL,
                                                              "process": PROCESS_REL}


def test_merge_env_pairing_is_one_critical_section(printer_store):
    """Read, merge and write share one lock acquisition, so nothing lands in between.

    Doing it as a separate read followed by a ``set_env_pairing()`` write took the lock
    twice, and a ``PATCH /api/printers/env`` that arrived between the two was overwritten
    with the copy the first half had already read.
    """
    store = printer_store.store
    store.set_env_pairing({"machine": MACHINE_REL})
    counter = CriticalSectionCounter(store._lock)
    store._lock = counter

    store.merge_env_pairing({"process": PROCESS_REL})

    assert counter.entries == 1
    assert stored_document(printer_store)["env_profiles"] == {"machine": MACHINE_REL,
                                                              "process": PROCESS_REL}


def test_merge_env_pairing_refuses_a_corrupt_store(printer_store):
    """A corrupt document is reported, never repaired over with a merge."""
    printer_store.path.write_text("{not json", encoding="utf-8")
    store = printer_store.store.reload()

    with pytest.raises(printers.PrinterStoreError):
        store.merge_env_pairing({"machine": MACHINE_REL})

    assert printer_store.path.read_text(encoding="utf-8") == "{not json"


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


def test_select_env_printer_applies_its_stored_pairing(printer_store, profile_file, api_client,
                                                       env_seed):
    """The env seed's pairing is a preference like any other, applied on selection."""
    add_printer(api_client)                 # a store printer is active, so the seed is not
    paired = api_client.patch(f"/api/printers/{printers.ENV_PRINTER_ID}",
                              json={"profiles": PAIRING})
    assert paired.status_code == 200, paired.text
    assert not profile_file.exists()        # pairing an inactive printer changes no profile

    response = api_client.post(f"/api/printers/{printers.ENV_PRINTER_ID}/select")

    assert response.status_code == 200, response.text
    assert response.json()["selected_id"] == printers.ENV_PRINTER_ID
    profile = json.loads(profile_file.read_text())
    assert profile["machine"] == MACHINE_REL
    assert profile["process"] == PROCESS_REL


# ---------------------------------------------------------------- patching the pairing
def test_patch_active_printer_applies_its_pairing(printer_store, profile_file, api_client):
    """Editing the active printer's pairing has to reach the file the slicer is given."""
    profile_file.write_text(json.dumps({"machine": OTHER_MACHINE_REL,
                                        "machine_label": "Generic Klipper Printer",
                                        "process": PROCESS_REL,
                                        "filament": FILAMENT_REL,
                                        "options": {"supports": True}}))
    view = add_printer(api_client)          # the first printer is the active one

    response = api_client.patch(f"/api/printers/{view['id']}",
                                json={"profiles": PAIRING})

    assert response.status_code == 200, response.text
    profile = json.loads(profile_file.read_text())
    assert profile["machine"] == MACHINE_REL
    assert profile["machine_label"] == "Generic Klipper 0.4 nozzle"
    assert profile["process"] == PROCESS_REL
    assert profile["filament"] == FILAMENT_REL          # untouched slots survive
    assert profile["options"] == {"supports": True}


def test_patch_inactive_printer_keeps_the_global_profile(printer_store, profile_file,
                                                         api_client):
    active = add_printer(api_client)
    other = add_printer(api_client, name="Core One")
    original = json.dumps({"machine": OTHER_MACHINE_REL,
                           "process": PROCESS_REL}).encode()
    profile_file.write_bytes(original)

    response = api_client.patch(f"/api/printers/{other['id']}",
                                json={"profiles": {"machine": MACHINE_REL}})

    assert response.status_code == 200, response.text
    assert response.json()["printer"]["selected"] is False
    assert active["selected"] is True       # the selection never moved
    assert profile_file.read_bytes() == original


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


def test_profile_select_writes_the_pairing_back_to_the_env_seed(printer_store, profile_file,
                                                               api_client, env_seed):
    """A dashboard selection on the env seed lands in ``env_profiles`` as it does on a row.

    Skipping the seed made the stored pairing contradict ``profile.json``: Settings would
    show the old machine/process and saving that form would revert the selection the
    operator just made on the dashboard.
    """
    response = api_client.post("/api/profile/select",
                               json={"machine": MACHINE_REL, "process": PROCESS_REL})

    assert response.status_code == 200, response.text
    assert response.json()["active"]["machine"] == MACHINE_REL
    profile = json.loads(profile_file.read_text())
    assert profile["machine"] == MACHINE_REL
    assert profile["process"] == PROCESS_REL
    assert stored_document(printer_store)["env_profiles"] == {"machine": MACHINE_REL,
                                                              "process": PROCESS_REL}
    assert env_row(api_client)["profiles"] == {"machine": MACHINE_REL,
                                               "process": PROCESS_REL}


def test_profile_select_merges_into_the_env_pairing(printer_store, profile_file, api_client,
                                                    env_seed, monkeypatch):
    """Only the slots the dashboard chose are touched; the stored machine survives.

    The merge happens inside the store, so it cannot be split into a read that a
    concurrent ``PATCH`` of that row then overwrites: the write-back never re-writes the
    stored object from a copy it read itself.
    """
    paired = api_client.patch(f"/api/printers/{printers.ENV_PRINTER_ID}",
                              json={"profiles": {"machine": MACHINE_REL}})
    assert paired.status_code == 200, paired.text

    def read_then_replace(*_args, **_kwargs):
        raise AssertionError("the env pairing must be merged in one locked write")

    monkeypatch.setattr(printers.PrinterStore, "set_env_pairing", read_then_replace)

    response = api_client.post("/api/profile/select", json={"process": PROCESS_REL})

    assert response.status_code == 200, response.text
    assert stored_document(printer_store)["env_profiles"] == {"machine": MACHINE_REL,
                                                              "process": PROCESS_REL}
    profile = json.loads(profile_file.read_text())
    assert profile["machine"] == MACHINE_REL
    assert profile["process"] == PROCESS_REL


def test_profile_select_survives_an_unwritable_printers_json(printer_store, profile_file,
                                                              api_client, monkeypatch):
    """The write-back is best effort: an unwritable store must not fail the selection.

    ``profile.json`` is the file the slicer loads and it is already written by the time the
    store is touched, so a read-only ``printers.json`` on a deployment costs the stored
    pairing, not the 200.
    """
    add_printer(api_client)
    attempts: list[str] = []

    def unwritable(path: Path, _data: dict) -> None:
        attempts.append(path.name)
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(printers, "_atomic_write", unwritable)

    response = api_client.post("/api/profile/select", json={"machine": MACHINE_REL})

    assert response.status_code == 200, response.text
    assert attempts == ["printers.json"]
    assert json.loads(profile_file.read_text())["machine"] == MACHINE_REL


# ---------------------------------------------------------------- adding a printer
def test_add_applies_the_first_printers_pairing(printer_store, profile_file, api_client):
    """``add`` selects the first printer, so the pairing it was created with has to land.

    Otherwise the row shows the pairing while ``profile.json`` keeps the global default
    until the operator clicks "Use".
    """
    profile_file.write_text(json.dumps({"machine": OTHER_MACHINE_REL,
                                        "machine_label": "Generic Klipper Printer",
                                        "process": PROCESS_REL,
                                        "filament": FILAMENT_REL,
                                        "options": {"supports": True}}))

    response = api_client.post("/api/printers",
                               json={"name": "Voron 2.4", "url": "http://voron.invalid:7125",
                                     "api_key": "k-0123456789abcdef",
                                     "profiles": PAIRING})

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"printer", "printers", "selected_id"}
    assert body["selected_id"] == body["printer"]["id"]
    profile = json.loads(profile_file.read_text())
    assert profile["machine"] == MACHINE_REL
    assert profile["machine_label"] == "Generic Klipper 0.4 nozzle"
    assert profile["process"] == PROCESS_REL
    assert profile["filament"] == FILAMENT_REL          # untouched slots survive
    assert profile["options"] == {"supports": True}     # so do the options


def test_add_of_a_further_printer_keeps_the_global_profile(printer_store, profile_file,
                                                           api_client):
    """Only the add that makes a printer active may write ``profile.json``."""
    active = add_printer(api_client)        # the first printer is the active one
    original = json.dumps({"machine": OTHER_MACHINE_REL,
                           "process": PROCESS_REL}).encode()
    profile_file.write_bytes(original)

    added = add_printer(api_client, name="Core One", profiles={"machine": MACHINE_REL})

    assert added["selected"] is False
    assert active["selected"] is True       # the selection never moved
    assert profile_file.read_bytes() == original


# ------------------------------------- a write that landed is never reported as a failure
def undecryptable_store(printer_store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
                        api_key: str = "k-0123456789abcdef") -> str:
    """Persist one printer, then read the file through a foreign ``secret.key``.

    A key file that no longer matches the tokens in ``printers.json`` is a documented
    operator state: ``views()`` masks it, ``add``/``update`` still write, and only
    resolving it as a print target fails. Returns the id that row keeps as the selection.
    """
    first = printer_store.store.add("Voron 2.4", "http://voron.invalid:7125", api_key)
    monkeypatch.setattr(main, "STORE",
                        printers.PrinterStore(printer_store.path, Fernet.generate_key()))
    return first.id


def test_add_printer_succeeds_when_the_active_rows_key_is_undecryptable(
        printer_store, profile_file, api_client, monkeypatch):
    """The 409 belongs to the row that cannot be decrypted, not to the row just added.

    ``active_target()`` raises when the *selected* row's stored API key no longer matches
    ``secret.key``. By the time ``apply_pairing_to_active`` resolves it, the new row is
    already on disk, so letting that 409 through answered an error for work that landed
    and a retry duplicated the printer. Pins the ``except HTTPException: return`` around
    the ``active_target()`` call in ``apply_pairing_to_active`` (app/main.py).
    """
    undecryptable_store(printer_store, monkeypatch)

    response = api_client.post("/api/printers",
                               json={"name": "Core One", "url": "http://core.invalid:7125",
                                     "api_key": "k-fedcba9876543210", "profiles": PAIRING})

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"printer", "printers", "selected_id"}
    assert profiles_of(printer_store, body["printer"]["id"]) == PAIRING   # the row landed
    assert len(stored_document(printer_store)["printers"]) == 2          # exactly one add
    assert not profile_file.exists()      # the pairing is stored, just not applicable yet


def test_add_printer_succeeds_when_the_datadir_is_read_only(printer_store, profile_file,
                                                           api_client, monkeypatch):
    """A read-only datadir costs the applied pairing, not the printer that just landed.

    ``write_profile_file`` raises ``OSError`` when ``profile.json`` cannot be written, and
    nothing above the endpoint handles that: the container answers 500 for a row that is
    already persisted. Pins the ``except (printers.PrinterError, printers.PrinterStoreError,
    OSError)`` around the apply call in ``apply_pairing_to_active`` (app/main.py).
    """
    def unwritable(_data: dict) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(main, "write_profile_file", unwritable)

    response = api_client.post("/api/printers",
                               json={"name": "Core One", "url": "http://core.invalid:7125",
                                     "api_key": "k-fedcba9876543210", "profiles": PAIRING})

    assert response.status_code == 201, response.text
    assert profiles_of(printer_store, response.json()["printer"]["id"]) == PAIRING


def test_patch_env_printer_succeeds_when_the_active_rows_key_is_undecryptable(
        printer_store, profile_file, api_client, env_seed, monkeypatch):
    """Pairing the env row persists into ``env_profiles``; the apply that follows is a bonus.

    The stored pairing is written before the active printer is resolved, so a 409 out of
    that resolution reported failure for a change the operator can see in Settings. Pins
    the same guard for the second caller, ``patch_env_printer`` (app/main.py).
    """
    undecryptable_store(printer_store, monkeypatch)

    response = api_client.patch(f"/api/printers/{printers.ENV_PRINTER_ID}",
                                json={"profiles": PAIRING})

    assert response.status_code == 200, response.text
    assert set(response.json()) == {"printer", "printers", "selected_id"}
    assert stored_document(printer_store)["env_profiles"] == PAIRING
    assert not profile_file.exists()


def test_select_still_reports_a_pairing_that_cannot_be_applied(printer_store, profile_file,
                                                               api_client, monkeypatch):
    """The guard stops at the post-write apply; selecting a printer still cannot be silent.

    ``profile.json`` is the file the slicer loads, so a select that cannot write it would
    leave Settings and the next slice disagreeing with no way to tell. ``apply_pairing``
    keeps raising — only the add/patch callers swallow it.
    """
    view = add_printer(api_client, profiles=PAIRING)

    def unwritable(_data: dict) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(main, "write_profile_file", unwritable)

    with pytest.raises(OSError):
        api_client.post(f"/api/printers/{view['id']}/select")
