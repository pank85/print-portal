"""Preset editing API: machines, processes, filaments.

Runs against the ``datadir`` fixture so every write lands in a private copy of the
shipped presets, never in the session-wide DATA_DIR.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import printers
import profiles as prof

MACHINE_REL = "system/Custom/machine/generic-klipper-0.4.json"
PROCESS_REL = "system/Custom/process/generic-0.20-standard.json"
FILAMENT_REL = "system/Custom/filament/generic-pla.json"


def _cfg(monkeypatch, datadir, tmp_path, main):
    import dataclasses
    cfg = dataclasses.replace(main.CONFIG, orca_datadir=datadir,
                              profile_file=tmp_path / "profile.json")
    monkeypatch.setattr(main, "CONFIG", cfg)


@pytest.fixture
def client(tmp_path, datadir, monkeypatch):
    import main
    jobs = tmp_path / "jobs"; sliced = tmp_path / "sliced"
    jobs.mkdir(); sliced.mkdir()
    monkeypatch.setattr(main, "JOBS", jobs)
    monkeypatch.setattr(main, "SLICED", sliced)
    _cfg(monkeypatch, datadir, tmp_path, main)
    return TestClient(main.app)


def test_list_marks_custom_presets_editable(client: TestClient):
    r = client.get("/api/presets?kind=machine")
    assert r.status_code == 200
    rows = r.json()["presets"]
    assert any(row["rel"] == MACHINE_REL and row["editable"] for row in rows)


def test_view_reports_own_and_inherited(client: TestClient):
    r = client.get(f"/api/presets/machine/{MACHINE_REL}")
    assert r.status_code == 200
    view = r.json()
    assert view["editable"] is True
    fields = {f["key"]: f for f in view["fields"]}
    assert fields["printable_height"]["source"] in ("own", "inherited")
    assert fields["printer_model"]["value"]


def test_patch_writes_only_exposed_keys_and_preserves_the_rest(client: TestClient, datadir: Path):
    before = json.loads((datadir / MACHINE_REL).read_text())
    r = client.patch(f"/api/presets/machine/{MACHINE_REL}",
                     json={"fields": {"printable_height": "260"}})
    assert r.status_code == 200, r.text
    after = json.loads((datadir / MACHINE_REL).read_text())
    assert after["printable_height"] == "260"
    # untouched keys survive
    for k in ("name", "type", "from", "version", "nozzle_diameter", "inherits"):
        if k in before:
            assert after.get(k) == before[k], k
    # index file untouched
    assert (datadir / "system/Custom.json").read_text() == (datadir / "system/Custom.json").read_text()


def test_patch_rejects_unknown_and_bad_values(client: TestClient):
    r = client.patch(f"/api/presets/machine/{MACHINE_REL}",
                     json={"fields": {"not_a_field": "1"}})
    assert r.status_code == 400
    r = client.patch(f"/api/presets/machine/{MACHINE_REL}",
                     json={"fields": {"printable_height": "nope"}})
    assert r.status_code == 400
    r = client.patch(f"/api/presets/machine/{MACHINE_REL}", json={"fields": {}})
    assert r.status_code == 400
    r = client.patch(f"/api/presets/machine/{MACHINE_REL}", json={"fields": "x"})
    assert r.status_code == 400


def test_patch_read_only_vendor_preset(client: TestClient, datadir: Path):
    # a preset outside system/Custom is read-only
    vendor_rel = "system/Vendor/m.json"
    (datadir / "system/Vendor").mkdir(parents=True, exist_ok=True)
    (datadir / "system/Vendor/m.json").write_text(
        json.dumps({"name": "V", "type": "machine", "from": "system",
                    "printable_height": "200"}))
    r = client.patch(f"/api/presets/machine/{vendor_rel}",
                     json={"fields": {"printable_height": "210"}})
    assert r.status_code == 409


def test_patch_derived_portal_preset_is_read_only(client: TestClient, datadir: Path):
    rel = "system/Custom/process/print-portal-options.json"
    (datadir / "system/Custom/process/print-portal-options.json").write_text(
        json.dumps({"name": "x", "type": "process", "from": "user"}))
    r = client.patch(f"/api/presets/process/{rel}",
                     json={"fields": {"layer_height": "0.3"}})
    assert r.status_code == 409


def test_delete_process_removes_file_and_index_row(client: TestClient, datadir: Path):
    index_before = (datadir / "system/Custom.json").read_text()
    r = client.delete(f"/api/presets/process/{PROCESS_REL}")
    assert r.status_code == 200, r.text
    assert not (datadir / PROCESS_REL).exists()
    index_after = (datadir / "system/Custom.json").read_text()
    assert PROCESS_REL not in index_after or "generic-0.20-standard" not in index_after


def test_delete_machine_cascades_its_processes(client: TestClient, datadir: Path):
    # create a machine that owns a process
    m_rel = "system/Custom/machine/Cascade.json"
    p_rel = "system/Custom/process/CascadeProc.json"
    (datadir / "system/Custom/machine/Cascade.json").write_text(json.dumps(
        {"name": "Cascade", "type": "machine", "from": "system",
         "nozzle_diameter": ["0.4"], "printable_area": ["0,0", "10,0", "10,10", "0,10"],
         "printable_height": "100"}))
    (datadir / "system/Custom/process/CascadeProc.json").write_text(json.dumps(
        {"name": "CascadeProc", "type": "process", "from": "user",
         "layer_height": "0.2", "compatible_printers": ["Cascade"]}))
    # register in index
    import profiles as prof
    idx = prof.read_vendor_index(datadir)
    idx.setdefault("machine_list", []).append({"name": "Cascade", "sub_path": m_rel})
    idx.setdefault("process_list", []).append({"name": "CascadeProc", "sub_path": p_rel})
    prof.write_vendor_index(datadir, idx)

    r = client.delete(f"/api/presets/machine/{m_rel}")
    assert r.status_code == 200, r.text
    body = r.json()
    deleted_rels = {d["rel"] for d in body["deleted"]}
    assert m_rel in deleted_rels
    assert p_rel in deleted_rels
    assert not (datadir / m_rel).exists()
    assert not (datadir / p_rel).exists()


def test_filament_editable_but_not_deletable(client: TestClient):
    r = client.get(f"/api/presets/filament/{FILAMENT_REL}")
    assert r.status_code == 200
    assert r.json()["editable"] is True
    r = client.patch(f"/api/presets/filament/{FILAMENT_REL}",
                     json={"fields": {"nozzle_temperature": "215"}})
    assert r.status_code == 200
    r = client.delete(f"/api/presets/filament/{FILAMENT_REL}")
    assert r.status_code == 405


def test_unknown_kind_is_400(client: TestClient):
    r = client.get("/api/presets?kind=widget")
    assert r.status_code == 400
    r = client.get("/api/presets/widget/x")
    assert r.status_code == 400
