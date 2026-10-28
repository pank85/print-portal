#!/usr/bin/env python3
"""OrcaSlicer profile handling for print-portal.

Two jobs:
  1. summarise the profile that is currently active (for the UI)
  2. import a `.orca_printer` bundle (or a bare preset .json) exported from another
     OrcaSlicer install and make it slicable here

A fresh install ships no preset at all (see `machine_slot`), so import is the only way in.
Why import needs real work: a bundle carries presets with
  * no `type` field at all (the folder it sits in is the only hint),
  * `compatible_printers` naming a machine preset that does not exist here.
The one field the CLI is strict about is `from`, measured with OrcaSlicer 2.4.2: a machine
preset must carry `"from": "system"` (`"user"` -> return_code -17 "The selected printer is not
compatible with the process preset in the 3mf", `"System"`/`"SYSTEM"`/absent -> return_code 251
"The input preset file is invalid and can not be parsed"), while process and filament presets
take `"User"`/`"user"` and reject `"System"`. A dangling `inherits` is tolerated (rc=0) and so is
an empty `system/Custom.json`, so import sets the type and `from`, rewrites
`compatible_printers` to the imported machine's own name, and writes everything else as it came.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

FOLDER_TYPE = {"printer": "machine", "machine": "machine",
               "process": "process", "filament": "filament"}

# The legacy stock slot names. `machine` is the hard fallback: `machine_slot` resolves one on
# any datadir (MACHINE_SLOT -> datadir default -> first instantiated machine). `process` and
# `filament` are only stand-ins while the datadir still ships those files — the seed ships
# none, so a fresh install selects nothing there and `partial_selection` drops the key — and
# an import writes every preset under its own name anyway.
#
# The one strict field is `from`. Measured with OrcaSlicer 2.4.2: a machine preset carrying
# "user" answers -17 "The selected printer is not compatible with the process preset in the
# 3mf", and "System"/"SYSTEM"/a missing `from` answers 251 "The input preset file is invalid
# and can not be parsed"; only "system" slices. Process and filament are the mirror image
# ("User"/"user" fine, "System" invalid). `normalise` enforces both halves.
DEFAULT_PROFILE = {
    "machine":  "system/Custom/machine/generic-klipper-0.4.json",
    "process":  "system/Custom/process/generic-0.20-standard.json",
    "filament": "system/Custom/filament/generic-pla.json",
}

# The persisted "basic options". These are the portal's own state, not a copy of any preset:
# supports off, and a brim on because the CLI's own `brim_width` defaults to "5" when nothing
# overrides it. `regenerate_derived` writes `supports`/`brim` into the derived process preset;
# `hide_profiles` is a UI switch and `auto_orient`/`shrink_to_fit` are command-line switches,
# so those three never reach a preset.
OPTION_KEYS = ("supports", "brim", "hide_profiles", "auto_orient", "shrink_to_fit")
DEFAULT_OPTIONS = {"supports": False, "brim": True, "hide_profiles": False,
                   "auto_orient": True, "shrink_to_fit": False}

# The derived process preset that carries the options into a slice. It lives inside the writable
# datadir next to imported presets, and is hidden from the preset picker (`list_presets`).
PORTAL_PROCESS_REL = "system/Custom/process/print-portal-options.json"
PORTAL_PROCESS_FILE = PurePosixPath(PORTAL_PROCESS_REL).name

# What every preset-dependent *action* says when nothing has been imported yet. It lives here,
# next to the preset paths, so the API layer answers with this one sentence instead of inventing
# its own (a read answers 200 with an empty selection instead).
NO_MACHINE_MESSAGE = "no printer profile imported yet"

# The vendor registry import read-modify-writes. The CLI never opens it (the app passes explicit
# --load-settings/--load-filaments), it is only where the portal records the presets it wrote.
VENDOR_INDEX_REL = "system/Custom.json"
VENDOR_LISTS = {"machine": "machine_list",
                "process": "process_list",
                "filament": "filament_list"}

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


def read_vendor_index(datadir: Path) -> dict:
    """`system/Custom.json` as a dict; a missing or unreadable index reads as an empty one.

    The CLI never opens this file — import passes explicit --load-settings/--load-filaments
    paths — so it is only the registry the portal writes its presets into. A corrupt index
    must therefore degrade to a fresh document instead of failing an import.
    """
    path = Path(datadir) / VENDOR_INDEX_REL
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key in ("machine_model_list", "process_list", "filament_list", "machine_list"):
        if not isinstance(data.get(key), list):
            data[key] = []
    return data


def register_preset(index: dict, ptype: str, name: str, sub_path: str) -> bool:
    """Put ``{"name": name, "sub_path": sub_path}`` in ``<ptype>_list``; True if it changed.

    Keyed on `name`, so re-importing a bundle updates its entry in place instead of appending
    a duplicate, and every other key of the document (machine_model_list, the other types,
    version, description) is left untouched.
    """
    key = VENDOR_LISTS[ptype]
    rows = index.setdefault(key, [])
    entry = {"name": name, "sub_path": sub_path}
    for row in rows:
        if isinstance(row, dict) and row.get("name") == name:
            if row == entry:
                return False
            row.update(entry)
            return True
    rows.append(entry)
    return True


def write_vendor_index(datadir: Path, index: dict) -> Path:
    """Persist ``system/Custom.json`` atomically (readers see the old or the new document)."""
    path = Path(datadir) / VENDOR_INDEX_REL
    write_json_atomic(path, index)
    return path


def _preset_rel(ptype: str, name: str) -> Path:
    """Where a preset of ``ptype`` named ``name`` lives, relative to the datadir."""
    return Path("system/Custom") / ptype / f"{safe_stem(name)}.json"


def write_preset(datadir: Path, ptype: str, preset: dict) -> Path:
    """Write one normalised preset to ``system/Custom/<ptype>/<safe_stem(name)>.json``.

    The single writer for all three types (tab-indented, no trailing newline), so the JSON
    convention lives in one place. A machine preset reaches it only with ``from: "system"``
    (see `normalise`): the CLI answers -17 for ``"user"`` and 251 for anything else.
    """
    dest = Path(datadir) / _preset_rel(ptype, str(preset["name"]))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(preset, indent="\t"))
    return dest


def _same_preset(existing: dict, incoming: dict) -> bool:
    """True when ``incoming`` is the preset already on disk, not a different one reusing its name.

    ``setting_id`` decides when both documents carry one; otherwise the normalised documents
    must be identical — which is exactly the re-import case, because `normalise` is
    deterministic. A different bundle reusing a name fails this and is refused.
    """
    old_id, new_id = existing.get("setting_id"), incoming.get("setting_id")
    if isinstance(old_id, str) and old_id and isinstance(new_id, str) and new_id:
        return old_id == new_id
    return existing == incoming


def machine_slot(datadir: Path, override: str | None = None) -> Path | None:
    """Resolve the machine preset the slicer should load, or None when the datadir has none.

    1. override: relative path inside datadir; must exist and be a machine preset.
    2. else datadir / DEFAULT_PROFILE["machine"] if it exists.
    3. else first `system/**/machine/*.json` with type=="machine" and instantiation=="true".
    4. else None — a fresh install ships no presets; importing a bundle is the only way in.

    A bad *override* is still a ProfileError: that is operator configuration, not an empty
    datadir, and silently ignoring it would slice with a machine the operator did not choose.
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

    return None


