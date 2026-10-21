"""The preset identity embedded in a 3MF, and how to fit it to this install.

A 3MF exported from another slicer names *its own* printer and process preset inside
``Metadata/project_settings.config``. OrcaSlicer applies those settings when it loads the
file, so handing a foreign 3MF to the CLI alongside ``--load-settings <local machine>``
returns ``-17`` ("the selected printer is not compatible with the process preset") even
after the portal has switched to a local preset. That splits the work in two:

* `read_info` plus the ``match_*``/``choose_printer`` functions answer "which local
  printer and process does this file mean?", tolerating every shape a real export has.
* `rewrite_identity` re-zips a job-local copy whose embedded identity names the
  *local* presets, so the CLI and ``--load-settings`` agree.

Everything here is pure and never raises: a broken upload must surface as that job's own
error, not as a crash while it is being classified. No FastAPI, no printer store, no
``main`` imports — plain dicts in, plain dicts and tuples out.

Identity is read from, in order, ``Metadata/project_settings.config`` (OrcaSlicer/Bambu
JSON), ``Metadata/Slic3r_PE.config`` (PrusaSlicer-style ``key = value`` ini) and
``Metadata/slice_info.config`` (XML, ``printer_model`` only). ``Metadata/model_settings.config``
and ``Metadata/Slic3r_PE_model.config`` are deliberately *not* used for identity: they
describe objects and per-object setting overrides, so reading a printer name out of them
would switch the portal to whatever the first object happened to inherit from. Adopting
per-object overrides is a separate future feature.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

PROJECT_SETTINGS_MEMBER = "Metadata/project_settings.config"
SLIC3R_PE_MEMBER = "Metadata/Slic3r_PE.config"
SLICE_INFO_MEMBER = "Metadata/slice_info.config"

SETTINGS_MEMBERS = (PROJECT_SETTINGS_MEMBER, SLIC3R_PE_MEMBER)
"""Members that carry the project identity; `rewrite_identity` rewrites or drops these."""

IDENTITY_KEYS = ("printer_settings_id", "printer_model", "printer_variant",
                 "print_settings_id", "filament_settings_id")

MEMBER_MAX_BYTES = 8 * 1024 * 1024
"""Largest settings member this reader will decompress into memory.

An identity member is a few KB; a ``.3mf`` is uploader-supplied, and deflate reaches
1000:1, so a member claiming (or hiding) gigabytes must cost at most this much. A member
above the cap reads as missing: the caller keeps the current presets and notes the miss,
which is what a broken upload would get anyway.
"""

MACHINE_THRESHOLD = 60
PROCESS_THRESHOLD = 60
PRINTER_THRESHOLD = 80
PROCESS_COMPATIBLE_BONUS = 30
VARIANT_BONUS = 10

STRIP = False
"""Drop the settings members instead of rewriting them.

The documented ``-17`` fallback: with no project settings OrcaSlicer treats the file as a
plain 3D model and ``--load-settings`` wins outright. A one-line change, as promised in the
plan, because the rewrite already knows which members those are.
"""


@dataclass(frozen=True, slots=True)
class ThreeMfInfo:
    """What a 3MF says about the presets it was sliced for; every field may be missing.

    ``source`` is the member the identity was read from (first member that contributed
    anything), so a surprising switch can be traced back to the file itself.

    ``filaments`` is recorded, never matched: the slicer is told which filament to load
    explicitly (``--load-filaments``), so a name in the file is evidence for the operator,
    not something to resolve against this datadir.
    """
    printer: str | None = None
    printer_model: str | None = None
    printer_variant: str | None = None
    process: str | None = None
    filaments: tuple[str, ...] = ()
    source: str | None = None


# --------------------------------------------------------------------------- reading

def _text(value: object) -> str | None:
    """One non-empty string out of a 3MF value (real exports use strings *and* lists)."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _texts(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ()


def _identity_from_json(raw: bytes) -> dict[str, Any]:
    """Identity keys of a ``project_settings.config`` member; ``{}`` when unreadable."""
    try:
        data = json.loads(raw)
    except ValueError:                      # includes UnicodeDecodeError / JSONDecodeError
        return {}
    if not isinstance(data, dict):
        return {}
    found: dict[str, Any] = {}
    for key in IDENTITY_KEYS:
        if key not in data:
            continue
        if key == "filament_settings_id":
            names = _texts(data[key])
            if names:
                found[key] = names
        else:
            text = _text(data[key])
            if text:
                found[key] = text
    return found


