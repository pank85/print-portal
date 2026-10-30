"""Read the statistics a slicer writes into a gcode header.

OrcaSlicer and PrusaSlicer-lineage slicers dump the numbers the user cares about
(grams of filament, estimated time, layer count, layer height) as ``;`` comments
near the top of the file. A profile that prints every setting puts that summary
roughly 230 kB in, so the scan window has to be far larger than the summary
offset — but it still must not read a 200 MB gcode into memory. Everything here
reads at most ``MAX_HEAD_BYTES``, chunk by chunk, and tolerates junk bytes.

Two header shapes are understood:

* OrcaSlicer, e.g. ``; total filament used [g] = 3.94``
* the plainer PrusaSlicer style, e.g. ``;TIME:125`` / ``;Filament used:1.5m``

Prusa-style values are only used as fallbacks for what the Orca-style header is
missing, and are normalised to the same units and human shape.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

MAX_HEAD_BYTES = 2_000_000
"""Window scanned from the start of the file (bytes). The documented worst case
summary offset is ~230 kB, so 2 MB leaves an order of magnitude of headroom."""

_CHUNK_BYTES = 64 * 1024
_PLA_G_PER_CM3 = 1.24        # PLA density: only OrcaSlicer's cm3 fallback needs it
_PRUSA_G_PER_MM = 0.0029     # ~2.9 mg per mm of 1.75 mm PLA; the Prusa fallback uses 100 mm/m

ORCA_PATTERNS: dict[str, re.Pattern[str]] = {
    "grams": re.compile(r"^; total filament used \[g\] = ([\d.]+)", re.M),
    "cm3": re.compile(r"^; filament used \[cm3\] = ([\d.]+)", re.M),
    "mm": re.compile(r"^; filament used \[mm\] = ([\d.]+)", re.M),
    "time": re.compile(r"^; estimated printing time \(normal mode\) = (.+)$", re.M),
    "layers": re.compile(r"^; total layers count = (\d+)", re.M),
    "height": re.compile(r"^; layer_height = ([\d.]+)", re.M),
}

PRUSA_PATTERNS: dict[str, re.Pattern[str]] = {
    "time_s": re.compile(r"^;TIME:(\d+)", re.M),
    "mm": re.compile(r"^;Filament used:([\d.]+)m", re.M),
    "height": re.compile(r"^;Layer height:([\d.]+)", re.M),
}


def _as_float(text: str | None) -> float | None:
    """float() that treats junk as absent — a binary-garbage match must not raise."""
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _human_time(seconds: int) -> str:
    """OrcaSlicer's own shape: ``1h 2m 3s``, or ``1m 2s`` below an hour."""
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {secs}s" if hours else f"{minutes}m {secs}s"


def _collect(block: str, orca: dict[str, str], prusa: dict[str, str]) -> None:
    """Record the first value seen per key; blocks always end on a line boundary."""
    for key, pattern in ORCA_PATTERNS.items():
        if key in orca:
            continue
        m = pattern.search(block)
        if m:
            orca[key] = m.group(1)
    for key, pattern in PRUSA_PATTERNS.items():
        if key in prusa:
            continue
        m = pattern.search(block)
        if m:
            prusa[key] = m.group(1)


def _header_complete(orca: dict[str, str]) -> bool:
    """True once the Orca-style header alone answers every question we can ask."""
    grams = _as_float(orca.get("grams"))
    return len(orca) == len(ORCA_PATTERNS) and grams is not None and grams > 0