@dataclass(frozen=True, slots=True)
class BedLimits:
    """The printable box of one machine preset, in millimetres."""

    xmin: float
    ymin: float
    xmax: float
    ymax: float
    zmax: float
    machine: str

    @property
    def size(self) -> tuple[float, float, float]:
        """(xmax-xmin, ymax-ymin, zmax) in millimetres."""
        return (self.xmax - self.xmin, self.ymax - self.ymin, self.zmax)


def bed_limits(machine: Path) -> BedLimits:
    """Read the printable limits out of ONE machine preset file.

    `inherits` is deliberately not resolved: the CLI loads only the file it is given, so an
    inherited `printable_area` would not reach the slice and is reported as missing instead.
    The min corner is kept — a bed authored `10x10`..`230x230` is a 220 mm span, not 230.
    """
    path = Path(machine)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ProfileError(f"machine preset '{path.stem}' is not readable") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"machine preset '{path.stem}' is not readable")

    name = data.get("name")
    name = name if isinstance(name, str) and name else path.stem

    area = data.get("printable_area")
    if area is None:
        raise ProfileError(f"machine preset '{name}' has no printable_area")
    xs: list[float] = []
    ys: list[float] = []
    try:
        if not isinstance(area, list) or len(area) < 3:
            raise ValueError
        for corner in area:
            x, y = str(corner).lower().split("x")
            xs.append(float(x))
            ys.append(float(y))
    except (AttributeError, TypeError, ValueError):
        raise ProfileError(
            f"machine preset '{name}' has an unparsable printable_area") from None

    height = data.get("printable_height")
    if height is None:
        raise ProfileError(f"machine preset '{name}' has no printable_height")
    try:
        zmax = float(str(height))
    except (TypeError, ValueError):
        raise ProfileError(
            f"machine preset '{name}' has an unparsable printable_height") from None

    return BedLimits(min(xs), min(ys), max(xs), max(ys), zmax, name)


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


