"""The /api/printers contract: masked views, selection wiring, health gating, probing.

Every test owns its printer storage: the ``printer_store`` fixture opens a
``printers.json``/``secret.key`` pair inside ``tmp_path`` and monkeypatches
``main.STORE``/``main.SEED``, and ``api_client`` points ``main.JOBS``/``main.SLICED`` at the
same ``tmp_path`` so nothing touches the session-wide DATA_DIR.

``TestClient`` is used without its context manager, so the lifespan never starts the
slicing worker. Prerecorded printer answers come from ``FakeWire`` (a fake
``urllib.request.urlopen``), which proves ``current_client()`` is rebuilt per request.
"""
from __future__ import annotations

import dataclasses
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import main
import printers

VIEW_KEYS = {"id", "name", "url", "api_key_masked", "has_api_key", "selected",
             "source", "editable", "pairing_editable", "profiles", "created", "updated"}
TEST_KEYS = {"ok", "state", "webhooks", "error", "url", "ms"}
MACHINE_REL = "system/Custom/machine/generic-klipper-0.4.json"
STATUS = {"webhooks": {"state": "ready"}, "print_stats": {"state": "standby"}}


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class FakeWire:
    """Fake ``urlopen``: records every requested URL and answers a canned Moonraker payload."""

    def __init__(self, status: dict | None = None, error: Exception | None = None) -> None:
        self.urls: list[str] = []
        self.status = STATUS if status is None else status
        self.error = error

    def __call__(self, req, timeout=None) -> FakeResponse:
        self.urls.append(req.full_url)
        if self.error is not None:
            raise self.error
        return FakeResponse({"result": {"status": self.status}})


def hosts(wire: FakeWire) -> set[str]:
    return {urllib.parse.urlsplit(url).netloc for url in wire.urls}


@pytest.fixture
def printer_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> printers.PrinterStore:
    store = printers.load_printers(tmp_path / "printers.json", tmp_path / "secret.key")
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "SEED", None)
    return store


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
    """A per-test ``profile.json`` plus datadir copy, so no test writes into DATA_DIR."""
    cfg = dataclasses.replace(main.CONFIG, orca_datadir=datadir,
                              profile_file=tmp_path / "profile.json")
    monkeypatch.setattr(main, "CONFIG", cfg)
    return cfg.profile_file


def seed_printer(api_key: str = "env-secret-key") -> printers.SeededPrinter:
    return printers.SeededPrinter(id=printers.ENV_PRINTER_ID, name="Default (environment)",
                                  url="http://env.invalid:7125", api_key=api_key)


def add_printer(client: TestClient, name: str = "Voron 2.4",
                url: str = "http://voron.invalid:7125",
                api_key: str = "k-0123456789abcdef"):
    return client.post("/api/printers", json={"name": name, "url": url, "api_key": api_key})


def sliced_job(jid: str = "decafbad1234", output_name: str = "cube") -> None:
    """A finished job whose gcode already sits in the sliced dir, as run_slice leaves it."""
    (main.JOBS / jid / "upload").mkdir(parents=True)
    (main.SLICED / jid).mkdir(parents=True)
    (main.SLICED / jid / f"{output_name}.gcode").write_text("; total filament used [g] = 3.94\n")
    (main.JOBS / jid / "job.json").write_text(json.dumps({
        "id": jid, "status": "sliced", "filename": "cube.stl", "output_name": output_name,
        "gcode": f"{output_name}.gcode", "gcode_dir": "sliced",
    }))


# ---------------------------------------------------------------- listing
def test_list_printers_is_empty_with_only_the_env_seed(
        printer_store, api_client, monkeypatch):
    monkeypatch.setattr(main, "SEED", seed_printer())

    body = api_client.get("/api/printers").json()

    assert set(body) == {"printers", "selected_id", "env_seed", "warning"}
    assert body["selected_id"] is None
    assert body["env_seed"] is True
    assert body["warning"] is None
    assert len(body["printers"]) == 1
    view = body["printers"][0]
    assert set(view) == VIEW_KEYS
    assert view["id"] == printers.ENV_PRINTER_ID
    assert view["source"] == "env"
    assert view["editable"] is False
    assert view["pairing_editable"] is True   # the pairing is a preference, the rest is not
    assert view["profiles"] == {"machine": None, "process": None}
    assert view["selected"] is True       # the seed is what a request would use
    assert view["created"] is None and view["updated"] is None
    assert view["api_key_masked"] == printers.MASK + "-key"


