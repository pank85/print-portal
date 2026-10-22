"""profiles.py: bundle import into an empty datadir, bed clamping, `inherits` kept verbatim.

Two datadir fixtures, and the difference matters:

* ``empty_datadir`` is a copy of the shipped seed, which holds no preset at all — what a
  fresh install has and what an import has to fill. Every import test starts there,
  because re-importing onto existing presets is a different, collision-guarded path.
* ``datadir`` is the trimmed copy of the presets the old seed used to ship; the machine-slot
  override tests still need it, since an override has to resolve *something*.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

import profiles as prof

ROOT = Path(__file__).resolve().parents[1]
MACHINE_NAME = "Generic Klipper 0.4 nozzle"           # the machine in `datadir`, not the seed

# What the committed fixture bundle (tests/fixtures/portal-import.orca_printer) carries, and
# where import writes it. The filenames are `safe_stem()` of the names, spaces included.
PORTAL_MACHINE = "Portal Test Printer"
PORTAL_PROCESS = "0.20mm Portal Standard"
PORTAL_FILAMENT = "Portal PLA"
IMPORTED_MACHINE_REL = "system/Custom/machine/Portal_Test_Printer.json"
IMPORTED_PROCESS_REL = "system/Custom/process/0.20mm_Portal_Standard.json"
IMPORTED_FILAMENT_REL = "system/Custom/filament/Portal_PLA.json"
SHIPPED_INDEX = "system/Custom.json"

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


def machine_bundle(fields: dict | None = None) -> bytes:
    """A one-preset bundle whose machine is named like the fixture's, plus ``fields``."""
    data = {"type": "machine", "name": PORTAL_MACHINE, "printer_model": PORTAL_MACHINE,
            "printer_variant": "0.4", "nozzle_diameter": ["0.4"],
            "printable_area": ["0x0", "220x0", "220x220", "0x220"]}
    data.update(fields or {})
    return bundle({"printer/portal.json": data})


def listing(datadir: Path) -> list[str]:
    """Every file under ``datadir``, relative and sorted — "what got written" as a value."""
    return sorted(path.relative_to(datadir).as_posix()
                  for path in datadir.rglob("*") if path.is_file())


def digests(datadir: Path) -> dict[str, str]:
    """sha256 per relative path: proves an import left the bytes alone, not just the names."""
    return {rel: hashlib.sha256((datadir / rel).read_bytes()).hexdigest()
            for rel in listing(datadir)}


def test_bundle_zip_imports_every_preset_type(empty_datadir):
    blob = bundle({
        "printer/vendor-machine.json": {
            "name": "Vendor Mach", "inherits": "fdm_klipper_common",
            "printer_model": "Vendor", "printer_variant": "0.6",
            "printable_area": ["0x0", "200x0", "200x200", "0x200"],
            "retraction_length": ["1.5"], "nozzle_diameter": ["0.6"],
        },
        "process/vendor 0.28.json": {
            "name": "vendor 0.28", "inherits": "fdm_process_klipper_common",
            "layer_height": "0.28", "wall_loops": "3",
        },
        "filament/vendor pla.json": {
            "name": "vendor pla", "filament_type": ["PLA"], "nozzle_temperature": ["215"],
        },
    })

    result = prof.import_into(empty_datadir, blob, "vendor.orca_printer")

    assert result["ok"] is True
    assert [item["type"] for item in result["imported"]] == ["machine", "process", "filament"]
    # the machine becomes a file of its own, named after itself — never applied onto a slot
    assert result["imported"][0]["path"] == "system/Custom/machine/Vendor_Mach.json"
    machine = json.loads((empty_datadir / "system/Custom/machine/Vendor_Mach.json").read_text())
    assert machine["name"] == "Vendor Mach"
    assert machine["from"] == "system"
    assert machine["instantiation"] == "true"
    assert machine["compatible_printers"] == ["Vendor Mach"]

    # the bundle's process and filament name the machine the same bundle brought
    for rel, kind in (("system/Custom/process/vendor_0.28.json", "process"),
                      ("system/Custom/filament/vendor_pla.json", "filament")):
        preset = json.loads((empty_datadir / rel).read_text())
        assert preset["type"] == kind
        assert preset["compatible_printers"] == ["Vendor Mach"]

    index = json.loads((empty_datadir / SHIPPED_INDEX).read_text())
    assert index["machine_list"] == [{"name": "Vendor Mach",
                                     "sub_path": "machine/Vendor_Mach.json"}]