def normalise(data: dict, ptype: str, machine_name: str | None,
              bed: tuple[float, float] = (220.0, 220.0)) -> tuple[dict, list[str]]:
    """Return (preset, warnings) ready to be loaded by the CLI.

    `from` is forced per type (machine: "system", everything else: "User"/"user"), `inherits`
    is kept verbatim — a dangling inherits is tolerated by the CLI — and
    `compatible_printers` is rewritten to `machine_name` only when the bundle brought a machine.
    """
    notes: list[str] = []
    out = dict(data)
    out["type"] = ptype
    if ptype == "machine":
        # measured with OrcaSlicer 2.4.2: anything but these two literals fails the slice
        # (-17 for "user", 251 for "System"/"SYSTEM"/absent), so neither is the bundle's
        out["from"] = "system"
        out["instantiation"] = "true"
    else:
        out["from"] = data["from"] if data.get("from") in ("User", "user") else "User"
    out.setdefault("name", safe_stem(str(data.get("name") or "imported")))

    parent = out.get("inherits")
    if parent:
        # a dangling inherits slices fine (measured), and inventing a base would make the
        # imported preset depend on a file this datadir does not ship — so it is copied as-is
        notes.append(f"inherits '{parent}' kept as-is")

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
    elif isinstance(machine_name, str) and machine_name:
        out["compatible_printers"] = [machine_name]
    else:
        # no machine to point at: the bundle's own list is the only truth we have
        notes.append("no machine to target — compatible_printers kept as-is")

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


def derived_process_data(process: Path, datadir: Path, options: dict[str, bool]) -> dict:
    """Build the effective process without writing shared preset state."""
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

    return derived


def write_derived_process(process: Path, datadir: Path, options: dict[str, bool]) -> Path:
    derived = derived_process_data(process, datadir, options)
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
        if slot is None:
            return None
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
        "auto_orient": bool(options.get("auto_orient", DEFAULT_OPTIONS["auto_orient"])),
        "shrink_to_fit": bool(options.get("shrink_to_fit", DEFAULT_OPTIONS["shrink_to_fit"])),
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


