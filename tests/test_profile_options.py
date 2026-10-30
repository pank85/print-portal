"""Basic profile options: storage, API contract, derived preset, slice wiring, UI hooks.

Every test points ``main.CONFIG`` at a ``profile.json`` inside ``tmp_path`` and at the private
``datadir`` copy of the shipped seed, so options never leak between tests and nothing writes
into the session datadir or into ``presets/``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import profiles as prof

DEFAULTS = {"supports": False, "brim": True, "hide_profiles": False,
            "auto_orient": True, "shrink_to_fit": False}
SELECTED_PROCESS = prof.DEFAULT_PROFILE["process"]


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path: Path, datadir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A per-test profile.json and datadir; returns the profile file path."""
    cfg = dataclasses.replace(main.CONFIG,
                              profile_file=tmp_path / "profile.json",
                              orca_datadir=datadir)
    monkeypatch.setattr(main, "CONFIG", cfg)
    return cfg.profile_file


def options_of(response) -> dict:
    """The full option set from a 200 response, asserting the documented shape."""
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["options"]) == set(prof.OPTION_KEYS)
    return body["options"]


# ---------------------------------------------------------------- GET / storage defaults
def test_profile_options_defaults_when_profile_is_missing(client, isolated_profile):
    assert not isolated_profile.exists()

    assert options_of(client.get("/api/profile")) == DEFAULTS
    assert not isolated_profile.exists()          # a plain read never creates the store


def test_legacy_profile_without_options_gets_defaults(client, isolated_profile):
    legacy = {"machine_label": "Shop Floor", "process": SELECTED_PROCESS}
    isolated_profile.write_text(json.dumps(legacy))

    assert options_of(client.get("/api/profile")) == DEFAULTS
    assert json.loads(isolated_profile.read_text()) == legacy


def test_corrupt_profile_json_still_returns_default_options(client, isolated_profile):
    isolated_profile.write_text("{not json at all")

    assert options_of(client.get("/api/profile")) == DEFAULTS


# ---------------------------------------------------------------- POST /api/profile/options
def test_partial_option_updates_merge_and_persist(client, isolated_profile):
    isolated_profile.write_text(json.dumps({"machine_label": "Shop Floor",
                                            "process": SELECTED_PROCESS}))

    first = client.post("/api/profile/options", json={"supports": True})
    assert first.status_code == 200, first.text
    assert first.json() == {"ok": True,
                            "options": {"supports": True, "brim": True,
                                        "hide_profiles": False,
                                        "auto_orient": True, "shrink_to_fit": False}}

    second = client.post("/api/profile/options", json={"brim": False})
    assert second.status_code == 200, second.text
    assert second.json()["options"] == {"supports": True, "brim": False,
                                        "hide_profiles": False,
                                        "auto_orient": True, "shrink_to_fit": False}

    stored = json.loads(isolated_profile.read_text())
    assert stored["options"] == {"supports": True, "brim": False, "hide_profiles": False,
                                 "auto_orient": True, "shrink_to_fit": False}
    assert stored["machine_label"] == "Shop Floor"       # unrelated keys survive the merge
    assert stored["process"] == SELECTED_PROCESS


def test_hide_profiles_preference_round_trips(client, isolated_profile):
    response = client.post("/api/profile/options", json={"hide_profiles": True})
    assert response.status_code == 200, response.text
    assert response.json()["options"]["hide_profiles"] is True

    # A cold client (new app connection) proves the value is on disk, not in memory.
    cold = TestClient(main.app)
    assert options_of(cold.get("/api/profile"))["hide_profiles"] is True
    assert json.loads(isolated_profile.read_text())["options"]["hide_profiles"] is True


def test_options_survive_profile_select(client):
    assert client.post("/api/profile/options",
                       json={"supports": True, "hide_profiles": True}).status_code == 200

    select = client.post("/api/profile/select", json={"process": SELECTED_PROCESS})
    assert select.status_code == 200, select.text

    assert options_of(client.get("/api/profile")) == {"supports": True, "brim": True,
                                                     "hide_profiles": True,
                                                     "auto_orient": True,
                                                     "shrink_to_fit": False}


def test_the_new_options_round_trip_through_the_api(client, isolated_profile):
    response = client.post("/api/profile/options", json={"auto_orient": False})

    assert options_of(response) == {"supports": False, "brim": True, "hide_profiles": False,
                                    "auto_orient": False, "shrink_to_fit": False}
    profile = client.get("/api/profile").json()
    assert profile["options"]["auto_orient"] is False
    assert profile["options"]["shrink_to_fit"] is False
    assert profile["effective"]["auto_orient"] is False


def test_a_partial_update_keeps_the_other_new_option(client, isolated_profile):
    assert client.post("/api/profile/options",
                       json={"shrink_to_fit": True}).status_code == 200
    response = client.post("/api/profile/options", json={"auto_orient": False})

    assert response.status_code == 200, response.text
    stored = json.loads(isolated_profile.read_text())["options"]
    assert stored["shrink_to_fit"] is True
    assert stored["auto_orient"] is False
    profile = client.get("/api/profile").json()
    assert profile["effective"]["shrink_to_fit"] is True
    assert profile["effective"]["auto_orient"] is False


