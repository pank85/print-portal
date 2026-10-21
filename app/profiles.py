#!/usr/bin/env python3
"""OrcaSlicer profile handling for print-portal.

Two jobs:
  1. summarise the profile that is currently active (for the UI)
  2. import a `.orca_printer` bundle (or a bare preset .json) exported from another
     OrcaSlicer install and make it slicable here

Why import needs real work: a bundle carries presets with
  * no `type` field at all (the folder it sits in is the only hint),
  * `inherits` pointing at vendor presets that do not exist in this headless datadir,
  * `compatible_printers` naming a machine preset that does not exist here either.
OrcaSlicer fails those with `return -17` / "printer is not compatible with the process
preset". Import therefore adds the type, re-roots `inherits` onto a base that does exist,
and rewrites `compatible_printers` to the imported machine's own name.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

FOLDER_TYPE = {"printer": "machine", "machine": "machine",
               "process": "process", "filament": "filament"}

# OrcaSlicer's CLI refuses ANY machine preset whose identity is not the one it expects:
# a bundle machine carrying its own name returns -17 "The selected printer is not compatible
# with the process preset", even when the process lists that name in compatible_printers.
# The only reliable way in is to apply the bundle's settings onto the stock machine preset
# (identity preserved) — verified by bisect: every other variant stayed at -17.
# The slot is resolved at runtime (`MACHINE_SLOT` env -> datadir default -> first
# instantiated machine), so any datadir with a machine preset works.
DEFAULT_PROFILE = {
    "machine":  "system/Custom/machine/generic-klipper-0.4.json",
    "process":  "system/Custom/process/generic-0.20-standard.json",
    "filament": "system/Custom/filament/generic-pla.json",
}

# The three persisted "basic options". Defaults mirror the shipped process preset: supports off
# (`enable_support: "0"`) and a brim on (the inherited `brim_width: "5"`).
OPTION_KEYS = ("supports", "brim", "hide_profiles")
DEFAULT_OPTIONS = {"supports": False, "brim": True, "hide_profiles": False}

# The derived process preset that carries the options into a slice. It lives inside the writable
# datadir next to imported presets, and is hidden from the preset picker (`list_presets`).
PORTAL_PROCESS_REL = "system/Custom/process/print-portal-options.json"
PORTAL_PROCESS_FILE = PurePosixPath(PORTAL_PROCESS_REL).name

MACHINE_IDENTITY = {"name", "inherits", "from", "type", "compatible_printers",
                    "printer_model", "printer_variant"}

BASE_FOR_TYPE = {"machine": "fdm_klipper_common",
                 "process": "fdm_process_klipper_common",
                 "filament": "fdm_filament_pla"}

# fields worth showing in the UI, per preset type
SUMMARISE = {
    "machine": ["printer_model", "nozzle_diameter", "printable_area", "printer_structure",
                "retraction_length", "retraction_speed", "max_layer_height",
                "default_print_profile"],
    "process": ["layer_height", "initial_layer_print_height", "wall_loops", "top_shell_layers",
                "sparse_infill_density", "sparse_infill_pattern", "print_speed",
                "outer_wall_speed", "support_enable", "enable_support", "brim_width",
                "compatible_printers"],
    "filament": ["filament_type", "nozzle_temperature", "nozzle_temperature_initial_layer",
                 "hot_plate_temp", "filament_density", "filament_cost", "filament_flow_ratio"],
}


class ProfileError(RuntimeError):
    pass


# The upload endpoint refuses bundles above this size; a single zip member may not be
# larger either, so an inflated member cannot be read into memory.
BUNDLE_MAX_BYTES = 20 * 1024 * 1024


def read_options(profile: object) -> dict[str, bool]:
    """The stored options with defaults applied; never raises.

    A value counts only when it is a real bool (JSON `true`/`false`); anything else — a legacy
    document without `options`, a corrupt value, `0`/`1`, `"yes"` — falls back per key.
    """
    stored = profile.get("options") if isinstance(profile, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    options: dict[str, bool] = {}
    for key in OPTION_KEYS:
        value = stored.get(key)
        options[key] = value if type(value) is bool else DEFAULT_OPTIONS[key]
    return options


def validate_options(update: object) -> dict[str, bool]:
    """The supplied (partial) option update, or a ProfileError explaining what is wrong."""
    if not isinstance(update, dict):
        raise ProfileError("options must be an object")
    unknown = [key for key in update if key not in OPTION_KEYS]
    if unknown:
        raise ProfileError(f"unknown option: {unknown[0]}")
    invalid = [key for key, value in update.items() if type(value) is not bool]
    if invalid:
        raise ProfileError(f"{invalid[0]} must be true or false")
    if not update:
        raise ProfileError("nothing to update")
    return {key: update[key] for key in OPTION_KEYS if key in update}


def write_json_atomic(path: Path, data: dict) -> None:
    """Write ``data`` as JSON through a sibling temp file + os.replace.

    Readers see either the old or the new file, never a half-written one; the temp file lives
    in the same directory so the replace stays on one filesystem.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def safe_stem(name: str) -> str:
    """OrcaSlicer CLI chokes on spaces in preset filenames."""
    stem = re.sub(r"[^A-Za-z0-9._@+-]+", "_", name.strip())
    return stem.strip("_")[:80] or "preset"