def _plan_import(datadir: Path, items: list[tuple[str, dict]],
                 bed: tuple[float, float]) -> tuple[list[tuple[str, dict, Path]], list[str]]:
    """Normalise the whole bundle and resolve every destination before anything is written.

    Machines are planned first so the bundle's own processes/filaments can name the machine
    that is about to exist rather than whatever was selected before.

    :raises ProfileError: a destination exists and holds a *different* preset with that name,
        or this one bundle maps two different presets onto one destination (nothing has been
        written when it raises). The message names the file, so the 400 tells the operator
        what to delete.
    """
    datadir = Path(datadir)
    # machines first: every process/filament the bundle brings must point at the machine it
    # brought, not at whatever this datadir selected before the import
    ordered = ([item for item in items if item[0] == "machine"]
               + [item for item in items if item[0] != "machine"])
    notes: list[str] = []
    planned: list[tuple[str, dict, Path]] = []
    seen: dict[Path, dict] = {}
    machine = None
    for ptype, data in ordered:
        preset, item_notes = normalise(data, ptype, machine, bed)
        if ptype == "machine" and machine is None:
            machine = str(preset["name"])
        notes.extend(f"{preset['name']}: {note}" for note in item_notes)
        dest = datadir / _preset_rel(ptype, str(preset["name"]))
        earlier = seen.get(dest)
        if earlier is not None:
            # one bundle naming one destination twice: the second write would silently replace
            # the first. The identical document collapses onto that one file; a different one
            # is refused like a collision with a file already on disk, before any write.
            if earlier != preset:
                name = preset["name"]
                raise ProfileError(
                    f"{dest} is written twice by this bundle with different presets named "
                    f"{name!r}; rename one of them and re-import")
            continue
        seen[dest] = preset
        planned.append((ptype, preset, dest))

    # the machine carries the bundle's own defaults, so the CLI's default resolution points at
    # a preset that exists here (what the deleted "slot defaults" block used to do). Set before
    # the collision test, because a re-import has to produce the document that is on disk.
    machine_preset = next((preset for ptype, preset, _ in planned if ptype == "machine"), None)
    if machine_preset is not None:
        process = next((preset for ptype, preset, _ in planned if ptype == "process"), None)
        filament = next((preset for ptype, preset, _ in planned if ptype == "filament"), None)
        if process is not None:
            machine_preset["default_print_profile"] = process["name"]
        if filament is not None:
            machine_preset["default_filament_profile"] = [filament["name"]]

    for _ptype, preset, dest in planned:
        if not dest.is_file():
            continue
        try:
            existing = json.loads(dest.read_text())
        except (OSError, ValueError):
            existing = None
        if not isinstance(existing, dict) or not _same_preset(existing, preset):
            name = preset["name"]
            raise ProfileError(
                f"{dest} already holds a different preset named {name!r}; "
                f"delete that file and re-import if you want to replace it")
    return planned, notes


def import_into(datadir: Path, blob: bytes, filename: str,
                bed: tuple[float, float] = (220.0, 220.0)) -> dict:
    """Write every preset in the bundle into the datadir under its own name.

    Two phases, so a refused import writes nothing: the bundle is read and normalised in
    memory first (`_plan_import`, which raises on a name collision), then the presets are
    written machine-first and `system/Custom.json` is updated last — a crash then leaves an
    unreferenced file rather than an index entry pointing at nothing.

    Returns {"imported": [{"type", "name", "label", "file", "path", "registered"}],
             "notes": [str], "ok": bool}.
    """
    datadir = Path(datadir)
    items = read_bundle(blob, filename)
    if not items:
        return {"imported": [], "notes": ["no presets found in that file"], "ok": False}

    planned, notes = _plan_import(datadir, items, bed)

    index = read_vendor_index(datadir)
    index_changed = False
    imported: list[dict] = []
    for ptype, preset, _dest in planned:
        dest = write_preset(datadir, ptype, preset)
        index_changed = register_preset(index, ptype, str(preset["name"]),
                                        f"{ptype}/{dest.name}") or index_changed
        imported.append({"type": ptype, "name": preset["name"], "label": preset["name"],
                         "file": dest.name, "path": str(dest.relative_to(datadir)),
                         "registered": True})
    if index_changed:
        write_vendor_index(datadir, index)

    return {"imported": imported, "notes": notes, "ok": bool(imported)}


class ProfileError(Exception):
    def __init__(self, *args, status: int | None = None, detail: str | None = None):
        if len(args) == 1:
            detail = args[0] if detail is None else detail
            status = 400 if status is None else status
        elif len(args) == 2:
            status = args[0] if status is None else status
            detail = args[1] if detail is None else detail
        else:
            status = 400 if status is None else status
            detail = "" if detail is None else detail
        self.status = status
        self.detail = detail or ""
        super().__init__(self.detail)