@pytest.mark.parametrize("payload,status,detail", [
    ({}, 400, "nothing to update"),
    ({"supports": "yes"}, 400, "supports must be true or false"),   # a string is not a bool
    ({"supports": 1}, 400, "supports must be true or false"),       # 0/1 are not bools either
    ({"support": True}, 400, "unknown option: support"),            # unknown key (typo)
    ({"auto_orient": "yes"}, 400, "auto_orient must be true or false"),
    ({"shrink_to_fit": 1}, 400, "shrink_to_fit must be true or false"),
    ([], 422, None),                     # non-object body is a FastAPI validation error
    ("x", 422, None),
])
def test_invalid_option_payloads_are_rejected(client, isolated_profile, payload, status,
                                              detail):
    isolated_profile.write_text(json.dumps({"machine_label": "Shop Floor",
                                            "options": DEFAULTS}))
    before = isolated_profile.read_bytes()

    response = client.post("/api/profile/options", json=payload)

    assert response.status_code == status, response.text
    returned = response.json()["detail"]
    if detail is None:
        assert returned                       # 422 carries FastAPI's validation error list
    else:
        assert returned == detail
    assert isolated_profile.read_bytes() == before          # rejection writes nothing


def test_profile_json_write_leaves_no_temp_files(client, isolated_profile):
    response = client.post("/api/profile/options", json={"brim": False})
    assert response.status_code == 200, response.text

    assert isolated_profile.is_file()
    assert list(isolated_profile.parent.glob(isolated_profile.name + ".*.tmp")) == []


# ---------------------------------------------------------------- derived process preset
def test_generated_process_encodes_supports_and_brim(datadir):
    selected = datadir / SELECTED_PROCESS

    path = prof.write_derived_process(selected, datadir,
                                      {"supports": True, "brim": True, "hide_profiles": False,
                                       "auto_orient": True, "shrink_to_fit": False})

    assert path == datadir / prof.PORTAL_PROCESS_REL
    data = json.loads(path.read_text())
    assert data["type"] == "process"
    assert data["from"] == "system"
    assert data["name"].endswith("(portal options)")
    assert data["enable_support"] == "1"
    assert data["brim_type"] == "outer_only"
    assert data["brim_width"] == "5"            # inherited from fdm_process_common.json
    assert "inherits" not in data
    assert data["layer_height"] == "0.2"

    prof.write_derived_process(selected, datadir,
                               {"supports": False, "brim": False, "hide_profiles": False,
                                "auto_orient": True, "shrink_to_fit": False})
    off = json.loads(path.read_text())
    assert off["enable_support"] == "0"
    assert off["brim_type"] == "no_brim"
    assert off["brim_width"] == "0"


def test_the_new_options_do_not_change_the_derived_preset(client, datadir):
    assert client.post("/api/profile/options",
                       json={"shrink_to_fit": True}).status_code == 200
    # The two new switches are command-line flags, so only supports/brim may reach the preset.
    saved = client.post("/api/profile/options",
                        json={"auto_orient": False, "supports": True, "brim": False})
    assert saved.status_code == 200, saved.text

    preset = json.loads((datadir / prof.PORTAL_PROCESS_REL).read_text())
    assert "auto_orient" not in preset
    assert "shrink_to_fit" not in preset
    assert preset["enable_support"] == "1"
    assert preset["brim_type"] == "no_brim"
    assert preset["brim_width"] == "0"


def test_generated_process_preserves_preset_brim_width(datadir):
    selected = datadir / "system/Custom/process/wide-brim.json"
    selected.write_text(json.dumps({"type": "process", "name": "Wide brim",
                                   "inherits": "fdm_process_klipper_common",
                                   "from": "system", "brim_width": "7.5"}))

    path = prof.write_derived_process(selected, datadir,
                                      {"supports": False, "brim": True, "hide_profiles": False,
                                       "auto_orient": True, "shrink_to_fit": False})
    assert json.loads(path.read_text())["brim_width"] == "7.5"

    prof.write_derived_process(selected, datadir,
                               {"supports": False, "brim": False, "hide_profiles": False,
                                "auto_orient": True, "shrink_to_fit": False})
    assert json.loads(path.read_text())["brim_width"] == "0"


def test_generated_process_does_not_mutate_selected_preset(datadir):
    selected = datadir / SELECTED_PROCESS
    before = selected.read_bytes()

    path = prof.write_derived_process(selected, datadir, dict(DEFAULTS))

    assert path != selected
    assert path.name == prof.PORTAL_PROCESS_FILE
    assert selected.read_bytes() == before