def machine_slot(datadir: Path, override: str | None = None) -> Path:
    """Resolve the machine preset that imports rebase onto.

    1. override: relative path inside datadir; must exist and be a machine preset.
    2. else datadir / DEFAULT_PROFILE["machine"] if it exists.
    3. else first `system/**/machine/*.json` with type=="machine" and instantiation=="true".
    4. else raise ProfileError("no machine preset found in <datadir> (set MACHINE_SLOT)").
    """
    datadir = Path(datadir)
    if override is not None:
        rel = Path(override)
        if rel.is_absolute() or ".." in rel.parts:
            raise ProfileError("machine slot override must be a relative path inside the datadir")
        candidate = datadir / rel
        try:
            resolved = candidate.resolve()
            root = datadir.resolve()
        except OSError as exc:
            raise ProfileError(str(exc)) from exc
        if root not in resolved.parents and resolved != root:
            raise ProfileError("machine slot override escapes the datadir")
        if not candidate.is_file():
            raise ProfileError(f"machine slot override does not exist: {override}")
        try:
            data = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"machine slot override is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or data.get("type") not in (None, "machine"):
            raise ProfileError("machine slot override is not a machine preset")
        return rel

    default_rel = Path(DEFAULT_PROFILE["machine"])
    if (datadir / default_rel).is_file():
        return default_rel

    for f in sorted(datadir.glob("system/**/machine/*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (isinstance(data, dict) and data.get("type") == "machine"
                and data.get("instantiation") == "true"):
            return f.relative_to(datadir)

    raise ProfileError(f"no machine preset found in {datadir} (set MACHINE_SLOT)")


def clamp_area(area: list[str], bed: tuple[float, float]) -> list[str] | None:
    """'0x0'.. style area -> clamped ["0x0", f"{W:g}x0", f"{W:g}x{H:g}", f"0x{H:g}"].
    Returns None when within tolerance (max_x <= W+1 and max_y <= H+1) or unparsable."""
    try:
        xs = [float(v.split("x")[0]) for v in area]
        ys = [float(v.split("x")[1]) for v in area]
    except (AttributeError, ValueError, IndexError):
        return None
    if not xs or not ys:
        return None
    max_x, max_y = max(xs), max(ys)
    w, h = bed
    if max_x <= w + 1 and max_y <= h + 1:
        return None
    return ["0x0", f"{w:g}x0", f"{w:g}x{h:g}", f"0x{h:g}"]


def _all_names(datadir: Path) -> dict[str, set[str]]:
    """Every preset name already known to this datadir, per type."""
    found: dict[str, set[str]] = {}
    for ptype, pattern in (("machine", "system/**/machine/*.json"),
                           ("process", "system/**/process/*.json"),
                           ("filament", "system/**/filament/**/*.json")):
        names: set[str] = set()
        for f in datadir.glob(pattern):
            try:
                data = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            name = data.get("name") or f.stem
            names.add(name)
            names.add(f.stem)
        found[ptype] = names
    return found


def _guess_type(data: dict, hint: str | None) -> str:
    if hint in ("machine", "process", "filament"):
        return hint
    if data.get("type") in ("machine", "process", "filament"):
        return data["type"]
    keys = set(data)
    if {"printable_area", "nozzle_diameter"} & keys or "printer_model" in keys:
        return "machine"
    if {"layer_height", "wall_loops", "sparse_infill_density"} & keys:
        return "process"
    if {"filament_type", "nozzle_temperature"} & keys:
        return "filament"
    return "process"


def normalise(data: dict, ptype: str, machine_name: str,
              known: dict[str, set[str]], bed: tuple[float, float] = (220.0, 220.0)
              ) -> tuple[dict, list[str]]:
    """Return (preset, warnings) ready to be loaded by the CLI."""
    notes: list[str] = []
    out = dict(data)
    out["type"] = ptype
    out.setdefault("from", "User")
    out.setdefault("name", safe_stem(str(data.get("name") or "imported")))

    parent = out.get("inherits")
    base = BASE_FOR_TYPE[ptype]
    if parent and parent not in known.get(ptype, set()):
        out["inherits"] = base
        notes.append(f"inherits '{parent}' not present here -> re-rooted to '{base}'")
    elif not parent:
        out["inherits"] = base
        notes.append(f"no inherits -> '{base}'")

    if ptype == "machine":
        out["compatible_printers"] = [out["name"]]
        area = out.get("printable_area")
        if area is None:
            area = data.get("printable_area")
        if isinstance(area, list):
            try:
                xs = [float(v.split("x")[0]) for v in area]
                ys = [float(v.split("x")[1]) for v in area]
                max_x, max_y = max(xs), max(ys)
                clamped = clamp_area(area, bed)
                if clamped is not None:
                    w, h = bed
                    notes.append(f"printable_area {max_x:g}x{max_y:g} exceeds the {w:g}x{h:g} bed — clamped")
                    out["printable_area"] = clamped
            except (ValueError, IndexError):
                notes.append("could not parse printable_area — left untouched")
    else:
        out["compatible_printers"] = [machine_name]

    return out, notes


def read_bundle(blob: bytes, filename: str) -> list[tuple[str, dict]]:
    """Return [(ptype, preset)] from a .orca_printer zip, a .3mf-ish zip or a single .json.

    Anything unreadable is a ProfileError (the API answers 400), never an unhandled
    JSONDecodeError/BadZipFile. JSONDecodeError and UnicodeDecodeError are ValueErrors.
    """
    try:
        return _read_bundle(blob, filename)
    except (ValueError, zipfile.BadZipFile) as exc:
        raise ProfileError(f"not a readable bundle: {exc}") from exc


def _read_bundle(blob: bytes, filename: str) -> list[tuple[str, dict]]:
    items: list[tuple[str, dict]] = []
    if filename.lower().endswith(".json"):
        data = json.loads(blob.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            raise ProfileError(f"not a readable bundle: {filename} is not a preset object")
        items.append((_guess_type(data, None), data))
        return items

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for info in zf.infolist():
            if not info.filename.lower().endswith(".json"):
                continue
            if info.file_size > BUNDLE_MAX_BYTES:
                continue
            parts = info.filename.strip("./").split("/")
            hint = FOLDER_TYPE.get(parts[0].lower()) if len(parts) > 1 else None
            try:
                data = json.loads(zf.read(info).decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict) or "bundle_structure" in str(data)[:40]:
                continue
            if "bundle_id" in data or "bundle_type" in data:
                continue
            if not data.get("name") and not data.get("layer_height"):
                continue
            items.append((_guess_type(data, hint), data))
    return items


def _parent_chain(datadir: Path, ptype: str, data: dict, depth: int = 3) -> list[dict]:
    """[self, parent, grandparent...] for parents that exist in this datadir."""
    chain = [data]
    current = data
    for _ in range(depth):
        parent = current.get("inherits")
        if not parent:
            break
        found = None
        pattern = {"machine": "system/**/machine/*.json",
                   "process": "system/**/process/*.json",
                   "filament": "system/**/filament/**/*.json"}[ptype]
        for f in datadir.glob(pattern):
            try:
                cand = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if (cand.get("name") or f.stem) == parent or f.stem == parent:
                found = cand
                break
        if not found:
            break
        chain.append(found)
        current = found
    return chain


def summarise(path: Path, ptype: str, datadir: Path | None = None) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"name": path.stem, "error": "unreadable"}
    chain = _parent_chain(datadir, ptype, data) if datadir else [data]
    out = {"name": data.get("name", path.stem), "file": path.name, "type": ptype}
    if datadir is not None:
        # the value the printer pairing stores, so the UI never rebuilds a path from the basename
        try:
            out["rel"] = Path(path).relative_to(Path(datadir)).as_posix()
        except ValueError:
            pass
    fields = SUMMARISE.get(ptype, [])
    for key in fields:
        for level in chain:                      # own value first, then inherited
            if key in level and level[key] not in (None, "", []):
                out[key] = level[key]
                break
    if "compatible_printers" in fields:
        # published for every row, so the machine filter never has to guess between
        # "universal" and "field missing", and a hand-edited preset that stores a bare
        # string (or junk) cannot reach a caller unnormalised
        out["compatible_printers"] = _name_list(out.get("compatible_printers"))
    if "default_print_profile" in fields:
        # one name or None, never the raw list: the 3MF matcher resolves this name against
        # the process presets, so a malformed value must not reach it as a list of junk
        out["default_print_profile"] = _first_name(out.get("default_print_profile"))
    return out


def _first_name(value: object) -> str | None:
    """One preset name out of a string or a name list; ``None`` for anything unusable."""
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, (list, tuple)):
        return None
    for name in names:
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _name_list(value: object) -> list[str]:
    """A preset's name list as plain strings; a malformed value reads as an empty list."""
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, (list, tuple)):
        return []
    return [name for name in names if isinstance(name, str) and name.strip()]