def test_import_into_an_empty_datadir_writes_only_the_bundle_files(empty_datadir, bundle_bytes):
    assert listing(empty_datadir) == [SHIPPED_INDEX]

    result = prof.import_into(empty_datadir, bundle_bytes, "portal-import.orca_printer")

    assert result["ok"] is True
    assert [row["name"] for row in result["imported"]] == [PORTAL_MACHINE, PORTAL_PROCESS,
                                                           PORTAL_FILAMENT]
    assert listing(empty_datadir) == sorted([SHIPPED_INDEX, IMPORTED_MACHINE_REL,
                                             IMPORTED_PROCESS_REL, IMPORTED_FILAMENT_REL])
    # the options preset is written per slice, not per import
    assert not (empty_datadir / prof.PORTAL_PROCESS_REL).exists()


def test_a_machine_preset_carries_from_system_verbatim(empty_datadir):
    """"user" in the bundle is overwritten, not defaulted: the CLI answers -17 for it."""
    prof.import_into(empty_datadir, machine_bundle({"from": "user"}), "portal.orca_printer")

    machine = json.loads((empty_datadir / IMPORTED_MACHINE_REL).read_text())

    assert machine["from"] == "system"
    assert machine["instantiation"] == "true"


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
        {"name": "Big", "printable_area": area}, "machine", MACHINE_NAME, (220.0, 220.0))

    assert preset["printable_area"] == ["0x0", "220x0", "220x220", "0x220"]
    assert any("350x350" in note and "220x220" in note and "clamped" in note for note in notes)


def test_printable_area_inside_the_bed_is_untouched():
    area = ["0x0", "200x0", "200x180", "0x180"]

    preset, notes = prof.normalise(
        {"name": "Small", "printable_area": area}, "machine", MACHINE_NAME, (220.0, 220.0))

    assert preset["printable_area"] == area
    assert not any("clamped" in note for note in notes)


def test_a_300_bed_does_not_clamp_a_300_area():
    area = ["0x0", "300x0", "300x300", "0x300"]

    preset, notes = prof.normalise(
        {"name": "Large", "printable_area": area}, "machine", MACHINE_NAME, (300.0, 300.0))

    assert preset["printable_area"] == area
    assert not any("clamped" in note for note in notes)


def test_clamp_area_edge_cases():
    assert prof.clamp_area(["0x0", "350x0", "350x350", "0x350"], (300.0, 300.0)) == [
        "0x0", "300x0", "300x300", "0x300"]
    assert prof.clamp_area(["0x0", "300x0", "300x300", "0x300"], (300.0, 300.0)) is None
    # 1 mm of tolerance is allowed before clamping
    assert prof.clamp_area(["0x0", "221x0", "221x221", "0x221"], (220.0, 220.0)) is None
    assert prof.clamp_area(["nonsense"], (220.0, 220.0)) is None


def test_a_dangling_inherits_is_kept_verbatim():
    """A parent this datadir does not ship is tolerated, so it is copied in untouched."""
    preset, notes = prof.normalise(
        {"name": "Imported", "inherits": "Vendor Pla @0.4", "layer_height": "0.2"},
        "process", MACHINE_NAME)

    assert preset["inherits"] == "Vendor Pla @0.4"
    assert notes == ["inherits 'Vendor Pla @0.4' kept as-is"]


def test_a_preset_without_inherits_gets_none():
    """No `inherits` is invented: the preset stays standalone instead of gaining a parent."""
    preset, notes = prof.normalise({"name": "Bare"}, "filament", MACHINE_NAME)

    assert "inherits" not in preset
    assert not any("inherits" in note for note in notes)


def test_a_known_parent_is_kept(datadir):
    """A parent the datadir really ships is still copied verbatim — nothing is re-rooted."""
    known = "fdm_process_klipper_common"
    assert (datadir / "system/Custom/process" / f"{known}.json").is_file()

    preset, notes = prof.normalise(
        {"name": "Kept", "inherits": known, "layer_height": "0.2"}, "process", MACHINE_NAME)

    assert preset["inherits"] == known
    assert notes == [f"inherits '{known}' kept as-is"]


def test_importing_the_same_bundle_twice_is_idempotent(empty_datadir, bundle_bytes):
    prof.import_into(empty_datadir, bundle_bytes, "portal-import.orca_printer")
    first = digests(empty_datadir)

    second = prof.import_into(empty_datadir, bundle_bytes, "portal-import.orca_printer")

    assert second["ok"] is True
    assert digests(empty_datadir) == first      # the same bytes, not just the same names
    index = json.loads((empty_datadir / SHIPPED_INDEX).read_text())
    assert [len(index[key]) for key in ("machine_list", "process_list", "filament_list")] \
        == [1, 1, 1]
    assert {kind: len(rows) for kind, rows in prof.list_presets(empty_datadir).items()} == {
        "machine": 1, "process": 1, "filament": 1}


