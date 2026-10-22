"""The bounding box of an uploaded STL or OBJ, in millimetres as authored.

A later module scales an oversize model down to the bed, and to pick that factor it has to
know how big the file is. This module only *measures*: no scaling, no orientation, no
preset work, no dependency beyond the standard library.

The one trap worth naming is STL's two encodings. Binary and ASCII are told apart by
arithmetic — the uint32 triangle count at offset 80 has to account for the file size
exactly — never by the leading keyword: real CAD exports write *binary* files whose
80-byte header happens to begin with ``solid``, and a keyword check silently mis-parses
them into nonsense. A length that does not match the declared triangle count is reported,
not guessed at.

Coordinates are read verbatim. The formats carry no unit, the portal treats them as
millimetres, so nothing here converts, rounds or transforms a number.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MEASURABLE_SUFFIXES = (".stl", ".obj")
"""Suffixes `model_bounding_box` can read; anything else is refused with `MeshError`."""

_HEADER_BYTES = 80            # STL header, immediately followed by the uint32 triangle count
_BINARY_RECORD = struct.Struct("<12fH")
"""One binary STL facet: normal (3 floats), three vertices (9 floats), attribute word."""

_THREEMF_MESSAGE = ("{name}: the portal reads STL and OBJ geometry only "
                    "(3MF geometry lives in 3D/3dmodel.model)")


class MeshError(RuntimeError):
    """The upload is not a mesh this module can measure."""


@dataclass(frozen=True, slots=True)
class BoundingBox:
    xmin: float
    ymin: float
    zmin: float
    xmax: float
    ymax: float
    zmax: float

    @property
    def size(self) -> tuple[float, float, float]:
        return (self.xmax - self.xmin, self.ymax - self.ymin, self.zmax - self.zmin)

    @property
    def is_degenerate(self) -> bool:
        """True for a flat or empty mesh — every span has to be positive and finite."""
        return any(span <= 0 or not math.isfinite(span) for span in self.size)


def _coords(tokens: list[str]) -> tuple[float, float, float] | None:
    """The first three tokens as floats, or None when the line is not a coordinate triad."""
    if len(tokens) < 3:
        return None
    try:
        return (float(tokens[0]), float(tokens[1]), float(tokens[2]))
    except ValueError:
        return None


def _bounding_box(
    name: str, points: Iterable[tuple[float, float, float]], noun: str
) -> BoundingBox:
    """Fold points into a box as they stream in, so a large mesh never lands in memory."""
    lo = [math.inf, math.inf, math.inf]
    hi = [-math.inf, -math.inf, -math.inf]
    seen = 0
    for point in points:
        if not all(math.isfinite(value) for value in point):
            raise MeshError(f"{name} has a non-finite {noun} vertex")
        for axis, value in enumerate(point):
            if value < lo[axis]:
                lo[axis] = value
            if value > hi[axis]:
                hi[axis] = value
        seen += 1
    if not seen:
        raise MeshError(f"{name} has no readable {noun} vertices")
    return BoundingBox(lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])


def _binary_triangle_count(data: bytes) -> int:
    """Triangle count when ``data`` is a binary STL, otherwise 0.

    The count only counts when the file is exactly as long as that many facets make it,
    which is what tells binary apart from ASCII without trusting the header text. Trailing
    bytes are tolerated by neither format here: a mismatch is a corrupt or truncated file,
    not something to measure.
    """
    if len(data) < _HEADER_BYTES + 4:
        return 0
    (count,) = struct.unpack_from("<I", data, _HEADER_BYTES)
    if count and len(data) == _HEADER_BYTES + 4 + _BINARY_RECORD.size * count:
        return count
    return 0


def _binary_points(data: bytes, count: int) -> Iterable[tuple[float, float, float]]:
    """The three vertices of each facet; floats [0:3] are the normal, never a vertex."""
    body = data[_HEADER_BYTES + 4:_HEADER_BYTES + 4 + _BINARY_RECORD.size * count]
    for record in struct.iter_unpack(_BINARY_RECORD.format, body):
        yield record[3:6]
        yield record[6:9]
        yield record[9:12]


def _ascii_stl_points(data: bytes) -> list[tuple[float, float, float]]:
    """``vertex x y z`` lines only — ``facet normal`` carries three floats too."""
    points = []
    for line in data.decode(errors="replace").splitlines():
        parts = line.split()
        if not parts or parts[0] != "vertex":
            continue
        coords = _coords(parts[1:4])
        if coords is not None:
            points.append(coords)
    return points


def _stl_no_vertices_message(name: str, data: bytes) -> str:
    """Name the truncation when the bytes look like a short binary file."""
    count = struct.unpack_from("<I", data, _HEADER_BYTES)[0] if len(data) >= _HEADER_BYTES + 4 else 0
    expected = _HEADER_BYTES + 4 + _BINARY_RECORD.size * count
    if count and len(data) < expected:
        return (f"{name} declares {count} binary STL triangles ({expected} bytes) but holds "
                f"{len(data)} bytes: the file is truncated")
    return f"{name} has no readable STL vertices"


def parse_stl(data: bytes, *, name: str = "<stl>") -> BoundingBox:
    """Measure an STL given as bytes. ``name`` is only used in error messages."""
    count = _binary_triangle_count(data)
    if count:
        return _bounding_box(name, _binary_points(data, count), "STL")
    points = _ascii_stl_points(data)
    if not points:
        raise MeshError(_stl_no_vertices_message(name, data))
    return _bounding_box(name, points, "STL")


def parse_obj(data: bytes, *, name: str = "<obj>") -> BoundingBox:
    """Measure an OBJ given as bytes. ``name`` is only used in error messages."""
    points = []
    for line in data.decode(errors="replace").splitlines():
        parts = line.split()
        if not parts or parts[0] != "v":
            continue
        coords = _coords(parts[1:4])
        if coords is not None:
            points.append(coords)
    return _bounding_box(name, points, "OBJ")


def model_bounding_box(path: Path) -> BoundingBox:
    """Box of the model at ``path``, chosen by suffix. ``MeshError`` if it is not measurable."""
    suffix = path.suffix.lower()
    if suffix not in MEASURABLE_SUFFIXES:
        raise MeshError(_THREEMF_MESSAGE.format(name=path.name))
    data = path.read_bytes()
    if suffix == ".obj":
        return parse_obj(data, name=path.name)
    return parse_stl(data, name=path.name)