# Field editors for the preset UI. Values in OrcaSlicer presets are JSON *strings* (even numbers),
# and some keys are lists; shapes below are preserved on write. "portal-options" owns supports/brim,
# so they are deliberately absent here — two writers would fight.
MACHINE_FIELDS = [
    {"key": "printer_model", "label": "Printer model", "type": "text"},
    {"key": "nozzle_diameter", "label": "Nozzle diameter", "type": "number_list", "unit": "mm"},
    {"key": "printable_area", "label": "Printable area", "type": "area_list", "unit": "mm"},
    {"key": "printable_height", "label": "Printable height", "type": "number", "unit": "mm"},
    {"key": "max_layer_height", "label": "Max layer height", "type": "number_list", "unit": "mm"},
    {"key": "retraction_length", "label": "Retraction length", "type": "number_list", "unit": "mm"},
    {"key": "retraction_speed", "label": "Retraction speed", "type": "number_list", "unit": "mm/s"},
]
PROCESS_FIELDS = [
    {"key": "layer_height", "label": "Layer height", "type": "number", "unit": "mm"},
    {"key": "initial_layer_print_height", "label": "Initial layer height", "type": "number", "unit": "mm"},
    {"key": "wall_loops", "label": "Wall loops", "type": "integer"},
    {"key": "top_shell_layers", "label": "Top shell layers", "type": "integer"},
    {"key": "sparse_infill_density", "label": "Sparse infill density", "type": "percent"},
    {"key": "sparse_infill_pattern", "label": "Sparse infill pattern", "type": "enum",
     "choices": ["grid", "line", "cubic", "triangles", "gyroid", "honeycomb", "concentric",
                 "zig-zag", "cross-hatch", "lightning", "3d honeycomb", "adaptive cubic",
                 "support cubic"]},
    {"key": "print_speed", "label": "Print speed", "type": "number", "unit": "mm/s"},
    {"key": "outer_wall_speed", "label": "Outer wall speed", "type": "number", "unit": "mm/s"},
    {"key": "travel_speed", "label": "Travel speed", "type": "number", "unit": "mm/s"},
]
FILAMENT_FIELDS = [
    {"key": "filament_type", "label": "Filament type", "type": "text"},
    {"key": "nozzle_temperature", "label": "Nozzle temperature", "type": "number", "unit": "°C"},
    {"key": "nozzle_temperature_initial_layer", "label": "Nozzle temp (initial layer)",
     "type": "number", "unit": "°C"},
    {"key": "hot_plate_temp", "label": "Bed temperature", "type": "number", "unit": "°C"},
    {"key": "hot_plate_temp_initial_layer", "label": "Bed temp (initial layer)",
     "type": "number", "unit": "°C"},
    {"key": "filament_flow_ratio", "label": "Flow ratio", "type": "number"},
    {"key": "filament_density", "label": "Density", "type": "number", "unit": "g/cm³"},
    {"key": "filament_cost", "label": "Cost", "type": "number", "unit": "per kg"},
    {"key": "enable_pressure_advance", "label": "Enable pressure advance", "type": "boolean"},
    {"key": "pressure_advance", "label": "Pressure advance", "type": "number"},
]
FIELD_SPECS = {"machine": MACHINE_FIELDS, "process": PROCESS_FIELDS, "filament": FILAMENT_FIELDS}
EXPOSED_KEYS = {k: {f["key"] for f in v} for k, v in FIELD_SPECS.items()}

LIST_KEYS = {"nozzle_diameter", "printable_area", "max_layer_height",
             "retraction_length", "retraction_speed"}