def _positive_width(value: object) -> str | None:
    """A usable brim width, as the string it was stored as (presets store numbers as strings)."""
    try:
        return str(value) if float(value) > 0 else None
    except (TypeError, ValueError):
        return None


def _effective_brim_width(datadir: Path, data: dict) -> str:
    """The brim width to generate when the brim is on: own value, else the first inherited one.

    The CLI loads `--load-settings` files without resolving `inherits`, so a preset that only
    inherits `brim_width` must have it copied in explicitly. 5 mm is the shipped preset intent.
    """
    chain = _parent_chain(datadir, "process", data)
    for level in chain:
        width = _positive_width(level.get("brim_width"))
        if width is not None:
            return width
    return "5"


def _portal_option_fields(datadir: Path, data: dict, options: dict[str, bool]) -> dict[str, str]:
    """The three preset fields the portal options control, as the strings presets store.

    Shared by `write_derived_process` (which writes them) and `effective_process` (which
    reports them), so what the profile card shows is what the slicer loads.
    """
    fields = {"enable_support": "1" if options.get("supports") else "0"}
    if options.get("brim"):
        fields["brim_type"] = "outer_only"
        fields["brim_width"] = _effective_brim_width(Path(datadir), data)
    else:
        fields["brim_type"] = "no_brim"
        fields["brim_width"] = "0"
    return fields


