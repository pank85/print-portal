"""profiles.py: bundle import, bed clamping, inherits re-rooting, machine rebase.

Every test works on a private copy of the shipped seed datadir (the ``datadir`` fixture),
because import_into() writes presets and rewrites the machine slot in place.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

import profiles as prof

ROOT = Path(__file__).resolve().parents[1]
MACHINE_NAME = "Generic Klipper 0.4 nozzle"

# Strings the brief bans from shipped files: the author's printer subnet, home paths and
# preset names. Assembled from fragments on purpose — typing them out would make this file
# itself fail the repo-wide grep that enforces the same rule.
_BANNED_FRAGMENTS = (
    "172.16" + ".",
    "/home/" + "pan" + "k",
    "My" + "Klipper",
    "End" + "er",
    "Crea" + "lity",
    "Ja" + "yo",
    "pan" + "k",
    "pkatopod" + "is",
)


def bundle(files: dict[str, dict]) -> bytes:
    """A `.orca_printer`-shaped zip: printer/ process/ filament folders of preset json."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, json.dumps(data))
    return buf.getvalue()


def test_bundle_zip_imports_every_preset_type(datadir):
    slot = prof.machine_slot(datadir)
    blob = bundle({
        "printer/vendor-machine.json": {
            "name": "Vendor Mach", "inherits": "fdm_klipper_common",
            "printer_model": "Vendor", "printer_variant": "0.6",
            "printable_area": ["0x0", "200x0", "200x200", "0x200"],
            "retraction_length": ["1.5"], "nozzle_diameter": ["0.6"],
        },
        "process/vendor 0.28.json": {
            "name": "vendor 0.28", "inherits": "Vendor Profile",
            "layer_height": "0.28", "wall_loops": "3",
        },
        "filament/vendor pla.json": {
            "name": "vendor pla", "inherits": "Vendor PLA",
            "filament_type": ["PLA"], "nozzle_temperature": ["215"],
        },
    })

    result = prof.import_into(datadir, blob, "vendor.orca_printer", slot, (220.0, 220.0))

    assert result["ok"] is True
    assert [item["type"] for item in result["imported"]] == ["machine", "process", "filament"]
    assert result["imported"][0]["path"] == str(slot)

    process_file = datadir / "system/Custom/process/vendor_0.28.json"
    filament_file = datadir / "system/Custom/filament/vendor_pla.json"
    assert process_file.is_file() and filament_file.is_file()

    process = json.loads(process_file.read_text())
    assert process["type"] == "process"
    assert process["compatible_printers"] == [MACHINE_NAME]
    assert process["inherits"] == "fdm_process_klipper_common"

    filament = json.loads(filament_file.read_text())
    assert filament["type"] == "filament"
    assert filament["compatible_printers"] == [MACHINE_NAME]

    # the machine never becomes its own file — it is applied onto the slot
    assert not list((datadir / "system/Custom/machine").glob("Vendor*"))


def test_machine_rebase_keeps_the_identity_keys(datadir):
    slot = prof.machine_slot(datadir)
    before = json.loads((datadir / slot).read_text())
    identity = {key: before[key] for key in prof.MACHINE_IDENTITY if key in before}
    assert {"name", "inherits", "type", "printer_model", "printer_variant"} <= set(identity)

    blob = bundle({
        "printer/vendor.json": {
            "name": "Vendor Mach", "inherits": "Vendor Base",
            "printer_model": "Vendor", "printer_variant": "0.6",
            "retraction_length": ["1.5"], "nozzle_diameter": ["0.6"],
        },
    })

    prof.import_into(datadir, blob, "vendor.orca_printer", slot, (220.0, 220.0))

    after = json.loads((datadir / slot).read_text())
    for key, value in identity.items():
        assert after[key] == value, f"{key} must survive the rebase"
    assert after["retraction_length"] == ["1.5"]
    assert after["nozzle_diameter"] == ["0.6"]


def test_machine_rebase_repoints_the_slot_defaults(datadir):
    slot = prof.machine_slot(datadir)
    blob = bundle({
        "printer/vendor.json": {"name": "Vendor Mach", "retraction_length": ["1.5"]},
        "process/vendor 0.28.json": {"name": "vendor 0.28", "layer_height": "0.28"},
        "filament/vendor pla.json": {"name": "vendor pla", "filament_type": ["PLA"]},
    })

    prof.import_into(datadir, blob, "vendor.orca_printer", slot, (220.0, 220.0))

    after = json.loads((datadir / slot).read_text())
    assert after["default_print_profile"] == "vendor 0.28"
    assert after["default_filament_profile"] == ["vendor pla"]


def test_read_bundle_rejects_garbage():
    with pytest.raises(prof.ProfileError, match="not a readable bundle"):
        prof.read_bundle(b"definitely not a zip file", "vendor.orca_printer")


def test_read_bundle_rejects_a_json_file_that_is_not_a_preset():
    with pytest.raises(prof.ProfileError, match="not a readable bundle"):
        prof.read_bundle(b"[1, 2, 3]", "vendor.json")