def _is_custom(datadir: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(datadir).as_posix()
    except ValueError:
        return False
    return rel.startswith("system/Custom/")


def preset_read_only_reason(datadir: Path, path: Path) -> str | None:
    if not _is_custom(datadir, path):
        return "this preset comes from the imported bundle and is read-only"
    rel = path.relative_to(datadir).as_posix()
    if rel == PORTAL_PROCESS_REL:
        return ("this is the derived preset the portal regenerates on every option save "
                "and every slice")
    return None


def _effective_value(chain: list[dict], key: str):
    for level in chain:
        if key in level and level[key] not in (None, "", []):
            return level[key], level
    return None, None


def _to_form_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        if any(isinstance(x, str) and "," in x for x in value):
            return "\n".join(str(x) for x in value)
        return ", ".join(str(x) for x in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def preset_view(datadir: Path, kind: str, path: Path) -> dict:
    data = json.loads(path.read_text())
    chain = _parent_chain(datadir, kind, data)
    rel = path.relative_to(datadir).as_posix()
    reason = preset_read_only_reason(datadir, path)
    editable = reason is None
    fields_out = []
    for spec in FIELD_SPECS[kind]:
        key = spec["key"]
        eff, src_level = _effective_value(chain, key)
        if eff is None:
            source = "inherited"
        elif src_level is chain[0]:
            source = "own"
        else:
            source = "inherited"
        inherited_from = None
        if source == "inherited" and src_level is not None and src_level is not chain[0]:
            inherited_from = src_level.get("name")
        is_list = isinstance(eff, list)
        if is_list and any(isinstance(x, str) and "," in x for x in eff):
            ftype = "area_list"
        elif is_list:
            ftype = "number_list"
        else:
            ftype = spec["type"]
        entry = {
            "key": key,
            "label": spec["label"],
            "type": ftype,
            "value": _to_form_value(eff),
            "source": source,
            "inherited_from": inherited_from,
        }
        if "choices" in spec:
            entry["choices"] = spec["choices"]
        if "unit" in spec:
            entry["unit"] = spec["unit"]
        fields_out.append(entry)
    return {
        "kind": kind,
        "rel": rel,
        "name": data.get("name", path.stem),
        "editable": editable,
        "read_only_reason": reason,
        "fields": fields_out,
    }


def _parse_field(spec, raw: str):
    key = spec["key"]
    ftype = spec["type"]
    if key in LIST_KEYS:
        if key == "printable_area":
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if len(lines) < 4:
                raise ValueError(f"{key}: need at least 4 'x,y' pairs")
            out = []
            for ln in lines:
                parts = [p.strip() for p in ln.split(",")]
                if len(parts) != 2:
                    raise ValueError(f"{key}: each line must be 'x,y'")
                try:
                    x, y = float(parts[0]), float(parts[1])
                except ValueError:
                    raise ValueError(f"{key}: '{ln}' is not two numbers")
                if not (x > 0 and y > 0):
                    raise ValueError(f"{key}: coordinates must be positive")
                out.append(f"{parts[0]},{parts[1]}")
            return out
        if raw.strip() == "":
            raise ValueError(f"{key}: value required")
        parts = [p.strip() for p in raw.split(",")]
        if any(p == "" for p in parts):
            raise ValueError(f"{key}: empty entry in list")
        out = []
        for p in parts:
            try:
                v = float(p)
            except ValueError:
                raise ValueError(f"{key}: '{p}' is not a number")
            if v != v or v == float("inf"):
                raise ValueError(f"{key}: '{p}' is not a finite number")
            if v < 0:
                raise ValueError(f"{key}: must be >= 0")
            out.append(p)
        return out
    if ftype == "number":
        try:
            v = float(raw)
        except ValueError:
            raise ValueError(f"{key}: '{raw}' is not a number")
        if v != v or v == float("inf"):
            raise ValueError(f"{key}: must be finite")
        if v < 0:
            raise ValueError(f"{key}: must be >= 0")
        return raw.strip()
    if ftype == "integer":
        try:
            v = int(raw)
        except ValueError:
            raise ValueError(f"{key}: '{raw}' is not an integer")
        if v < 0:
            raise ValueError(f"{key}: must be >= 0")
        return str(v)
    if ftype == "percent":
        s = raw.strip()
        if s.endswith("%"):
            s = s[:-1].strip()
        try:
            v = float(s)
        except ValueError:
            raise ValueError(f"{key}: '{raw}' is not a percentage")
        if v < 0 or v > 100:
            raise ValueError(f"{key}: must be between 0 and 100")
        return f"{v:g}%"
    if ftype == "boolean":
        s = raw.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return "true"
        if s in ("false", "0", "no", "off"):
            return "false"
        raise ValueError(f"{key}: must be true or false")
    if ftype == "enum":
        spec_choices = None
        for f in FIELD_SPECS.get(None, []) if False else []:
            pass
        # choices come from the spec; caller passes spec via closure-free lookup
        return raw
    if raw.strip() == "":
        raise ValueError(f"{key}: value required")
    return raw.strip()


def apply_preset_patch(datadir: Path, kind: str, path: Path, fields: dict) -> dict:
    reason = preset_read_only_reason(datadir, path)
    if reason is not None:
        raise ProfileError(409, reason)
    if not isinstance(fields, dict) or not fields:
        raise ProfileError(400, "nothing to update")
    exposed = EXPOSED_KEYS[kind]
    unknown = [k for k in fields if k not in exposed]
    if unknown:
        raise ProfileError(400, f"unknown field(s): {', '.join(sorted(unknown))}")
    specs = {f["key"]: f for f in FIELD_SPECS[kind]}
    parsed = {}
    for k, v in fields.items():
        if not isinstance(v, str):
            raise ProfileError(400, f"{k}: value must be a string")
        # enum choices validation uses spec
        if specs[k]["type"] == "enum" and v not in specs[k]["choices"]:
            raise ProfileError(400, f"{k}: '{v}' is not one of {specs[k]['choices']}")
        try:
            parsed[k] = _parse_field(specs[k], v)
        except ValueError as e:
            raise ProfileError(400, str(e))
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ProfileError(400, "preset file is unreadable")
    if not isinstance(data, dict):
        raise ProfileError(400, "preset file is not a JSON object")
    for k, v in parsed.items():
        data[k] = v
    write_json_atomic(path, data)
    return preset_view(datadir, kind, path)


def delete_preset(datadir: Path, kind: str, path: Path):
    if kind not in ("machine", "process"):
        raise ProfileError(405, f"presets of kind '{kind}' cannot be deleted")
    reason = preset_read_only_reason(datadir, path)
    if reason is not None:
        raise ProfileError(409, reason)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ProfileError(400, "preset file is unreadable")
    name = data.get("name", path.stem) if isinstance(data, dict) else path.stem
    rel = path.relative_to(datadir).as_posix()

    deleted = [{"kind": kind, "rel": rel, "name": name}]
    if kind == "machine":
        try:
            rows = list_presets(datadir).get("process", [])
        except Exception:
            rows = []
        for row in rows:
            if row.get("rel") == PORTAL_PROCESS_REL:
                continue
            cp = row.get("compatible_printers")
            if isinstance(cp, list) and name in cp:
                prow = _preset_path(row.get("rel")) if False else None
                # resolve via datadir
                try:
                    prow = (datadir / row["rel"]).resolve()
                except Exception:
                    prow = None
                if prow is not None and prow.is_file() and _is_custom(datadir, prow):
                    try:
                        pdata = json.loads(prow.read_text())
                    except (OSError, ValueError):
                        continue
                    pname = pdata.get("name", prow.stem) if isinstance(pdata, dict) else prow.stem
                    prow.unlink(missing_ok=True)
                    deleted.append({"kind": "process", "rel": row["rel"], "name": pname})

    if kind == "process":
        path.unlink(missing_ok=True)
    elif kind == "machine":
        path.unlink(missing_ok=True)

    deleted_rels = {d["rel"] for d in deleted}

    try:
        idx = read_vendor_index(datadir)
        changed_idx = False
        for key in ("machine_model_list", "machine_list", "process_list", "filament_list"):
            before = idx.get(key, [])
            after = [row for row in before
                     if not (isinstance(row, dict) and row.get("sub_path") in deleted_rels)]
            if after != before:
                idx[key] = after
                changed_idx = True
        if changed_idx:
            write_vendor_index(datadir, idx)
    except Exception:
        pass

    cleared = {"machine": False, "process": False, "filament": False}
    try:
        doc = read_profile_document()
        doc_changed = False
        for slot in ("machine", "process", "filament"):
            v = doc.get(slot)
            if isinstance(v, str) and v in deleted_rels:
                doc[slot] = None
                cleared[slot] = True
                doc_changed = True
        for mp in list_presets(datadir).get("machine", []):
            mpath = None
            try:
                mpath = (datadir / mp["rel"]).resolve()
            except Exception:
                mpath = None
            if mpath is None or not mpath.is_file() or not _is_custom(datadir, mpath):
                continue
            try:
                mdata = json.loads(mpath.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(mdata, dict):
                continue
            dpp = mdata.get("default_print_profile")
            deleted_names = {d["name"] for d in deleted if d["kind"] == "process"}
            if isinstance(dpp, str) and dpp in deleted_names:
                del mdata["default_print_profile"]
                write_json_atomic(mpath, mdata)
        if doc_changed:
            write_profile_file(doc)
    except Exception:
        pass

    # printers.json repair is done by the caller (main.py), which owns CONFIG.
    return deleted, cleared