def _derived_name(process: Path, data: dict) -> str:
    """The derived preset's name; it must stay distinct from the preset it was copied from."""
    return f"{data.get('name') or Path(process).stem} (portal options)"


def write_derived_process(process: Path, datadir: Path, options: dict[str, bool]) -> Path:
    """Write the per-slice process preset that carries the options; return its path.

    The selected preset is only ever read: the CLI refuses a second process file on
    `--load-settings`, so this copy replaces it, keeping every key the preset contributes.
    """
    try:
        data = json.loads(Path(process).read_text())
    except (OSError, ValueError) as exc:
        raise ProfileError("process preset is not readable") from exc
    if not isinstance(data, dict):
        raise ProfileError("process preset is not readable")

    derived = dict(data)
    derived["type"] = "process"
    derived["from"] = "system"
    derived["name"] = _derived_name(process, data)
    derived.update(_portal_option_fields(Path(datadir), data, options))

    derived_path = Path(datadir) / PORTAL_PROCESS_REL
    write_json_atomic(derived_path, derived)
    return derived_path


def _effective_machine(datadir: Path, data: dict) -> str | None:
    """The machine the process targets: its `compatible_printers`, else the datadir's slot."""
    printers = data.get("compatible_printers")
    if isinstance(printers, str):
        printers = [printers]
    if isinstance(printers, list):
        first = next((name for name in printers
                      if isinstance(name, str) and name), None)
        if first:
            return first
    try:
        slot = machine_slot(Path(datadir))
        slot_data = json.loads((Path(datadir) / slot).read_text())
    except (ProfileError, OSError, ValueError):
        return None
    if not isinstance(slot_data, dict):
        return None
    return slot_data.get("name") or Path(slot).stem