def _identity_from_ini(raw: bytes) -> dict[str, Any]:
    """Identity keys of a PrusaSlicer-style ``key = value`` member; ``{}`` when unreadable."""
    text = raw.decode("utf-8", "replace")
    found: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        if not sep or key.strip() not in IDENTITY_KEYS:
            continue
        value = value.strip().strip('"').strip()
        if not value:
            continue
        if key.strip() == "filament_settings_id":
            found[key.strip()] = (value,)
        else:
            found[key.strip()] = value
    return found


_XML_TAG = re.compile(r"<metadata\b([^>]*)>", re.IGNORECASE)
_XML_ATTR = re.compile(r"([A-Za-z_:][-\w:.]*)\s*=\s*\"([^\"]*)\"")


def _printer_model_id(raw: bytes) -> str | None:
    """The ``printer_model_id`` of a Bambu ``slice_info.config``; ``None`` when absent.

    Scanned with a regex instead of an XML parser on purpose: this is untrusted upload
    content, and one attribute pair does not justify handing it an entity-expanding parser.
    """
    text = raw.decode("utf-8", "replace")
    for tag in _XML_TAG.finditer(text):
        attrs = {name.lower(): value for name, value in _XML_ATTR.findall(tag.group(1))}
        if attrs.get("key", "").strip() == "printer_model_id":
            value = attrs.get("value", "").strip()
            if value:
                return value
    return None


_ARCHIVE_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError,
                   ValueError, MemoryError, zlib.error)
"""Everything reading a hostile archive can raise here.

``RuntimeError`` is zipfile's "file is encrypted", ``zlib.error`` a corrupt deflate
stream, ``MemoryError`` an allocation that cannot be satisfied, ``zipfile.BadZipFile`` a
broken central directory or member CRC, and ``ValueError`` covers a member name or JSON/XML
payload that cannot be decoded.
"""

_MEMBER_READ_ERRORS = (KeyError,) + _ARCHIVE_ERRORS
"""`_ARCHIVE_ERRORS` plus "that name is not a member", raised by ``ZipFile.getinfo``."""


def _member_index(archive: zipfile.ZipFile) -> dict[str, str]:
    """Lower-cased member name -> member name: a case change must not hide the identity."""
    index: dict[str, str] = {}
    for name in archive.namelist():
        index.setdefault(name.replace("\\", "/").lower(), name)
    return index


def _member_bytes(archive: zipfile.ZipFile, index: dict[str, str], member: str) -> bytes | None:
    """One member's bytes, or ``None`` when it is missing, oversized or unreadable.

    The header's size is checked first (cheap, and the usual way to ask for gigabytes),
    then the read stops one byte past `MEMBER_MAX_BYTES` so a header that *understates*
    its own size cannot make this allocate more than the cap. Both cases read as
    "missing": the identity falls back to whatever the portal already has.
    """
    name = index.get(member.lower())
    if name is None:
        return None
    try:
        if archive.getinfo(name).file_size > MEMBER_MAX_BYTES:
            return None
        with archive.open(name) as handle:
            raw = handle.read(MEMBER_MAX_BYTES + 1)
    except _MEMBER_READ_ERRORS:
        return None
    return raw if len(raw) <= MEMBER_MAX_BYTES else None


def read_info(source: Path | bytes) -> ThreeMfInfo:
    """Read the printer/process identity out of a ``.3mf``; never raises.

    Returns an empty `ThreeMfInfo` for anything unreadable — a non-zip blob, a
    missing file, a corrupt JSON/ini member, or an archive with no metadata at all.
    """
    try:
        blob = bytes(source) if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
    except (OSError, TypeError, ValueError):
        return ThreeMfInfo()
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            index = _member_index(archive)
            fields: dict[str, Any] = {}
            origin: str | None = None
            for member, reader in ((PROJECT_SETTINGS_MEMBER, _identity_from_json),
                                   (SLIC3R_PE_MEMBER, _identity_from_ini)):
                raw = _member_bytes(archive, index, member)
                if raw is None:
                    continue
                found = reader(raw)
                if found and origin is None:
                    origin = index[member.lower()]
                for key, value in found.items():
                    # the higher-priority member wins per key, so a JSON member that names
                    # only the printer still lets the ini supply the process
                    fields.setdefault(key, value)
            if not fields.get("printer_model"):
                raw = _member_bytes(archive, index, SLICE_INFO_MEMBER)
                model = _printer_model_id(raw) if raw is not None else None
                if model:
                    fields["printer_model"] = model
                    origin = origin or index[SLICE_INFO_MEMBER.lower()]
    except _ARCHIVE_ERRORS:
        return ThreeMfInfo()

    return ThreeMfInfo(
        printer=_text(fields.get("printer_settings_id")),
        printer_model=_text(fields.get("printer_model")),
        printer_variant=_text(fields.get("printer_variant")),
        process=_text(fields.get("print_settings_id")),
        filaments=_texts(fields.get("filament_settings_id")),
        source=origin,
    )