def test_read_bundle_skips_a_member_over_the_bundle_cap(monkeypatch):
    monkeypatch.setattr(prof, "BUNDLE_MAX_BYTES", 64)
    big = {"name": "big", "layer_height": "0.2", "wall_loops": "3",
           "sparse_infill_density": "15%", "print_speed": "120"}
    assert len(json.dumps(big)) > 64                     # above the patched cap on purpose

    oversized = bundle({"process/big.json": big})
    assert prof.read_bundle(oversized, "vendor.orca_printer") == []

    small = bundle({"process/small.json": {"layer_height": "0.2"}})
    assert [ptype for ptype, _ in prof.read_bundle(small, "vendor.orca_printer")] == ["process"]


def test_printable_area_over_the_bed_is_clamped():
    area = ["0x0", "350x0", "350x350", "0x350"]

    preset, notes = prof.normalise(
        {"name": "Big", "printable_area": area}, "machine", MACHINE_NAME, {}, (220.0, 220.0))

    assert preset["printable_area"] == ["0x0", "220x0", "220x220", "0x220"]
    assert any("350x350" in note and "220x220" in note and "clamped" in note for note in notes)


def test_printable_area_inside_the_bed_is_untouched():
    area = ["0x0", "200x0", "200x180", "0x180"]

    preset, notes = prof.normalise(
        {"name": "Small", "printable_area": area}, "machine", MACHINE_NAME, {}, (220.0, 220.0))

    assert preset["printable_area"] == area
    assert not any("clamped" in note for note in notes)


def test_a_300_bed_does_not_clamp_a_300_area():
    area = ["0x0", "300x0", "300x300", "0x300"]

    preset, notes = prof.normalise(
        {"name": "Large", "printable_area": area}, "machine", MACHINE_NAME, {}, (300.0, 300.0))

    assert preset["printable_area"] == area
    assert not any("clamped" in note for note in notes)


def test_clamp_area_edge_cases():
    assert prof.clamp_area(["0x0", "350x0", "350x350", "0x350"], (300.0, 300.0)) == [
        "0x0", "300x0", "300x300", "0x300"]
    assert prof.clamp_area(["0x0", "300x0", "300x300", "0x300"], (300.0, 300.0)) is None
    # 1 mm of tolerance is allowed before clamping
    assert prof.clamp_area(["0x0", "221x0", "221x221", "0x221"], (220.0, 220.0)) is None
    assert prof.clamp_area(["nonsense"], (220.0, 220.0)) is None


def test_unknown_parent_is_re_rooted_to_a_base_that_exists():
    preset, notes = prof.normalise(
        {"name": "Imported", "inherits": "Vendor Pla @0.4", "layer_height": "0.2"},
        "process", MACHINE_NAME, {}, (220.0, 220.0))

    assert preset["inherits"] == "fdm_process_klipper_common"
    assert any("Vendor Pla @0.4" in note and "re-rooted" in note for note in notes)


def test_missing_parent_is_set_to_the_base():
    preset, notes = prof.normalise(
        {"name": "Bare"}, "filament", MACHINE_NAME, {}, (220.0, 220.0))

    assert preset["inherits"] == "fdm_filament_pla"
    assert any("re-rooted" in note or "no inherits" in note for note in notes)


def test_a_known_parent_is_kept():
    known = {"process": {"fdm_process_klipper_common"}}

    preset, notes = prof.normalise(
        {"name": "Kept", "inherits": "fdm_process_klipper_common", "layer_height": "0.2"},
        "process", MACHINE_NAME, known, (220.0, 220.0))

    assert preset["inherits"] == "fdm_process_klipper_common"
    assert notes == []


def test_machine_slot_defaults_to_the_shipped_machine(datadir):
    assert prof.machine_slot(datadir) == Path(prof.DEFAULT_PROFILE["machine"])


def test_machine_slot_override_is_honoured(datadir):
    override = prof.DEFAULT_PROFILE["machine"]

    assert prof.machine_slot(datadir, override) == Path(override)


def test_machine_slot_override_must_exist(datadir):
    with pytest.raises(prof.ProfileError, match="does not exist"):
        prof.machine_slot(datadir, "system/Custom/machine/not-here.json")


def test_machine_slot_override_must_be_a_machine_preset(datadir):
    with pytest.raises(prof.ProfileError, match="not a machine preset"):
        prof.machine_slot(datadir, prof.DEFAULT_PROFILE["process"])


def test_machine_slot_override_cannot_escape_the_datadir(datadir):
    with pytest.raises(prof.ProfileError, match="relative path inside the datadir"):
        prof.machine_slot(datadir, "/etc/passwd")


def test_a_datadir_without_a_machine_preset_says_how_to_fix_it(tmp_path):
    with pytest.raises(prof.ProfileError, match="set MACHINE_SLOT"):
        prof.machine_slot(tmp_path)


def test_list_presets_shows_exactly_one_of_each_on_the_seed(datadir):
    presets = prof.list_presets(datadir)

    assert {kind: len(rows) for kind, rows in presets.items()} == {
        "machine": 1, "process": 1, "filament": 1}
    assert presets["machine"][0]["name"] == MACHINE_NAME
    # the machine_model file (type "machine_model") must not be selectable
    assert all(row["name"] != "Generic Klipper Printer" for row in presets["machine"])


def test_profiles_module_carries_no_personal_strings():
    source = (ROOT / "app" / "profiles.py").read_text()

    found = [needle for needle in _BANNED_FRAGMENTS if needle in source]
    assert found == []
