"""profiles.py: the effective supports/brim state, the derived preset's freshness, and `rel` ids.

Every test runs on the private ``datadir`` copy of the shipped seed, so nothing writes into
``presets/`` or into the session datadir. `effective_process` and `derived_state` are the
read-only counterparts of `write_derived_process`; they must agree with it and never raise.
"""
from __future__ import annotations

import json
from pathlib import Path

import profiles as prof

SELECTED_PROCESS = prof.DEFAULT_PROFILE["process"]
DEFAULTS = {"supports": False, "brim": True, "hide_profiles": False}


def write_process(datadir: Path, name: str, data: dict) -> Path:
    path = datadir / "system/Custom/process" / name
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------- effective_process
def test_effective_reports_supports_and_brim_width(datadir):
    effective = prof.effective_process(datadir / SELECTED_PROCESS, datadir,
                                       {"supports": True, "brim": True, "hide_profiles": False})

    assert effective["supports"] is True
    assert effective["brim"] is True
    assert effective["brim_width"] == "5"          # inherited from fdm_process_common.json
    assert effective["machine"] == "Generic Klipper 0.4 nozzle"
    assert effective["process"] == "0.20mm Standard @Generic Klipper (portal options)"
    assert effective["process_rel"] == prof.PORTAL_PROCESS_REL


def test_effective_with_brim_off_has_no_width(datadir):
    effective = prof.effective_process(datadir / SELECTED_PROCESS, datadir,
                                       {**DEFAULTS, "brim": False})

    assert effective["supports"] is False
    assert effective["brim"] is False
    assert effective["brim_width"] is None


def test_effective_machine_falls_back_to_the_slot_when_compatible_printers_is_absent(datadir):
    preset = write_process(datadir, "no-compat.json",
                           {"type": "process", "name": "No compat",
                            "inherits": "fdm_process_klipper_common", "from": "system"})

    effective = prof.effective_process(preset, datadir, dict(DEFAULTS))

    slot_name = json.loads((datadir / prof.machine_slot(datadir)).read_text())["name"]
    assert effective["machine"] == slot_name


# ---------------------------------------------------------------- derived_state
def test_derived_state_is_stale_before_the_first_write(datadir):
    state = prof.derived_state(datadir / SELECTED_PROCESS, datadir, dict(DEFAULTS))

    assert state["path"] == prof.PORTAL_PROCESS_REL
    assert state["stale"] is True
    assert state["error"]


def test_derived_state_is_fresh_after_write_derived_process(datadir):
    prof.write_derived_process(datadir / SELECTED_PROCESS, datadir,
                               {"supports": True, "brim": True, "hide_profiles": False})

    state = prof.derived_state(datadir / SELECTED_PROCESS, datadir,
                               {"supports": True, "brim": True, "hide_profiles": False})

    assert state == {"path": prof.PORTAL_PROCESS_REL, "stale": False, "error": None}


def test_derived_state_goes_stale_when_options_change(datadir):
    selected = datadir / SELECTED_PROCESS
    prof.write_derived_process(selected, datadir, dict(DEFAULTS))

    state = prof.derived_state(selected, datadir, {**DEFAULTS, "brim": False})

    assert state["stale"] is True
    assert state["error"] is None                  # readable, just out of date


# ---------------------------------------------------------------- datadir-relative ids
def test_list_presets_exposes_datadir_relative_paths(datadir):
    rows = prof.list_presets(datadir)["process"]

    assert rows
    assert rows[0]["rel"] == "system/Custom/process/generic-0.20-standard.json"


def test_summarise_without_datadir_has_no_rel(datadir):
    row = prof.summarise(datadir / SELECTED_PROCESS, "process")

    assert "rel" not in row
    assert row["file"] == "generic-0.20-standard.json"