# ------------------------------------------------------------------------- normalising

_PORTAL_SUFFIX = " (portal options)"
_TOKEN_SPLIT = re.compile(r"[^0-9a-z.]+")


def normalise(s: str) -> tuple[str, str]:
    """``(full, base)`` for comparing preset names; never raises on a non-string.

    Casefolded, ``_`` turned into a space, whitespace collapsed, and the decorations
    OrcaSlicer adds removed: a trailing ``*`` (an edited preset) and a trailing
    ``" (portal options)"`` (our own derived preset). ``base`` is ``full`` without a
    trailing ``"@…"``, so ``"0.20mm Standard @Generic Klipper"`` and
    ``"0.20mm Standard @BBL X1C"`` compare equal.
    """
    if not isinstance(s, str):
        return "", ""
    text = " ".join(s.casefold().replace("_", " ").split())
    while True:
        before = text
        if text.endswith("*"):
            text = text[:-1].strip()
        if text.endswith(_PORTAL_SUFFIX):
            text = text[:-len(_PORTAL_SUFFIX)].strip()
        if text == before:
            break
    full = text
    head, sep, _ = full.rpartition("@")
    base = head.strip() if sep else full
    return full, base or full


def _tokens(text: object) -> set[str]:
    if not isinstance(text, str):
        return set()
    normalised, _ = normalise(text)
    return {token.strip(".") for token in _TOKEN_SPLIT.split(normalised) if len(token.strip(".")) >= 2}


def _same_text(left: object, right: object) -> bool:
    """Normalised equality that treats a missing value as matching nothing."""
    first, _ = normalise(left) if isinstance(left, str) else ("", "")
    second, _ = normalise(right) if isinstance(right, str) else ("", "")
    return bool(first) and first == second


def _name_score(targets: Sequence[str], preset: dict) -> int:
    """100 for an exact preset name, 80 for an ``@``-suffix-insensitive one, else 0."""
    name = preset.get("name")
    full, base = normalise(name) if isinstance(name, str) else ("", "")
    best = 0
    for target in targets:
        target_full, target_base = normalise(target)
        if not target_full:
            continue
        if target_full == full:
            return 100
        if target_base and target_base == base:
            best = 80
    return best


def _token_score(targets: Sequence[str], texts: Sequence[object]) -> int:
    """40 when the identity and the preset share at least two whole tokens."""
    wanted: set[str] = set()
    for target in targets:
        wanted |= _tokens(target)
    if not wanted:
        return 0
    for text in texts:
        if len(wanted & _tokens(text)) >= 2:
            return 40
    return 0


def _pick(scored: list[tuple[int, dict]], threshold: int) -> dict | None:
    """The unique best preset scoring at least ``threshold``; a tie means "never guess"."""
    if not scored:
        return None
    best = max(score for score, _ in scored)
    if best < threshold:
        return None
    winners = [row for score, row in scored if score == best]
    return winners[0] if len(winners) == 1 else None


# --------------------------------------------------------------------------- matching

def _machine_score(info: ThreeMfInfo, preset: dict) -> int:
    targets = [t for t in (info.printer, info.printer_model) if t]
    score = _name_score(targets, preset)
    if score < 60:
        model = preset.get("printer_model")
        model_full = normalise(model)[0] if isinstance(model, str) else ""
        if model_full and any(normalise(target)[0] == model_full for target in targets):
            score = 60
    if score == 0:
        score = _token_score(targets, [preset.get("name"), preset.get("printer_model")])
    if score and _same_text(info.printer_variant, preset.get("printer_variant")):
        score += VARIANT_BONUS
    return score


def match_machine(presets: list[dict], info: ThreeMfInfo) -> dict | None:
    """The local machine preset the 3MF names, or ``None`` when that is not knowable.

    Accepts only a unique best scoring >= 60: an ambiguous or fuzzy-only file must fall
    back to the current machine instead of silently printing for the wrong hardware.
    """
    if not info.printer and not info.printer_model:
        return None
    scored = [(_machine_score(info, preset), preset)
              for preset in presets if isinstance(preset, dict)]
    return _pick([item for item in scored if item[0] > 0], MACHINE_THRESHOLD)