def effective_process(process: Path, datadir: Path, options: dict[str, bool]) -> dict:
    """What the options plus the selected process preset mean for the next slice.

    Never raises: an unreadable preset reports the same text `write_derived_process` would
    have raised, so the endpoint can show it instead of failing the profile card.
    """
    try:
        data = json.loads(Path(process).read_text())
    except (OSError, ValueError):
        return {"error": "process preset is not readable"}
    if not isinstance(data, dict):
        return {"error": "process preset is not readable"}

    fields = _portal_option_fields(Path(datadir), data, options)
    brim = bool(options.get("brim"))
    return {
        "supports": bool(options.get("supports")),
        "brim": brim,
        "brim_width": fields["brim_width"] if brim else None,
        "machine": _effective_machine(Path(datadir), data),
        "process": _derived_name(process, data),
        "process_rel": PORTAL_PROCESS_REL,
    }


def derived_state(process: Path, datadir: Path, options: dict[str, bool]) -> dict:
    """Whether the derived preset on disk still matches the options; never raises.

    `stale` covers the three ways the slice would load something other than the options: the
    file is missing, unreadable, or carries different support/brim fields.
    """
    try:
        data = json.loads(Path(process).read_text())
    except (OSError, ValueError):
        return {"path": PORTAL_PROCESS_REL, "stale": True,
                "error": "process preset is not readable"}
    if not isinstance(data, dict):
        return {"path": PORTAL_PROCESS_REL, "stale": True,
                "error": "process preset is not readable"}

    expected = _portal_option_fields(Path(datadir), data, options)
    try:
        on_disk = json.loads((Path(datadir) / PORTAL_PROCESS_REL).read_text())
    except (OSError, ValueError):
        return {"path": PORTAL_PROCESS_REL, "stale": True,
                "error": "derived process preset is not readable"}
    if not isinstance(on_disk, dict):
        return {"path": PORTAL_PROCESS_REL, "stale": True,
                "error": "derived process preset is not readable"}
    stale = any(on_disk.get(key) != value for key, value in expected.items())
    return {"path": PORTAL_PROCESS_REL, "stale": stale, "error": None}