def test_generated_process_is_hidden_from_api_profiles(client, datadir):
    prof.write_derived_process(datadir / SELECTED_PROCESS, datadir, dict(DEFAULTS))

    rows = client.get("/api/profiles").json()["process"]

    assert rows
    assert all(row["file"] != prof.PORTAL_PROCESS_FILE for row in rows)


# ---------------------------------------------------------------- slicer wiring
def test_derivation_failure_marks_job_failed_without_slicing(client, jobs_dir: Path,
                                                            isolated_profile: Path,
                                                            cube_stl: bytes,
                                                            monkeypatch: pytest.MonkeyPatch):
    jid = "badc0ffee123"
    upload = jobs_dir / jid / "upload"
    upload.mkdir(parents=True)
    (upload / "cube.stl").write_bytes(cube_stl)
    (jobs_dir / jid / "job.json").write_text(json.dumps(
        {"id": jid, "status": "queued", "filename": "cube.stl"}))
    isolated_profile.write_text(json.dumps(
        {"process": "system/Custom/process/does-not-exist.json"}))
    monkeypatch.setattr(main, "CONFIG",
                        dataclasses.replace(main.CONFIG, profile_file=isolated_profile))

    calls: list[list[str]] = []

    def recorder(cmd, **kwargs):                # reaching the slicer is the failure
        calls.append(cmd)
        raise AssertionError(f"subprocess.run must not be reached: {cmd}")

    monkeypatch.setattr(main.subprocess, "run", recorder)

    main.run_slice(jid)

    assert calls == []
    job = json.loads((jobs_dir / jid / "job.json").read_text())
    assert job["status"] == "failed"
    assert job["error"].startswith("profile: ProfileError")


def test_upload_preserves_options(client, isolated_profile: Path, datadir: Path):
    # Make the existing process universal so the imported machine is a permitted pair.
    process = datadir / SELECTED_PROCESS
    data = json.loads(process.read_text())
    data['compatible_printers'] = []
    process.write_text(json.dumps(data))
    assert client.post("/api/profile/options",
                       json={"supports": True}).status_code == 200

    # A machine preset is the one upload branch that rewrites profile.json, so it is the one
    # that could drop the stored options.
    preset = {"type": "machine", "name": "Shop machine", "printer_model": "Shop 250"}
    uploaded = client.post("/api/profile/upload",
                           files={"file": ("shop-machine.json", json.dumps(preset),
                                            "application/octet-stream")})
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["ok"] is True

    assert options_of(client.get("/api/profile"))["supports"] is True
    stored = json.loads(isolated_profile.read_text())
    assert stored["options"]["supports"] is True
    assert stored["machine_label"] == "Shop machine"      # the upload did rewrite the file


def test_run_slice_uses_single_generated_process_path(jobs_dir: Path, datadir: Path,
                                                      monkeypatch: pytest.MonkeyPatch,
                                                      cube_stl: bytes):
    jid = "deadbeefcafe"
    upload = jobs_dir / jid / "upload"
    upload.mkdir(parents=True)
    (upload / "cube.stl").write_bytes(cube_stl)
    options = prof.read_options({})
    paths = {kind: datadir / rel for kind, rel in prof.DEFAULT_PROFILE.items()}
    machine = prof.effective_preset(paths["machine"], "machine", datadir)
    prof.clamp_machine(machine, main.CONFIG.bed_size)
    values = {"machine": machine,
              "process": prof.derived_process_data(paths["process"], datadir, options),
              "filament": prof.effective_preset(paths["filament"], "filament", datadir)}
    pinned = jobs_dir / jid / "pinned"
    pinned.mkdir()
    hashes = {}
    for kind, value in values.items():
        raw = json.dumps(value, indent=2).encode()
        (pinned / f"{kind}.json").write_bytes(raw)
        hashes[kind] = hashlib.sha256(raw).hexdigest()
    (jobs_dir / jid / "job.json").write_text(json.dumps(
        {"id": jid, "status": "queued", "filename": "cube.stl",
         "pinned_presets": {"version": 1, "sha256": hashes, "options": options}}))
    monkeypatch.setattr(main, "JOBS", jobs_dir)

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        entries = cmd[cmd.index("--load-settings") + 1].split(";")
        captured["process"] = json.loads(Path(entries[1]).read_text())
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main.run_slice(jid)

    cmd = captured["cmd"]
    entries = cmd[cmd.index("--load-settings") + 1].split(";")
    assert len(entries) == 2                     # machine;derived_process, never a third
    assert Path(entries[1]).name == "process.json"
    assert captured["process"]["brim_width"] == "5"
    assert "inherits" not in captured["process"]
    assert not Path(entries[1]).exists()  # the private per-job copy is removed


# ---------------------------------------------------------------- UI wiring
def test_index_exposes_basic_option_controls(client):
    page = client.get("/").text

    for needle in ('id="profileSelectors"', 'id="optionsTitle"', 'id="optSupports"',
                   'id="optBrim"', 'id="optHideProfiles"', "/api/profile/options",
                   "applyProfileVisibility"):
        assert needle in page, needle