def test_a_different_preset_with_the_same_name_is_a_400_and_writes_nothing(empty_datadir,
                                                                         bundle_bytes):
    """A different printer reusing an imported name is refused, and the message names the file.

    Overwriting would silently replace the machine the operator imported; the destination
    path is in the text because the only way out is deleting that one file.
    """
    prof.import_into(empty_datadir, bundle_bytes, "portal-import.orca_printer")
    before = digests(empty_datadir)

    with pytest.raises(prof.ProfileError) as refused:
        prof.import_into(empty_datadir, machine_bundle({"nozzle_diameter": ["0.6"]}),
                         "other.orca_printer")

    assert str(empty_datadir / IMPORTED_MACHINE_REL) in str(refused.value)
    assert digests(empty_datadir) == before     # nothing written, not even a partial file


def test_one_bundle_naming_one_destination_twice_is_refused(empty_datadir):
    """Two bundle members with one name but different bodies map to one file; nothing is written.

    The second write would silently replace the first, so the whole import is refused before
    any file is touched and the operator renames one of them.
    """
    blob = bundle({
        "process/a.json": {"type": "process", "name": "Dup", "layer_height": "0.2"},
        "process/b.json": {"type": "process", "name": "Dup", "layer_height": "0.3"},
    })
    before = digests(empty_datadir)

    with pytest.raises(prof.ProfileError, match="written twice"):
        prof.import_into(empty_datadir, blob, "dup.orca_printer")

    assert digests(empty_datadir) == before     # no preset file, no index row


def test_one_bundle_naming_one_destination_twice_with_one_body_imports_once(empty_datadir):
    """The identical document collapses onto that one destination instead of being refused."""
    dup = {"type": "process", "name": "Dup", "layer_height": "0.2"}
    blob = bundle({"process/a.json": dup, "process/b.json": dup})

    result = prof.import_into(empty_datadir, blob, "dup.orca_printer")

    assert result["ok"] is True
    assert [row["path"] for row in result["imported"]] == ["system/Custom/process/Dup.json"]
    assert listing(empty_datadir) == sorted([SHIPPED_INDEX, "system/Custom/process/Dup.json"])
    assert len(prof.list_presets(empty_datadir)["process"]) == 1
    index = json.loads((empty_datadir / SHIPPED_INDEX).read_text())
    assert index["process_list"] == [{"name": "Dup", "sub_path": "process/Dup.json"}]


def test_the_vendor_index_keeps_every_other_key(empty_datadir, bundle_bytes):
    """Import read-modify-writes `system/Custom.json`: every key it does not own survives."""
    index_path = empty_datadir / SHIPPED_INDEX
    seeded = json.loads(index_path.read_text())
    seeded["machine_model_list"] = [{"name": "Keep Me", "sub_path": "machine/keep.json"}]
    index_path.write_text(json.dumps(seeded, indent=2))

    prof.import_into(empty_datadir, bundle_bytes, "portal-import.orca_printer")

    index = json.loads(index_path.read_text())
    ours = ("machine_list", "process_list", "filament_list")
    assert {k: v for k, v in index.items() if k not in ours} == {
        k: v for k, v in seeded.items() if k not in ours}
    assert index["machine_model_list"] == [{"name": "Keep Me",
                                           "sub_path": "machine/keep.json"}]
    assert [row["name"] for row in index["machine_list"]] == [PORTAL_MACHINE]


def test_a_second_bundle_adds_a_second_machine_and_touches_nothing_else(empty_datadir,
                                                                      bundle_bytes):
    prof.import_into(empty_datadir, bundle_bytes, "portal-import.orca_printer")
    before = digests(empty_datadir)

    second = bundle({
        "printer/portal-two.json": {
            "name": "Portal Test Printer Two", "printer_model": "Portal Test Printer Two",
            "printer_variant": "0.4", "nozzle_diameter": ["0.4"],
            "printable_area": ["0x0", "220x0", "220x220", "0x220"],
        },
        "process/portal-two 0.20.json": {
            "name": "0.20mm Portal Two", "layer_height": "0.2", "wall_loops": "3",
        },
        "filament/portal petg.json": {
            "name": "Portal PETG", "filament_type": ["PETG"], "nozzle_temperature": ["240"],
        },
    })

    result = prof.import_into(empty_datadir, second, "portal-two.orca_printer")

    assert result["ok"] is True
    assert (empty_datadir / "system/Custom/machine/Portal_Test_Printer_Two.json").is_file()
    assert (empty_datadir / IMPORTED_MACHINE_REL).is_file()      # the first one is still there
    index = json.loads((empty_datadir / SHIPPED_INDEX).read_text())
    assert [row["name"] for row in index["machine_list"]] == [
        PORTAL_MACHINE, "Portal Test Printer Two"]

    after = digests(empty_datadir)
    assert set(before) <= set(after)                            # B only adds files
    assert {rel for rel in before if after[rel] != before[rel]} == {SHIPPED_INDEX}
    assert len(prof.list_presets(empty_datadir)["machine"]) == 2


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


