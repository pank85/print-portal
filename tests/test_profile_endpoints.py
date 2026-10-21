"""GET/POST profile endpoints: effective state, the derived preset, and the machine filter.

``isolated_profile`` gives every test its own ``profile.json`` and a private ``datadir``
copy, so nothing writes into ``presets/`` or into the session datadir. The filter tests add
their own presets to that copy: the shipped seed only has one process preset, which cannot
show a preset being hidden.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import main
import profiles as prof

MACHINE_REL = prof.DEFAULT_PROFILE["machine"]
PROCESS_REL = prof.DEFAULT_PROFILE["process"]
FILAMENT_REL = prof.DEFAULT_PROFILE["filament"]
MACHINE_NAME = "Generic Klipper 0.4 nozzle"
OTHER_MACHINE_REL = "system/Custom/machine/generic-klipper.json"   # a machine_model preset


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path: Path, datadir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = dataclasses.replace(main.CONFIG, profile_file=tmp_path / "profile.json",
                              orca_datadir=datadir)
    monkeypatch.setattr(main, "CONFIG", cfg)
    return cfg.profile_file


def write_preset(datadir: Path, kind: str, filename: str, name: str, **fields) -> Path:
    path = datadir / "system" / "Custom" / kind / filename
    data = {"name": name, "type": kind, "instantiation": "true", **fields}
    path.write_text(json.dumps(data, indent="\t"))
    return path


def process_names(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [row["name"] for row in response.json()["process"]]


# ---------------------------------------------------------------- GET /api/profile
def test_profile_reports_effective_supports_and_brim(client, datadir):
    assert client.post("/api/profile/options",
                       json={"supports": True, "brim": True}).status_code == 200

    body = client.get("/api/profile").json()

    assert set(body) == {"source", "machine_label", "paths", "presets", "options",
                         "effective", "derived"}
    assert body["effective"] == {"supports": True, "brim": True, "brim_width": "5",
                                 "machine": MACHINE_NAME,
                                 "process": f"{body['presets']['process']['name']} (portal options)",
                                 "process_rel": prof.PORTAL_PROCESS_REL}
    assert body["derived"] == {"path": prof.PORTAL_PROCESS_REL, "stale": False, "error": None}
    assert (datadir / prof.PORTAL_PROCESS_REL).is_file()


def test_profile_reports_a_fresh_derived_preset_after_an_option_save(client, datadir):
    client.post("/api/profile/options", json={"supports": True, "brim": False})

    derived = client.get("/api/profile").json()["derived"]

    assert derived["stale"] is False
    preset = json.loads((datadir / prof.PORTAL_PROCESS_REL).read_text())
    assert preset["enable_support"] == "1"
    assert preset["brim_type"] == "no_brim"
    assert preset["brim_width"] == "0"


def test_option_save_survives_a_missing_process_preset(client, isolated_profile):
    isolated_profile.write_text(json.dumps({"machine": MACHINE_REL,
                                            "process": "system/Custom/process/gone.json"}))

    response = client.post("/api/profile/options", json={"supports": True})

    assert response.status_code == 200, response.text
    assert response.json()["options"]["supports"] is True
    derived = client.get("/api/profile").json()["derived"]
    assert derived["stale"] is True
    assert derived["error"] == "process preset is not readable"


def test_option_save_response_shape_is_unchanged(client, isolated_profile):
    response = client.post("/api/profile/options", json={"brim": False})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"ok", "options"}
    assert body["ok"] is True
    assert set(body["options"]) == set(prof.OPTION_KEYS)
    assert body["options"] == {"supports": False, "brim": False, "hide_profiles": False}
    assert json.loads(isolated_profile.read_text())["options"] == body["options"]


# ---------------------------------------------------------------- GET /api/profiles
def test_profiles_endpoint_filters_process_by_machine(client, datadir, isolated_profile):
    write_preset(datadir, "process", "voron-only.json", "0.20mm Voron",
                 compatible_printers=["Voron 2.4 350"])

    unfiltered = process_names(client.get("/api/profiles"))
    filtered = process_names(client.get("/api/profiles", params={"machine": MACHINE_REL}))

    assert "0.20mm Voron" in unfiltered
    assert "0.20mm Voron" not in filtered
    assert "0.20mm Standard @Generic Klipper" in filtered
    assert len(filtered) < len(unfiltered)      # the filter actually hides something

    # only the process list is filtered
    both = client.get("/api/profiles", params={"machine": MACHINE_REL}).json()
    alone = client.get("/api/profiles").json()
    assert both["machine"] == alone["machine"]
    assert both["filament"] == alone["filament"]

    # the active process never vanishes: selecting an incompatible preset keeps it listed
    isolated_profile.write_text(json.dumps(
        {"machine": MACHINE_REL, "process": "system/Custom/process/voron-only.json"}))
    assert "0.20mm Voron" in process_names(
        client.get("/api/profiles", params={"machine": MACHINE_REL}))


def test_profiles_endpoint_keeps_universal_process_presets(client, datadir):
    write_preset(datadir, "process", "universal.json", "0.20mm Universal")
    write_preset(datadir, "process", "lenient.json", "0.20mm Klipper",
                 compatible_printers=["GENERIC KLIPPER 0.4 NOZZLE*"])
    write_preset(datadir, "process", "voron-only.json", "0.20mm Voron",
                 compatible_printers=["Voron 2.4 350"])

    filtered = process_names(client.get("/api/profiles", params={"machine": MACHINE_REL}))

    assert "0.20mm Universal" in filtered         # no compatible_printers = every machine
    assert "0.20mm Klipper" in filtered           # case and a trailing '*' are tolerated
    assert "0.20mm Voron" not in filtered


def test_profiles_endpoint_ignores_an_unknown_machine(client):
    unfiltered = client.get("/api/profiles")
    unknown = client.get("/api/profiles",
                         params={"machine": "system/Custom/machine/missing.json"})

    assert unknown.status_code == 200
    assert unknown.json() == unfiltered.json()


def test_profiles_endpoint_ignores_a_wrong_typed_machine_filter(client, datadir):
    """A process/filament path is not a machine: treating it as one hid every process."""
    write_preset(datadir, "process", "voron-only.json", "0.20mm Voron",
                 compatible_printers=["Voron 2.4 350"])
    unfiltered = client.get("/api/profiles")

    for rel in (PROCESS_REL, FILAMENT_REL):
        response = client.get("/api/profiles", params={"machine": rel})

        assert response.status_code == 200, response.text
        assert response.json() == unfiltered.json()
        assert "0.20mm Voron" in process_names(response)


def test_profiles_endpoint_is_unfiltered_without_the_param(client, datadir):
    write_preset(datadir, "process", "voron-only.json", "0.20mm Voron",
                 compatible_printers=["Voron 2.4 350"])

    response = client.get("/api/profiles")

    assert response.status_code == 200
    assert response.json() == prof.list_presets(datadir)
    assert "0.20mm Voron" in process_names(response)


def test_profile_select_rejects_a_wrong_typed_slot(client, isolated_profile):
    """`/api/profile/select` checks the kind of every slot, like the printer pairing does."""
    before = isolated_profile.exists()

    for body in ({"machine": PROCESS_REL}, {"process": MACHINE_REL},
                 {"filament": PROCESS_REL}, {"machine": OTHER_MACHINE_REL}):
        response = client.post("/api/profile/select", json=body)

        assert response.status_code == 400, (body, response.text)
        assert "not a preset inside the datadir" in response.json()["detail"]
    assert isolated_profile.exists() is before


def test_profile_select_rejects_a_non_string_path(client, isolated_profile):
    """A JSON number or list in a preset slot is a 400, not a 500 out of `Path`."""
    for body in ({"machine": 5}, {"process": ["a", "b"]}, {"filament": {"rel": "x"}}):
        response = client.post("/api/profile/select", json=body)

        assert response.status_code == 400, (body, response.text)
        assert "not a preset inside the datadir" in response.json()["detail"]
    assert not isolated_profile.exists()


def test_profile_select_regenerates_the_derived_preset(client, datadir):
    """Switching the process must rewrite the derived preset the slicer loads.

    `derived.stale` only compares the option fields, so without the regeneration it kept
    reporting a fresh file while that file still carried the old process's name.
    """
    client.post("/api/profile/options", json={"supports": True})
    replacement = write_preset(datadir, "process", "voron-fast.json", "0.20mm Voron Fast",
                               compatible_printers=["Voron 2.4 350"])
    rel = replacement.relative_to(datadir).as_posix()

    response = client.post("/api/profile/select", json={"process": rel})

    assert response.status_code == 200, response.text
    preset = json.loads((datadir / prof.PORTAL_PROCESS_REL).read_text())
    assert preset["name"] == "0.20mm Voron Fast (portal options)"
    assert preset["enable_support"] == "1"             # the stored options came along
    body = client.get("/api/profile").json()
    assert body["effective"]["process"] == "0.20mm Voron Fast (portal options)"
    assert body["derived"]["stale"] is False


def test_process_rows_expose_the_compatible_printers_list(client):
    row = next(item for item in client.get("/api/profiles").json()["process"]
               if item["name"] == "0.20mm Standard @Generic Klipper")

    assert row["compatible_printers"] == [MACHINE_NAME]
    assert row["rel"] == PROCESS_REL
    assert FILAMENT_REL in {item["rel"] for item in client.get("/api/profiles").json()["filament"]}
