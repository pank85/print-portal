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