def _compatible(preset: dict, machine_name: str) -> bool:
    for name in _texts(preset.get("compatible_printers")):
        if _same_text(name, machine_name):
            return True
    return False


def _default_process(presets: list[dict], machine: dict | None) -> dict | None:
    """The process preset the matched machine declares as its default, resolved by name."""
    if not isinstance(machine, dict):
        return None
    default = _text(machine.get("default_print_profile"))
    if not default:
        return None
    scored = [(_name_score([default], preset), preset)
              for preset in presets if isinstance(preset, dict)]
    return _pick([item for item in scored if item[0] > 0], PROCESS_THRESHOLD)


def match_process(presets: list[dict], info: ThreeMfInfo, machine: dict | None) -> dict | None:
    """The local process preset the 3MF names, the machine's default, or ``None``.

    ``None`` is not an error — the caller keeps the current process and records the miss.
    """
    targets = [info.process] if info.process else []
    machine_name = machine.get("name") if isinstance(machine, dict) else None
    scored: list[tuple[int, dict]] = []
    for preset in presets:
        if not isinstance(preset, dict):
            continue
        score = _name_score(targets, preset) if targets else 0
        if score == 0:
            score = _token_score(targets, [preset.get("name")])
        if machine_name and _compatible(preset, machine_name):
            score += PROCESS_COMPATIBLE_BONUS
        if score:
            scored.append((score, preset))
    chosen = _pick(scored, PROCESS_THRESHOLD)
    return chosen if chosen is not None else _default_process(presets, machine)


def _paired_machine(row: dict) -> str | None:
    profiles = row.get("profiles")
    if isinstance(profiles, dict):
        value = profiles.get("machine")
        if isinstance(value, str) and value:
            return value
    return None


def _printer_score(row: dict, info: ThreeMfInfo, machine: dict | None) -> int:
    name = row.get("name")
    if _same_text(name, info.printer):
        return 100
    if _same_text(name, info.printer_model):
        return 90
    if isinstance(machine, dict):
        rel = machine.get("rel")
        paired = _paired_machine(row)
        if isinstance(rel, str) and rel and paired == rel:
            return 80
    # a shared-word name is a hint, not a match: it tops out below `PRINTER_THRESHOLD`
    return _token_score([t for t in (info.printer, info.printer_model) if t], [name])


def _label_for(printer_rows: list[dict], current_id: str | None) -> str:
    if current_id is None:
        return "none"
    for row in printer_rows:
        if isinstance(row, dict) and row.get("id") == current_id:
            name = row.get("name")
            if isinstance(name, str) and name:
                return name
    return current_id


def choose_printer(printer_rows: list[dict], info: ThreeMfInfo,
                   machine: dict | None, current_id: str | None) -> tuple[str | None, list[str]]:
    """The configured printer the 3MF names, as ``(id, notes)``.

    ``None`` means "leave the selection alone": either the file names a printer that is
    not configured here or the match is ambiguous. Both say so in the notes, because the
    operator will wonder why the selection did not follow the file. A printer paired with
    the matched machine outscores a fuzzy name match, and a tie between equals keeps
    ``current_id``.
    """
    targets = [t for t in (info.printer, info.printer_model) if t]
    if not targets:
        return None, []
    scored = [(_printer_score(row, info, machine), row)
              for row in printer_rows if isinstance(row, dict)]
    scored = [item for item in scored if item[0] > 0]
    if scored:
        best = max(score for score, _ in scored)
        if best >= PRINTER_THRESHOLD:
            winners = [row for score, row in scored if score == best]
            if len(winners) == 1:
                chosen = winners[0].get("id")
                return (chosen if isinstance(chosen, str) and chosen else None), []
            current = [row for row in winners if row.get("id") == current_id]
            if current:
                chosen = current[0].get("id")
                return (chosen if isinstance(chosen, str) and chosen else None), []
            wanted = info.printer or info.printer_model
            return None, [f"printer '{wanted}' from the 3MF matches more than one configured "
                          f"printer; kept '{_label_for(printer_rows, current_id)}'"]
    wanted = info.printer or info.printer_model
    return None, [f"printer '{wanted}' from the 3MF is not configured here; "
                  f"kept '{_label_for(printer_rows, current_id)}'"]


# --------------------------------------------------------------------------- rewriting