# ---------------------------------------------------------------- add
def test_add_printer_returns_a_masked_view(printer_store, api_client):
    response = add_printer(api_client)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"printer", "printers", "selected_id"}
    view = body["printer"]
    assert set(view) == VIEW_KEYS
    assert view["name"] == "Voron 2.4"
    assert view["url"] == "http://voron.invalid:7125"
    assert view["source"] == "store"
    assert view["editable"] is True
    assert view["pairing_editable"] is True
    assert view["has_api_key"] is True
    assert view["selected"] is True                    # first printer becomes the selection
    assert view["api_key_masked"] == printers.MASK + "cdef"
    assert view["created"] == view["updated"]
    assert body["selected_id"] == view["id"]
    assert [item["id"] for item in body["printers"]] == [view["id"]]


def test_add_printer_never_returns_the_key_or_the_token(printer_store, api_client):
    response = add_printer(api_client, api_key="plain-secret-key")

    assert response.status_code == 201
    assert "plain-secret-key" not in response.text
    assert "api_key_enc" not in response.text
    assert "gAAAAA" not in response.text               # no Fernet token either
    listing = api_client.get("/api/printers")
    assert "plain-secret-key" not in listing.text
    assert "api_key_enc" not in listing.text
    assert "plain-secret-key" not in printer_store.path.read_text()


@pytest.mark.parametrize("payload", [
    {"name": "", "url": "http://p.invalid:7125", "api_key": "k"},
    {"name": "x" * 61, "url": "http://p.invalid:7125", "api_key": "k"},
    {"name": "Voron", "url": "ftp://p.invalid", "api_key": "k"},
    {"name": "Voron", "url": "http://user:pw@p.invalid:7125", "api_key": "k"},
    {"name": "Voron", "url": "http://p.invalid:7125", "api_key": ""},
])
def test_add_printer_validation_is_400(printer_store, api_client, payload):
    response = api_client.post("/api/printers", json=payload)

    assert response.status_code == 400
    assert response.json()["detail"]
    assert printer_store.all() == []


# ---------------------------------------------------------------- patch / delete
def test_patch_printer_updates_name_and_url(printer_store, api_client):
    created = add_printer(api_client).json()["printer"]

    response = api_client.patch(f"/api/printers/{created['id']}",
                                json={"name": "Voron 0.2",
                                      "url": "https://voron.invalid/moonraker/"})

    assert response.status_code == 200
    view = response.json()["printer"]
    assert view["name"] == "Voron 0.2"
    assert view["url"] == "https://voron.invalid/moonraker"      # trailing slash dropped
    assert view["api_key_masked"] == created["api_key_masked"]   # key kept when omitted
    assert view["created"] == created["created"]
    assert response.json()["selected_id"] == created["id"]


def test_patch_printer_with_a_new_api_key_reencrypts_it(printer_store, api_client):
    created = add_printer(api_client, api_key="k-0123456789abcdef").json()["printer"]

    response = api_client.patch(f"/api/printers/{created['id']}",
                                json={"api_key": "k-fedcba9876543210"})

    assert response.status_code == 200
    view = response.json()["printer"]
    assert view["api_key_masked"] == printers.MASK + "3210"
    assert "k-fedcba9876543210" not in response.text
    assert "api_key_enc" not in response.text
    stored = json.loads(printer_store.path.read_text(encoding="utf-8"))["printers"][0]
    key = printers.load_or_create_key(printer_store.path.with_name("secret.key"))
    assert printers.decrypt_secret(stored["api_key_enc"], key) == "k-fedcba9876543210"
    assert printer_store.api_key(printer_store.get(created["id"])) == "k-fedcba9876543210"


def test_patch_printer_with_an_empty_body_is_400(printer_store, api_client):
    created = add_printer(api_client).json()["printer"]

    response = api_client.patch(f"/api/printers/{created['id']}", json={})

    assert response.status_code == 400
    assert response.json()["detail"] == "nothing to update"