def test_machine_slot_returns_none_without_a_machine(empty_datadir):
    """No machine is an empty datadir, not a broken one: None lets the caller pick the answer."""
    assert prof.machine_slot(empty_datadir) is None


def test_list_presets_on_the_empty_seed_is_three_empty_lists(empty_datadir):
    assert prof.list_presets(empty_datadir) == {"machine": [], "process": [], "filament": []}


def test_bed_limits_reads_the_preset(datadir):
    limits = prof.bed_limits(datadir / prof.DEFAULT_PROFILE["machine"])

    assert (limits.xmin, limits.ymin, limits.xmax, limits.ymax, limits.zmax) == (
        0.0, 0.0, 220.0, 220.0, 250.0)
    assert limits.size == (220.0, 220.0, 250.0)
    assert limits.machine == MACHINE_NAME


def test_bed_limits_keeps_the_min_corner(tmp_path):
    """An offset bed is 220 mm across, not 230: the min corner is where the span starts."""
    machine = tmp_path / "offset-bed.json"
    machine.write_text(json.dumps({
        "name": "Offset Bed",
        "printable_area": ["10x10", "230x10", "230x230", "10x230"],
        "printable_height": "300",
    }))

    limits = prof.bed_limits(machine)

    assert limits.xmin == limits.ymin == 10.0
    assert limits.size == (220.0, 220.0, 300.0)


@pytest.mark.parametrize("fields, field", [
    ({"name": "No Area", "printable_height": "300"}, "printable_area"),
    ({"name": "Empty Area", "printable_area": [],
      "printable_height": "300"}, "printable_area"),
    ({"name": "Bad Area", "printable_area": ["0x0", "oops"],
      "printable_height": "300"}, "printable_area"),
    ({"name": "No Height",
      "printable_area": ["0x0", "220x0", "220x220", "0x220"]}, "printable_height"),
    ({"name": "Bad Height", "printable_area": ["0x0", "220x0", "220x220", "0x220"],
      "printable_height": "tall"}, "printable_height"),
])
def test_bed_limits_refuses_missing_and_unparsable_fields(tmp_path, fields, field):
    machine = tmp_path / "machine.json"
    machine.write_text(json.dumps(fields))

    with pytest.raises(prof.ProfileError) as excinfo:
        prof.bed_limits(machine)

    assert fields["name"] in str(excinfo.value)
    assert field in str(excinfo.value)


def test_new_options_default_the_right_way():
    options = prof.read_options({})
    assert options["auto_orient"] is True
    assert options["shrink_to_fit"] is False

    with pytest.raises(prof.ProfileError, match="auto_orient must be true or false"):
        prof.validate_options({"auto_orient": "yes"})
    with pytest.raises(prof.ProfileError, match="shrink_to_fit must be true or false"):
        prof.validate_options({"shrink_to_fit": 1})

    assert prof.validate_options({"auto_orient": False}) == {"auto_orient": False}


def test_new_options_do_not_touch_the_derived_preset(datadir):
    """Both are command-line switches, so no preset gains a field for them."""
    process = datadir / prof.DEFAULT_PROFILE["process"]
    prof.write_derived_process(process, datadir, dict(prof.DEFAULT_OPTIONS))
    before = json.loads((datadir / prof.PORTAL_PROCESS_REL).read_text())

    flipped = {**prof.DEFAULT_OPTIONS, "auto_orient": False, "shrink_to_fit": True}
    prof.write_derived_process(process, datadir, flipped)
    after = json.loads((datadir / prof.PORTAL_PROCESS_REL).read_text())

    assert "auto_orient" not in after
    assert "shrink_to_fit" not in after
    assert set(after) == set(before)
    assert prof.derived_state(process, datadir, flipped)["stale"] is False


def test_profiles_module_carries_no_personal_strings():
    source = (ROOT / "app" / "profiles.py").read_text()

    found = [needle for needle in _BANNED_FRAGMENTS if needle in source]
    assert found == []