def list_presets(datadir: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    pattern_for = {"machine": "system/Custom/machine/*.json",
                   "process": "system/Custom/process/*.json",
                   "filament": "system/Custom/filament/*.json"}
    for ptype, pattern in pattern_for.items():
        rows = []
        for f in sorted(datadir.glob(pattern)):
            if f.name.startswith("fdm_") or f.name == PORTAL_PROCESS_FILE:
                continue
            try:
                data = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") != ptype:
                continue
            if data.get("instantiation") not in (None, "true"):
                continue
            rows.append(summarise(f, ptype, datadir))
        out[ptype] = rows
    return out


# ------------------------------------------------------- machine/process compatibility
def _preset_name(text: object) -> tuple[str, str]:
    """``(full, base)`` for lenient preset-name comparison; ``("", "")`` on a non-string.

    Preset names are compared the way they read: casefolded, ``_`` as a space, whitespace
    collapsed, a trailing ``*`` (an edited preset) dropped, and the ``@printer`` suffix
    split off so that the same process for two printer models still matches by base name.
    """
    if not isinstance(text, str):
        return "", ""
    name = " ".join(text.casefold().replace("_", " ").split())
    while name.endswith("*"):
        name = name[:-1].strip()
    head, sep, _suffix = name.rpartition("@")
    base = head.strip() if sep else ""
    return name, base or name


def _same_preset_name(name: object, wanted: tuple[str, str]) -> bool:
    full, base = _preset_name(name)
    if not full:
        return False
    return full == wanted[0] or bool(base) and base == wanted[1]


def filter_process_by_machine(rows: list[dict], machine: dict | None) -> list[dict]:
    """The process presets that may be sliced with ``machine``.

    A preset is compatible when its ``compatible_printers`` is empty — a universal preset
    — or names this machine. ``machine=None`` (nothing selected, or no local preset the
    file names) returns the rows untouched, and a malformed row or ``compatible_printers``
    counts as universal, so a broken preset file never takes the picker down.
    """
    if not isinstance(machine, dict):
        return rows
    wanted = _preset_name(machine.get("name"))
    if not wanted[0]:
        return rows
    kept = []
    for row in rows:
        allowed = _name_list(row.get("compatible_printers")) if isinstance(row, dict) else []
        if not allowed or any(_same_preset_name(name, wanted) for name in allowed):
            kept.append(row)
    return kept


def import_into(datadir: Path, blob: bytes, filename: str, slot: Path,
                bed: tuple[float, float]) -> dict:
    """Normalise every preset in the bundle and drop it into the datadir.

    Machine presets are merged onto the stock slot (see machine_slot); process and filament
    presets are written as ordinary files and pointed at the slot's name.
    """
    items = read_bundle(blob, filename)
    if not items:
        return {"imported": [], "notes": ["no presets found in that file"], "ok": False}

    known = _all_names(datadir)
    datadir = Path(datadir)
    slot_path = Path(slot)
    if not slot_path.is_absolute():
        slot_path = datadir / slot_path
    slot = json.loads(slot_path.read_text())
    slot_name = slot.get("name", slot_path.stem)
    backup = slot_path.with_suffix(".json.orig")
    if not backup.exists():
        backup.write_text(json.dumps(slot, indent="\t"))

    imported: list[dict] = []
    notes: list[str] = []
    machine_seen = False

    for ptype, data in items:
        if ptype == "machine":
            merged = dict(slot)
            for key, val in data.items():
                if key in MACHINE_IDENTITY:
                    continue
                merged[key] = val
            area = merged.get("printable_area")
            if isinstance(area, list):
                clamped = clamp_area(area, bed)
                if clamped is not None:
                    try:
                        xs = [float(v.split("x")[0]) for v in area]
                        ys = [float(v.split("x")[1]) for v in area]
                        max_x, max_y = max(xs), max(ys)
                        w, h = bed
                        notes.append(f"printable_area {max_x:g}x{max_y:g} exceeds the {w:g}x{h:g} bed — clamped")
                    except (ValueError, IndexError):
                        pass
                    merged["printable_area"] = clamped
            slot_path.write_text(json.dumps(merged, indent="\t"))
            machine_seen = True
            imported.append({"type": "machine", "name": data.get("name") or slot_name,
                             "label": data.get("name") or slot_name,
                             "file": slot_path.name, "path": str(slot_path.relative_to(datadir)),
                             "rebased_on": slot_name})
            notes.append(f"machine '{data.get('name')}' applied onto the stock preset "
                         f"'{slot_name}' (Orca rejects new machine identities with -17)")
            continue

        preset, item_notes = normalise(data, ptype, slot_name, known, bed)
        dest_dir = datadir / "system/Custom" / ptype
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{safe_stem(preset['name'])}.json"
        dest.write_text(json.dumps(preset, indent="\t"))
        imported.append({"type": ptype, "name": preset["name"], "label": preset["name"],
                         "file": dest.name, "path": str(dest.relative_to(datadir))})
        notes.extend(f"{preset['name']}: {n}" for n in item_notes)

    if machine_seen:
        # keep the slot's own defaults pointing at something that exists
        slot = json.loads(slot_path.read_text())
        procs = [i for i in imported if i["type"] == "process"]
        fils = [i for i in imported if i["type"] == "filament"]
        if procs:
            slot["default_print_profile"] = procs[0]["name"]
        if fils:
            slot["default_filament_profile"] = [fils[0]["name"]]
        slot_path.write_text(json.dumps(slot, indent="\t"))

    return {"imported": imported, "notes": notes, "ok": bool(imported)}