def test_delete_printer_returns_the_new_selection(printer_store, api_client):
    first = add_printer(api_client, "A", "http://a.invalid:7125").json()["printer"]
    second = add_printer(api_client, "B", "http://b.invalid:7125").json()["printer"]
    assert second["selected"] is False

    response = api_client.delete(f"/api/printers/{first['id']}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"deleted", "printers", "selected_id"}
    assert body["deleted"] == first["id"]
    assert body["selected_id"] == second["id"]
    assert [view["id"] for view in body["printers"]] == [second["id"]]


# ---------------------------------------------------------------- selection
def test_select_printer_switches_the_active_client(printer_store, api_client, monkeypatch):
    first = add_printer(api_client, "A", "http://a.invalid:7125").json()["printer"]
    second = add_printer(api_client, "B", "http://b.invalid:7125").json()["printer"]
    wire = FakeWire()
    monkeypatch.setattr(urllib.request, "urlopen", wire)

    assert api_client.get("/api/printer").json()["host"] == first["url"]
    assert hosts(wire) == {"a.invalid:7125"}
    wire.urls.clear()

    selected = api_client.post(f"/api/printers/{second['id']}/select")

    assert selected.status_code == 200
    assert selected.json()["selected_id"] == second["id"]
    assert api_client.get("/api/printer").json()["host"] == second["url"]
    assert hosts(wire) == {"b.invalid:7125"}


def test_select_env_without_a_seed_is_404(printer_store, api_client):
    add_printer(api_client)

    response = api_client.post(f"/api/printers/{printers.ENV_PRINTER_ID}/select")

    assert response.status_code == 404
    assert response.json()["detail"] == "printer 'env' not found"
    assert printer_store.selected_id() == printer_store.all()[0].id   # selection unchanged


def test_select_env_with_a_seed_becomes_the_active_printer(
        printer_store, api_client, monkeypatch):
    monkeypatch.setattr(main, "SEED", seed_printer())
    add_printer(api_client)                              # the store printer starts out selected
    wire = FakeWire()
    monkeypatch.setattr(urllib.request, "urlopen", wire)

    response = api_client.post(f"/api/printers/{printers.ENV_PRINTER_ID}/select")

    assert response.status_code == 200
    body = response.json()
    assert body["selected_id"] == printers.ENV_PRINTER_ID
    assert [view["id"] for view in body["printers"] if view["selected"]] == [
        printers.ENV_PRINTER_ID]
    printer = api_client.get("/api/printer").json()["printer"]
    assert printer == {"id": printers.ENV_PRINTER_ID, "name": "Default (environment)",
                       "source": "env"}
    assert hosts(wire) == {"env.invalid:7125"}


def test_printer_payload_reports_the_selected_host(printer_store, api_client, monkeypatch):
    created = add_printer(api_client).json()["printer"]
    monkeypatch.setattr(urllib.request, "urlopen", FakeWire())

    body = api_client.get("/api/printer").json()

    assert body["ok"] is True
    assert body["host"] == "http://voron.invalid:7125"
    assert body["printer"] == {"id": created["id"], "name": "Voron 2.4", "source": "store"}


def test_print_uses_the_selected_printer(printer_store, api_client, monkeypatch):
    add_printer(api_client, "A", "http://a.invalid:7125")
    second = add_printer(api_client, "B", "http://b.invalid:7125").json()["printer"]
    api_client.post(f"/api/printers/{second['id']}/select")
    sliced_job()
    wire = FakeWire()
    monkeypatch.setattr(urllib.request, "urlopen", wire)

    response = api_client.post("/api/jobs/decafbad1234/print")

    assert response.status_code == 200
    assert response.json()["remote_file"] == "portal_decafbad1234_cube.gcode"
    assert hosts(wire) == {"b.invalid:7125"}


# ---------------------------------------------------------------- test connection
def test_test_connection_reports_offline_without_raising(
        printer_store, api_client, monkeypatch):
    created = add_printer(api_client).json()["printer"]

    def refuse(req, timeout=None):
        raise urllib.error.URLError(OSError("connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    response = api_client.post(f"/api/printers/{created['id']}/test")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == TEST_KEYS
    assert body["ok"] is False
    assert body["state"] == "offline"
    assert "unreachable" in body["error"]
    assert body["url"] == created["url"]
    assert isinstance(body["ms"], int)


def test_test_connection_times_out_gracefully(printer_store, api_client, monkeypatch):
    created = add_printer(api_client).json()["printer"]

    def time_out(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", time_out)

    response = api_client.post(f"/api/printers/{created['id']}/test")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "TimeoutError" in body["error"]
    assert isinstance(body["ms"], int)


# ---------------------------------------------------------------- error mapping
@pytest.mark.parametrize("method,path", [
    ("patch", "/api/printers/000000000000"),
    ("delete", "/api/printers/000000000000"),
    ("post", "/api/printers/000000000000/select"),
    ("post", "/api/printers/000000000000/test"),
])
def test_unknown_printer_is_404(printer_store, api_client, method, path):
    call = getattr(api_client, method)
    response = call(path, json={"name": "x"}) if method == "patch" else call(path)

    assert response.status_code == 404


def test_env_printer_refuses_credentials_but_pairs_and_cannot_be_deleted(
        printer_store, api_client, profile_file, monkeypatch):
    """The env row's name/url/key stay deployment-owned; its pairing is editable."""
    monkeypatch.setattr(main, "SEED", seed_printer())

    listing = api_client.get("/api/printers").json()
    assert [view["id"] for view in listing["printers"]] == [printers.ENV_PRINTER_ID]
    assert listing["env_seed"] is True

    for field, value in (("name", "renamed"), ("url", "http://other.invalid:7125"),
                         ("api_key", "k-fedcba9876543210")):
        refused = api_client.patch(f"/api/printers/{printers.ENV_PRINTER_ID}",
                                   json={field: value})
        assert refused.status_code == 409, (field, refused.text)   # same code as delete
        assert refused.json()["detail"] == printers.READ_ONLY_ERROR
    deleted = api_client.delete(f"/api/printers/{printers.ENV_PRINTER_ID}")
    assert deleted.status_code == 409
    assert "read-only" in deleted.json()["detail"]

    # a store printer becomes active, so pairing the seed below writes no profile.json
    add_printer(api_client)
    paired = api_client.patch(f"/api/printers/{printers.ENV_PRINTER_ID}",
                              json={"profiles": {"machine": MACHINE_REL}})

    assert paired.status_code == 200, paired.text
    assert not profile_file.exists()               # the inactive seed changes no profile
    assert paired.json()["printer"]["profiles"] == {"machine": MACHINE_REL,
                                                    "process": None}
    assert paired.json()["printer"]["editable"] is False
    assert paired.json()["printer"]["pairing_editable"] is True
    row = api_client.get("/api/printers").json()["printers"][-1]
    assert row["id"] == printers.ENV_PRINTER_ID
    assert row["profiles"] == {"machine": MACHINE_REL, "process": None}
    assert row["name"] == seed_printer().name      # the credential change never landed
    reloaded = printers.load_printers(printer_store.path)
    assert reloaded.views(seed_printer())[-1]["profiles"] == {"machine": MACHINE_REL,
                                                              "process": None}


def test_health_is_503_without_any_printer(printer_store, api_client):
    response = api_client.get("/api/health")

    assert response.status_code == 503
    assert "no printer configured" in response.json()["detail"]


def test_health_reports_the_build_once_a_printer_exists(printer_store, api_client):
    add_printer(api_client)

    response = api_client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "print-portal"
    assert body["version"] == config.VERSION


def test_corrupt_store_is_surfaced_as_a_warning(printer_store, api_client):
    printer_store.path.write_text("{not json")

    listing = api_client.get("/api/printers")

    assert listing.status_code == 200
    body = listing.json()
    assert body["printers"] == []
    assert body["selected_id"] is None
    assert "not valid JSON" in body["warning"]

    refused = api_client.post("/api/printers", json={
        "name": "Voron", "url": "http://voron.invalid:7125", "api_key": "k"})
    assert refused.status_code == 409
    assert printer_store.path.read_text() == "{not json"      # never overwritten
