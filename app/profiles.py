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
take `"User"`/`"user"` and reject `"System"`. The CLI tolerates a dangling `inherits` but
silently uses defaults, so the portal resolves chains itself and refuses incomplete slices.
Import sets the type and `from`, rewrites `compatible_printers` to the imported machine's own
name, and preserves the remaining source settings.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import stat
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


# The upload endpoint refuses bundles above this size; a single zip member may not be
# larger either, so an inflated member cannot be read into memory.
BUNDLE_MAX_BYTES = 20 * 1024 * 1024

# The resources belonging to the pinned Orca installation, not the writable datadir.
ORCA_PROFILES = Path('/opt/orca/resources/profiles')


def _resolved_chain(path: Path, ptype: str, datadir: Path,
                    resources: Path | None = None, *,
                    chain_paths: list[Path] | None = None) -> list[dict]:
    """Resolve a complete chain without changing the selected or parent presets.

    A bundled vendor preset has a trusted vendor context; custom presets do not.
    Otherwise require a unique name across both roots. Never interpret inherits as a path.
    """
    if ptype not in ('machine', 'process', 'filament'):
        raise ProfileError(f'unknown preset type: {ptype}')
    roots = [Path(datadir).resolve(), Path(resources or ORCA_PROFILES).resolve()]
    path = Path(path).resolve()
    if not any(path.is_relative_to(root) for root in roots):
        raise ProfileError('preset escapes the preset roots')

    def read_preset(file: Path) -> dict:
        try:
            if file.stat().st_size > BUNDLE_MAX_BYTES:
                raise ValueError('preset is too large')
            value = json.loads(file.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ProfileError(f'unreadable {ptype} preset: {file.name}') from exc
        if not isinstance(value, dict) or value.get('type') not in (None, ptype):
            raise ProfileError(f'invalid {ptype} preset: {file.name}')
        return value

    bindings = load_parent_edges(roots[0])

    def identity(file: Path) -> str:
        for prefix, root in zip(('datadir', 'resources'), roots):
            if file.is_relative_to(root):
                return prefix + ':' + file.relative_to(root).as_posix()
        raise ProfileError('preset escapes the preset roots')

    def mapped_parent(binding: object, expected: str) -> tuple[Path, dict]:
        if not isinstance(binding, dict) or set(binding) != {'parent', 'inherits', 'sha256'} or binding['inherits'] != expected:
            raise ProfileError(f'invalid explicit {ptype} parent mapping for {path.name}')
        target = binding['parent']
        digest = binding['sha256']
        if not isinstance(target, str) or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ProfileError(f'invalid explicit {ptype} parent mapping for {path.name}')
        prefix, sep, rel = target.partition(':')
        if not sep or prefix not in ('datadir', 'resources') or not rel or Path(rel).is_absolute() or '..' in Path(rel).parts:
            raise ProfileError('invalid explicit parent path')
        root = roots[('datadir', 'resources').index(prefix)]
        candidate = (root / rel).resolve()
        if not candidate.is_relative_to(root) or ptype not in candidate.relative_to(root).parts or candidate.suffix != '.json':
            raise ProfileError('explicit parent escapes its preset root or type')
        try:
            with candidate.open('rb') as handle:
                raw = handle.read(BUNDLE_MAX_BYTES + 1)
            if len(raw) > BUNDLE_MAX_BYTES or hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('digest mismatch')
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise ProfileError(f'explicit {ptype} parent is unavailable or changed') from exc
        if not isinstance(data, dict) or data.get('type') not in (None, ptype):
            raise ProfileError(f'invalid {ptype} preset: {candidate.name}')
        if (data.get('name') or candidate.stem) != expected:
            raise ProfileError(f'explicit {ptype} parent name does not match inherits')
        return candidate, data

    seen: set[Path] = set()
    chain: list[dict] = []
    verified: dict[Path, dict] = {}
    index: dict[str, set[Path]] | None = None
    while True:
        if path in seen:
            raise ProfileError(f'cyclic {ptype} preset inheritance at {path.name}')
        if len(seen) >= 128:
            raise ProfileError(f'{ptype} preset inheritance is too deep')
        seen.add(path)
        if chain_paths is not None:
            chain_paths.append(path)
        data = verified.pop(path, None)
        if data is None:
            data = read_preset(path)
        chain.append(data)
        parent = data.get('inherits')
        if parent is None or parent == '':
            break
        if not isinstance(parent, str) or not parent.strip():
            raise ProfileError(f'invalid {ptype} preset inherits in {path.name}')
        if identity(path) in bindings:
            path, parent_data = mapped_parent(bindings[identity(path)], parent)
            verified[path] = parent_data
            continue
        if index is None:
            index = {}
            for root in roots:
                if not root.is_dir():
                    continue
                for candidate in root.glob(f'**/{ptype}/**/*.json'):
                    resolved = candidate.resolve()
                    if not resolved.is_relative_to(root) or not resolved.is_file():
                        continue
                    try:
                        value = read_preset(resolved)
                    except ProfileError:
                        continue
                    name = value.get('name') or candidate.stem
                    if isinstance(name, str):
                        index.setdefault(name, set()).add(resolved)
        matches = sorted(index.get(parent, set()))
        # Within a vendor, Orca stores some presets in subdirectories. Prefer the
        # nearest containing directory, stopping at the type directory; never pick a
        # sibling vendor merely because filesystem traversal happens to find it first.
        directory = path.parent
        choices = []
        # Only bundled resources supply a trusted vendor namespace. An imported
        # Custom preset has no provenance, so a local file must not shadow a
        # different bundled parent with the same name.
        if path.is_relative_to(roots[1]):
            while directory.name != ptype and directory.parent != directory:
                choices = [candidate for candidate in matches if candidate.parent == directory]
                if choices:
                    break
                directory = directory.parent
            if not choices and directory.name == ptype:
                choices = [candidate for candidate in matches if candidate.parent == directory]
            if not choices and directory.name == ptype:
                choices = [candidate for candidate in matches if candidate.is_relative_to(directory)]
            if not choices:
                raise ProfileError(f'missing {ptype} parent {parent!r} in bundled vendor for {path.name}')
        else:
            choices = matches
        if not choices:
            raise ProfileError(f'missing {ptype} parent {parent!r} for {path.name}')
        if len(choices) != 1:
            raise ProfileError(f'ambiguous {ptype} parent {parent!r} for {path.name}')
        path = choices[0]
    return chain


PARENT_MAP_NAME = '.print-portal-parent-map.json'
PARENT_MAP_MAX = 1_000_000


def load_parent_edges(datadir: Path) -> dict:
    """Read explicit parent bindings. A missing file means none; a symlink is refused."""
    path = Path(datadir) / PARENT_MAP_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ProfileError('invalid parent mapping file') from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > PARENT_MAP_MAX:
        raise ProfileError('invalid parent mapping file')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ProfileError('invalid parent mapping file') from exc
    try:
        with os.fdopen(fd, 'r', encoding='utf-8') as handle:
            raw = handle.read(PARENT_MAP_MAX + 1)
    except OSError as exc:
        raise ProfileError('invalid parent mapping file') from exc
    if len(raw) > PARENT_MAP_MAX:
        raise ProfileError('invalid parent mapping file')
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ProfileError('invalid parent mapping file') from exc
    if not isinstance(data, dict) or data.get('version') != 1 or set(data) != {'version', 'edges'}:
        raise ProfileError('invalid parent mapping file')
    edges = data['edges']
    if not isinstance(edges, dict):
        raise ProfileError('invalid parent mapping file')
    return edges


def save_parent_edges(datadir: Path, edges: dict) -> None:
    write_json_atomic(Path(datadir) / PARENT_MAP_NAME, {'version': 1, 'edges': edges})


def list_parent_candidates(child: Path, ptype: str, datadir: Path,
                           resources: Path | None = None) -> dict:
    """List same-name parents without choosing one."""
    if ptype not in ('machine', 'process', 'filament'):
        raise ProfileError(f'unknown preset type: {ptype}')
    roots = [Path(datadir).resolve(), Path(resources or ORCA_PROFILES).resolve()]
    child = Path(child).resolve()
    if not child.is_relative_to(roots[0]) or ptype not in child.relative_to(roots[0]).parts:
        raise ProfileError('parent mapping applies only to a datadir preset of that type')
    try:
        data = json.loads(child.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ProfileError(f'unreadable {ptype} preset: {child.name}') from exc
    if not isinstance(data, dict):
        raise ProfileError(f'invalid {ptype} preset: {child.name}')
    inherits = data.get('inherits')
    if not isinstance(inherits, str) or not inherits.strip():
        raise ProfileError('preset has no parent to map')
    candidates = []
    for root, prefix in zip(roots, ('datadir', 'resources')):
        if not root.is_dir():
            continue
        for candidate in root.glob(f'**/{ptype}/**/*.json'):
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if (not resolved.is_relative_to(root) or resolved == child or not resolved.is_file()
                    or resolved.is_symlink() or resolved.stat().st_size > BUNDLE_MAX_BYTES):
                continue
            try:
                value = json.loads(resolved.read_text(encoding='utf-8'))
                raw = resolved.read_bytes()
            except (OSError, ValueError):
                continue
            if not isinstance(value, dict) or value.get('type') not in (None, ptype):
                continue
            name = value.get('name') or candidate.stem
            if name != inherits:
                continue
            candidates.append({
                'id': prefix + ':' + resolved.relative_to(root).as_posix(),
                'name': name,
                'sha256': hashlib.sha256(raw).hexdigest(),
                'source': prefix,
            })
    candidates.sort(key=lambda item: item['id'])
    return {'child': child.relative_to(roots[0]).as_posix(), 'type': ptype,
            'inherits': inherits, 'candidates': candidates}


def bind_parent(datadir: Path, resources: Path | None, child_rel: str, candidate_id: str,
                expected_inherits: str, expected_digest: str) -> dict:
    """Bind one explicit edge and roll it back if the chain still cannot resolve."""
    if (not isinstance(child_rel, str) or not isinstance(candidate_id, str)
            or Path(child_rel).is_absolute() or '..' in Path(child_rel).parts):
        raise ProfileError('invalid child preset')
    ptype = next((part for part in ('machine', 'process', 'filament') if part in Path(child_rel).parts), None)
    if ptype is None:
        raise ProfileError('invalid child preset')
    child = (Path(datadir) / child_rel).resolve()
    listing = list_parent_candidates(child, ptype, datadir, resources)
    if listing['inherits'] != expected_inherits:
        raise ProfileError('child inherits value does not match')
    match = next((item for item in listing['candidates']
                  if item['id'] == candidate_id and item['sha256'] == expected_digest
                  and item['name'] == expected_inherits), None)
    if match is None:
        raise ProfileError('explicit parent is not a current candidate')
    key = 'datadir:' + Path(listing['child']).as_posix()
    edges = load_parent_edges(datadir)
    previous = edges.get(key)
    edges[key] = {'parent': candidate_id, 'inherits': expected_inherits, 'sha256': expected_digest}
    save_parent_edges(datadir, edges)
    try:
        effective_preset(child, ptype, datadir, resources)
    except ProfileError:
        if previous is None:
            edges.pop(key, None)
        else:
            edges[key] = previous
        save_parent_edges(datadir, edges)
        raise
    return {'child': listing['child'], 'bound': candidate_id, 'resolved': True}


def effective_preset(path: Path, ptype: str, datadir: Path,
                     resources: Path | None = None) -> dict:
    """Return child-over-parent values with no unresolved inherits reference."""
    chain = _resolved_chain(path, ptype, datadir, resources)
    merged: dict = {}
    for level in reversed(chain):
        merged.update(level)
    merged.pop('inherits', None)
    return merged


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
    """Intersect a convex bed polygon with the size limit anchored at its minimum corner.

    Never replace a polygon by its bounding rectangle. A concave polygon whose clipping
    could produce disjoint regions is rejected instead of inventing printable space.
    Unparseable areas are left to the existing preset validation.
    """
    try:
        points = [tuple(float(c) for c in v.lower().split('x')) for v in area]
        if len(points) < 3 or any(len(p) != 2 or not all(math.isfinite(c) for c in p) for p in points):
            return None
        w, h = bed
        if not math.isfinite(w) or not math.isfinite(h) or w <= 0 or h <= 0:
            raise ProfileError('invalid configured bed dimensions')
    except (AttributeError, ValueError, TypeError):
        return None
    first = points[0]
    xmin, ymin = min(p[0] for p in points), min(p[1] for p in points)
    xmax, ymax = xmin + w, ymin + h
    if all(x <= xmax and y <= ymax for x, y in points):
        return None
    turns = []
    for a, b, c in zip(points, points[1:] + points[:1], points[2:] + points[:2]):
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if cross:
            turns.append(cross > 0)
    if not turns or any(turn != turns[0] for turn in turns):
        raise ProfileError('cannot safely clamp a non-convex printable_area')
    for axis, limit in ((0, xmax), (1, ymax)):
        clipped = []
        for start, end in zip(points[-1:] + points[:-1], points):
            start_in, end_in = start[axis] <= limit, end[axis] <= limit
            if start_in != end_in:
                t = (limit - start[axis]) / (end[axis] - start[axis])
                clipped.append(tuple(start[i] + t * (end[i] - start[i]) for i in range(2)))
            if end_in:
                clipped.append(end)
        points = [point for i, point in enumerate(clipped) if i == 0 or point != clipped[i - 1]]
        if len(points) > 1 and points[0] == points[-1]:
            points.pop()
    if len(points) < 3:
        raise ProfileError('configured bed leaves no printable_area')
    if first in points:
        index = points.index(first)
        points = points[index:] + points[:index]
    def coord(value: float) -> str:
        snapped = round(value, 6)
        if abs(snapped - round(snapped)) <= 1e-6:
            return str(int(round(snapped)))
        return format(snapped, 'f').rstrip('0').rstrip('.')
    return [f'{coord(x)}x{coord(y)}' for x, y in points]


def clamp_machine(data: dict, bed: tuple[float, float]) -> None:
    """Apply the import bed limit to an effective machine, including inherited area."""
    area = data.get("printable_area")
    if isinstance(area, list):
        clamped = clamp_area(area, bed)
        if clamped is not None:
            data["printable_area"] = clamped


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
    is kept verbatim and checked after the entire bundle has been written, and
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
        # Preserve the relationship; import_into reports if it cannot be resolved.
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


def summarise(path: Path, ptype: str, datadir: Path | None = None) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"name": path.stem, "error": "unreadable"}
    out = {"name": data.get("name", path.stem), "file": path.name, "type": ptype}
    try:
        effective = effective_preset(path, ptype, datadir) if datadir is not None else data
    except ProfileError as exc:
        # Keep the row visible, but never classify unknown compatibility as universal.
        effective = data
        out["error"] = str(exc)
    if datadir is not None:
        # the value the printer pairing stores, so the UI never rebuilds a path from the basename
        try:
            out["rel"] = Path(path).relative_to(Path(datadir)).as_posix()
        except ValueError:
            pass
    fields = SUMMARISE.get(ptype, [])
    for key in fields:
        if key in effective:
            out[key] = effective[key]
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
    """Use the resolved process width, or the existing portal option's 5 mm fallback."""
    return _positive_width(data.get("brim_width")) or "5"


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
    """Build the flattened per-slice process with portal options, without mutating sources."""
    data = effective_preset(process, "process", datadir)

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
        data = effective_preset(process, "process", datadir)
    except ProfileError as exc:
        message = "process preset is not readable" if not Path(process).is_file() else str(exc)
        return {"error": message}

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

    `stale` reports whether the shared UI-derived artifact is missing, unreadable or has
    different option fields. Slices independently capture a temporary effective preset.
    """
    try:
        data = effective_preset(process, "process", datadir)
    except ProfileError as exc:
        message = "process preset is not readable" if not Path(process).is_file() else str(exc)
        return {"path": PORTAL_PROCESS_REL, "stale": True, "error": message}

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
    if machine.get("error"):
        return []
    wanted = _preset_name(machine.get("name"))
    if not wanted[0]:
        return rows
    kept = []
    for row in rows:
        if isinstance(row, dict) and row.get("error"):
            continue
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
                bed: tuple[float, float] = (220.0, 220.0),
                protected: dict[str, Path] | None = None) -> dict:
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

    # Do not let an unselected import change or make ambiguous a currently
    # resolvable chain. Unresolved selections are deliberately excluded so a
    # missing parent can be imported to repair them.
    for kind, path in (protected or {}).items():
        paths: list[Path] = []
        try:
            chain = _resolved_chain(path, kind, datadir, chain_paths=paths)
        except ProfileError:
            continue
        names = {level.get('inherits') for level in chain if isinstance(level.get('inherits'), str)}
        names.update(level.get('name') for level in chain if isinstance(level.get('name'), str))
        for planned_kind, preset, dest in planned:
            if planned_kind != kind or (preset.get('name') not in names and dest.resolve() not in paths):
                continue
            try:
                unchanged = dest.is_file() and json.loads(dest.read_text()) == preset
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                raise ProfileError(f"import would change or make ambiguous the selected {kind} chain at {preset.get('name')!r}; choose another preset before importing")

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

    # A bundle can reference a parent it does not contain. Preserve the import so
    # the operator can import the missing preset later, but do not silently treat
    # the incomplete chain as usable. Validate only after all bundle files exist.
    for row in imported:
        try:
            effective_preset(datadir / row["path"], row["type"], datadir)
        except ProfileError as exc:
            row["error"] = str(exc)
            notes.append(f"{row['name']}: {exc}; import the correct parent preset before selecting or slicing, or bind an explicit parent when the name is ambiguous")
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
        if key in level:
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
    error = None
    try:
        chain = _resolved_chain(path, kind, datadir)
    except ProfileError as exc:
        chain = [data]
        error = str(exc)
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
        "error": error,
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