def _scan_header(gcode: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Stream the first ``MAX_HEAD_BYTES`` and return raw (orca, prusa) matches.

    Chunks are re-joined with the trailing partial line so a match is never split
    across a chunk boundary, which keeps this equivalent to searching the whole
    window at once while binding memory to a chunk instead of the file. Line
    endings are normalised (CRLF, lone CR, LF) before the split, the way opening
    the file in text mode used to.
    """
    orca: dict[str, str] = {}
    prusa: dict[str, str] = {}
    budget = MAX_HEAD_BYTES
    pending = ""
    with Path(gcode).open("rb") as fh:
        while budget > 0:
            chunk = fh.read(min(_CHUNK_BYTES, budget))
            if not chunk:
                break
            budget -= len(chunk)
            text = pending + chunk.decode("utf-8", errors="replace")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            cut = text.rfind("\n")
            if cut < 0:
                pending = text
                continue
            block, pending = text[:cut], text[cut + 1:]
            _collect(block, orca, prusa)
            if _header_complete(orca):
                return orca, prusa
    if pending:
        _collect(pending, orca, prusa)
    return orca, prusa


def parse_gcode_stats(gcode: Path) -> dict[str, Any]:
    """Statistics from the slicer's own header comments.

    Returns ``{"grams": float, "mm"?: float, "layers"?: int, "time"?: str,
    "layer_height"?: str}``; ``grams`` is always present, the rest only when the
    header supplied them. An unreadable file yields ``{}``.
    """
    try:
        orca, prusa = _scan_header(gcode)
    except OSError:
        return {}

    stats: dict[str, Any] = {}
    grams = _as_float(orca.get("grams"))
    if not grams:
        cm3 = _as_float(orca.get("cm3"))
        grams = round(cm3 * _PLA_G_PER_CM3, 2) if cm3 else 0.0
    stats["grams"] = grams

    mm = _as_float(orca.get("mm"))
    if mm is not None:
        stats["mm"] = mm
    layers = _as_int(orca.get("layers"))
    if layers is not None:
        stats["layers"] = layers
    if orca.get("time"):
        stats["time"] = orca["time"].strip()
    if orca.get("height"):
        stats["layer_height"] = orca["height"].strip()

    # Prusa-style fallbacks, for the header shapes that carry only these.
    seconds = _as_int(prusa.get("time_s"))
    if "time" not in stats and seconds is not None:
        stats["time"] = _human_time(seconds)
    metres = _as_float(prusa.get("mm"))
    if "mm" not in stats and metres is not None:
        stats["mm"] = round(metres * 1000, 1)
        if not stats["grams"]:
            stats["grams"] = round(metres * 100 * _PRUSA_G_PER_MM, 2)
    if "layer_height" not in stats and prusa.get("height"):
        stats["layer_height"] = prusa["height"].strip()
    return stats


_LAYER_NUM_RE = re.compile(r"^BEFORE_LAYER_CHANGE\s+LAYER=(\d+)")
_Z_COMMENT_RE = re.compile(r"^;Z:([\d.]+)")
_HEIGHT_COMMENT_RE = re.compile(r"^;HEIGHT:([\d.]+)")
_G_MOVE_RE = re.compile(r"^G[01]\b")
_Z_PARAM_RE = re.compile(r"\bZ(-?[\d.]+)")
_X_PARAM_RE = re.compile(r"\bX(-?[\d.]+)")
_Y_PARAM_RE = re.compile(r"\bY(-?[\d.]+)")
_E_PARAM_RE = re.compile(r"\bE(-?[\d.]+)")


def _fmt_num(val: float) -> str:
    text = f"{val:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def inspect_gcode_layers(gcode: Path) -> list[dict[str, Any]]:
    """Scan a G-code file for layer boundaries and their Z heights.

    Returns a list of ``{"layer": int, "z": float, "height": float, "line": int}``
    entries ordered by appearance in the file.
    """
    try:
        raw = Path(gcode).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    raw_layers: list[dict[str, Any]] = []
    default_h = 0.2
    for line in lines[:200]:
        if line.startswith(";Layer height:"):
            h = _as_float(line.split(":", 1)[1].strip())
            if h and h > 0:
                default_h = h
                break

    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped == ";LAYER_CHANGE":
            start_line = i
            z_val: float | None = None
            h_val: float | None = None
            layer_num: int | None = None
            j = i + 1
            while j < min(i + 15, n):
                s = lines[j].strip()
                if s == ";LAYER_CHANGE":
                    break
                if z_val is None:
                    mz = _Z_COMMENT_RE.match(s)
                    if mz:
                        z_val = _as_float(mz.group(1))
                if h_val is None:
                    mh = _HEIGHT_COMMENT_RE.match(s)
                    if mh:
                        h_val = _as_float(mh.group(1))
                if layer_num is None:
                    ml = _LAYER_NUM_RE.match(s)
                    if ml:
                        layer_num = _as_int(ml.group(1))
                if z_val is None and _G_MOVE_RE.match(s):
                    mgz = _Z_PARAM_RE.search(s)
                    if mgz:
                        z_val = _as_float(mgz.group(1))
                j += 1
            idx = len(raw_layers)
            prev_z = raw_layers[-1]["z"] if raw_layers else 0.0
            if h_val is None or h_val <= 0:
                h_val = round(z_val - prev_z, 4) if (z_val is not None and z_val > prev_z) else default_h
            if z_val is None:
                z_val = round(prev_z + h_val, 4)
            raw_layers.append({
                "layer": layer_num if layer_num is not None else idx,
                "z": round(z_val, 4),
                "height": round(h_val, 4),
                "line": start_line,
            })
            i = j
            continue

        ml = _LAYER_NUM_RE.match(stripped)
        if ml:
            layer_num = _as_int(ml.group(1))
            if not raw_layers or raw_layers[-1]["line"] < i - 10:
                idx = len(raw_layers)
                prev_z = raw_layers[-1]["z"] if raw_layers else 0.0
                z_val = None
                for j in range(i + 1, min(i + 25, n)):
                    s = lines[j].strip()
                    if _G_MOVE_RE.match(s):
                        mgz = _Z_PARAM_RE.search(s)
                        if mgz:
                            z_val = _as_float(mgz.group(1))
                            break
                if z_val is None:
                    z_val = round(prev_z + default_h, 4)
                h_val = round(z_val - prev_z, 4) if z_val > prev_z else default_h
                raw_layers.append({
                    "layer": layer_num if layer_num is not None else idx,
                    "z": round(z_val, 4),
                    "height": round(h_val, 4),
                    "line": i,
                })
        i += 1

    return raw_layers


def rebase_gcode_to_bed(
    src: Path,
    dst: Path,
    *,
    start_layer: int | None = None,
    start_z: float | None = None,
) -> dict[str, Any]:
    """Create a new G-code file that prints from ``start_layer`` (or ``start_z``) on the bed.

    All layers from the chosen layer onwards are shifted down by
    ``target_z - target_height`` so the starting layer prints directly on the bed at
    ``Z = target_height``.
    """
    layers = inspect_gcode_layers(src)
    if len(layers) < 2:
        raise ValueError("G-code must contain at least 2 layers to split")

    target_idx: int | None = None
    if start_layer is not None:
        for idx, entry in enumerate(layers):
            if entry["layer"] == start_layer:
                target_idx = idx
                break
        if target_idx is None and 0 <= start_layer < len(layers):
            target_idx = start_layer
        if target_idx is None or target_idx <= 0 or target_idx >= len(layers):
            max_layer = layers[-1]["layer"]
            raise ValueError(f"start_layer must be between 1 and {max_layer}")
    elif start_z is not None:
        for idx, entry in enumerate(layers):
            if idx >= 1 and entry["z"] >= start_z - 1e-6:
                target_idx = idx
                break
        if target_idx is None:
            raise ValueError(
                f"start_z must be between {layers[1]['z']} and {layers[-1]['z']} mm"
            )
    else:
        raise ValueError("Either start_layer or start_z is required")

    target = layers[target_idx]
    chosen_layer = int(target["layer"])
    bed_z = round(float(target["height"]) if float(target["height"]) > 0 else 0.2, 4)
    original_z = round(float(target["z"]), 4)
    z_offset = round(original_z - bed_z, 4)
    remaining_layers = len(layers) - target_idx
    orig_max_z = max(float(entry["z"]) for entry in layers)
    new_max_z = round(max(bed_z, orig_max_z - z_offset), 4)

    lines = Path(src).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    first_layer_line = layers[0]["line"]
    target_line = target["line"]
    next_layer_line = layers[target_idx + 1]["line"] if (target_idx + 1) < len(layers) else len(lines)

    out: list[str] = []
    has_m106_off = False
    for line in lines[:first_layer_line]:
        if line.startswith("; total layer number:"):
            out.append(f"; total layer number: {remaining_layers}\n")
        elif line.startswith("; max_z_height:"):
            out.append(f"; max_z_height: {_fmt_num(new_max_z)}\n")
        else:
            if line.strip() == "M106 S0":
                has_m106_off = True
            out.append(line)
    if not has_m106_off:
        out.append("M106 S0\n")

    # Inspect the entry sequence of the target layer (up to the first positive extrusion move)
    entry_end = target_line
    start_x: str | None = None
    start_y: str | None = None
    preserved_entry_lines: list[str] = []
    has_before_layer_macro = False
    found_unretract_after_z = False
    saw_z_drop = False

    scan_limit = min(next_layer_line, target_line + 60)
    for idx in range(target_line, scan_limit):
        s = lines[idx].strip()
        if _LAYER_NUM_RE.match(s):
            has_before_layer_macro = True
            continue
        if s.startswith(("EXCLUDE_OBJECT_START", "; printing object", "M140 ", "M104 ", "M109 ", "M190 ")):
            preserved_entry_lines.append(lines[idx] if lines[idx].endswith("\n") else lines[idx] + "\n")
            continue
        if _G_MOVE_RE.match(s):
            mx = _X_PARAM_RE.search(s)
            my = _Y_PARAM_RE.search(s)
            me = _E_PARAM_RE.search(s)
            mz = _Z_PARAM_RE.search(s)
            e_val = _as_float(me.group(1)) if me else None
            z_val = _as_float(mz.group(1)) if mz else None

            if mx and my and (e_val is None or e_val <= 0):
                if e_val is None:
                    start_x, start_y = mx.group(1), my.group(1)
            if mz and not mx and not my and z_val is not None and abs(z_val - original_z) < 0.05:
                saw_z_drop = True
                continue
            if saw_z_drop and me and not mx and not my and e_val is not None and e_val > 0:
                entry_end = idx + 1
                found_unretract_after_z = True
                break
            if mx and my and e_val is not None and e_val > 0:
                entry_end = idx
                break

    if found_unretract_after_z and start_x is not None and start_y is not None:
        out.append(";LAYER_CHANGE\n")
        out.append(f";Z:{_fmt_num(bed_z)}\n")
        out.append(f";HEIGHT:{_fmt_num(bed_z)}\n")
        if has_before_layer_macro:
            out.append("BEFORE_LAYER_CHANGE LAYER=0\n")
        out.append("G1 E-1.2 F2400\n")
        out.append(";_SET_FAN_SPEED_CHANGING_LAYER\n")
        out.extend(preserved_entry_lines)
        out.append(f"G1 X{start_x} Y{start_y} F9000\n")
        out.append(f"G1 Z{_fmt_num(round(bed_z + 0.4, 4))}\n")
        out.append(f"G1 Z{_fmt_num(bed_z)}\n")
        out.append("G1 E1.2 F1800\n")
        body_start = entry_end
    else:
        body_start = target_line

    def _shift_z_match(m: re.Match[str]) -> str:
        val = _as_float(m.group(1))
        if val is None:
            return m.group(0)
        shifted = round(max(bed_z, val - z_offset), 4)
        return f"Z{_fmt_num(shifted)}"

    in_first_bed_layer = True
    in_executable = True
    last_suppressed_fan: str | None = None

    for idx in range(body_start, len(lines)):
        line = lines[idx]
        stripped = line.strip()

        if idx >= next_layer_line and in_first_bed_layer:
            if last_suppressed_fan is not None:
                out.append(last_suppressed_fan)
                last_suppressed_fan = None
            in_first_bed_layer = False

        if stripped in ("; EXECUTABLE_BLOCK_END", "PRINT_END"):
            if stripped == "PRINT_END":
                out.append(line)
                in_executable = False
                continue
            in_executable = False

        if not in_executable:
            if stripped.startswith("; total layers count ="):
                out.append(f"; total layers count = {remaining_layers}\n")
            else:
                out.append(line)
            continue

        ml = _LAYER_NUM_RE.match(stripped)
        if ml:
            old_num = _as_int(ml.group(1)) or chosen_layer
            new_num = max(0, old_num - chosen_layer)
            out.append(f"BEFORE_LAYER_CHANGE LAYER={new_num}\n")
            continue

        mz = _Z_COMMENT_RE.match(stripped)
        if mz:
            val = _as_float(mz.group(1))
            if val is not None:
                shifted = round(max(bed_z, val - z_offset), 4)
                out.append(f";Z:{_fmt_num(shifted)}\n")
                continue

        if in_first_bed_layer and stripped.startswith("M106 "):
            # If this M106 is at the very end of the first bed layer (immediately before
            # the next ;LAYER_CHANGE), keep it so the fan starts for the second layer.
            if idx + 1 >= next_layer_line:
                last_suppressed_fan = None
                out.append(line)
            else:
                last_suppressed_fan = line if line.endswith("\n") else line + "\n"
                out.append(f"; {stripped} ; suppressed on first bed layer\n")
            continue

        if _G_MOVE_RE.match(stripped) and "Z" in stripped:
            code_part, *comment_part = line.split(";", 1)
            code_shifted = _Z_PARAM_RE.sub(_shift_z_match, code_part)
            line = code_shifted + (";" + comment_part[0] if comment_part else "")

        out.append(line)

    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text("".join(out), encoding="utf-8")

    return {
        "start_layer": chosen_layer,
        "total_layers": len(layers),
        "remaining_layers": remaining_layers,
        "original_z": original_z,
        "bed_z": bed_z,
        "z_offset": z_offset,
        "max_z_height": new_max_z,
    }