def _identity_updates(machine: dict | None, process: dict | None) -> dict[str, str]:
    """The identity fields the local presets stand for, in preset (string) form.

    ``filament_settings_id`` is deliberately absent: the slicer command already names the
    filament with an explicit ``--load-filaments``, so rewriting the member would only let
    the file disagree with the command line. What the upload asked for stays readable in
    `ThreeMfInfo.filaments`.
    """
    updates: dict[str, str] = {}
    machine_name = None
    if isinstance(machine, dict):
        machine_name = _text(machine.get("name"))
        if machine_name:
            updates["printer_settings_id"] = machine_name
        for preset_key, member_key in (("printer_model", "printer_model"),
                                       ("printer_variant", "printer_variant")):
            value = _text(machine.get(preset_key))
            if value:
                updates[member_key] = value
    if isinstance(process, dict):
        process_name = _text(process.get("name"))
        if process_name:
            updates["print_settings_id"] = process_name
    if machine_name:
        updates["compatible_printers"] = machine_name
    return updates


def _rewrite_json(raw: bytes, updates: dict[str, str]) -> bytes | None:
    """The JSON member with the identity replaced; ``None`` when it cannot be edited."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    for key, value in updates.items():
        # OrcaSlicer writes project settings as one-element lists, compatible_printers too
        data[key] = [value]
    return json.dumps(data, indent=4, sort_keys=False).encode()


def _rewrite_ini(raw: bytes, updates: dict[str, str]) -> bytes:
    """The ini member with the identity replaced; unknown lines are kept verbatim."""
    text = raw.decode("utf-8", "replace")
    written: set[str] = set()
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped and not stripped.startswith(("#", ";")) else ""
        if key in updates:
            if key not in written:
                lines.append(f"{key} = {updates[key]}")
                written.add(key)
            continue
        lines.append(line)
    for key, value in updates.items():
        if key not in written:
            lines.append(f"{key} = {value}")
    return ("\n".join(lines) + "\n").encode()


def _rezip(archive: zipfile.ZipFile, replacements: dict[str, bytes],
           dropped: set[str]) -> bytes:
    """Copy every member verbatim except the replaced ones; ``dropped`` are left out.

    `MEMBER_MAX_BYTES` does not apply to the copy: a geometry member may legitimately be
    far larger than an identity member, and skipping it would hand the slicer a broken
    project. Untouched members are instead streamed from one archive into the other in
    chunks, so no member is ever held in memory whole.
    """
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in archive.infolist():
            if item.filename in dropped:
                continue
            clone = zipfile.ZipInfo(item.filename, item.date_time)
            clone.compress_type = (item.compress_type
                                   if item.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                                   else zipfile.ZIP_DEFLATED)
            clone.external_attr = item.external_attr
            clone.internal_attr = item.internal_attr
            clone.create_system = item.create_system
            data = replacements.get(item.filename)
            if data is not None:
                target.writestr(clone, data)
            else:
                with archive.open(item) as source, target.open(clone, "w") as sink:
                    shutil.copyfileobj(source, sink)
    return out.getvalue()


def rewrite_identity(blob: bytes, machine: dict | None,
                     process: dict | None) -> tuple[bytes, str]:
    """Re-identify a job-local copy of a 3MF; returns ``(bytes, outcome)``.

    ``outcome`` is ``"identity"`` when the settings members were rewritten to name the
    local presets, ``"stripped"`` when `STRIP` removed them instead, and ``"none"``
    when nothing could or needed to change — in which case the bytes are returned
    untouched so the original upload is what gets sliced.
    """
    updates = _identity_updates(machine, process)
    if not updates:
        return blob, "none"
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            index = _member_index(archive)
            present = [index[member.lower()] for member in SETTINGS_MEMBERS
                       if member.lower() in index]
            if not present:
                return blob, "none"
            if STRIP:
                return _rezip(archive, {}, set(present)), "stripped"
            replacements: dict[str, bytes] = {}
            for name in present:
                raw = _member_bytes(archive, index, name)
                if raw is None:
                    # an oversized or unreadable settings member has no identity we can
                    # read, so the upload is sliced exactly as it arrived
                    return blob, "none"
                edited = (_rewrite_json(raw, updates)
                          if name.lower() == PROJECT_SETTINGS_MEMBER.lower()
                          else _rewrite_ini(raw, updates))
                if edited is None:
                    # an unparseable settings member has no identity to correct and the
                    # slicer cannot read it either, so the upload is sliced as-is
                    return blob, "none"
                replacements[name] = edited
            return _rezip(archive, replacements, set()), "identity"
    except _MEMBER_READ_ERRORS:
        return blob, "none"
