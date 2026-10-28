"""Triangle soup for the local, pre-slice 3-D preview.

The portal shows the finned result *before* the slicer runs, which means the server has to
hand the browser geometry. ``app/mesh.py`` only measures a bounding box; this module reads
actual vertices and triangles, and it reads them itself so the two jobs stay separable:

* **3MF** — ``3D/3dmodel.model`` inside the zip: ``<resources>`` meshes plus the ``<build>``
  items that place them. A build item usually points at an object made of ``<components>``,
  so the tree is flattened to one leaf per build item, composing each 3MF affine
  ``transform`` (a row-major 3x4 matrix acting on row vectors). Flattening is capped at
  ``MAX_COMPONENT_DEPTH`` levels and guarded against cycles: a hostile or broken project
  must not make the request hang or blow up.
* **STL** — binary and ASCII are told apart by arithmetic, never by the leading ``solid``
  keyword: the uint32 triangle count at offset 80 has to account for the file length
  exactly (the same rule documented in ``app/mesh.py:90-113``). Binary files whose header
  happens to start with ``solid`` are common CAD exports.
* **OBJ** — ``v`` and ``f`` records, with ``f`` indices in any of ``i``, ``i/j``, ``i//k``,
  ``i/j/k`` form, negative (relative) indices allowed, polygons fan-triangulated.

Everything returned is plain JSON-ready data: flat float/integer lists, coordinates rounded
to 3 decimal places (a micrometre — more than the renderer shows and a third of the
payload), one object per part so the UI can colour and toggle parts and supports
separately. Two caps keep the response bounded: ``MAX_BYTES`` on the input file and
``MAX_TRIANGLES`` on the scene, both reported with the actual number in
:class:`PreviewError`.

Role assignment (``"supports"`` vs ``"part"``) is a heuristic for colouring only:
an object whose name contains ``fin``/``support`` is supports, and so is an object the
file's own slicer settings document (``Metadata/model_settings.config``) does not
describe — that is how a slicer project distinguishes a real part from a support mesh
bolted onto it. A plain 3MF/STL/OBJ with no settings document has nothing to compare
against, so its objects are parts.
"""
from __future__ import annotations

import lzma
import math
import os
import tempfile
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

MAX_TRIANGLES = 450_000
"""Cap on the triangles in one preview response."""

MAX_BYTES = 32 * 1024 * 1024
"""Cap on the input file: a bigger archive is refused before it is unpacked."""

MAX_EXPANDED_MEMBER = 64 * 1024 * 1024
MAX_EXPANDED_TOTAL = 128 * 1024 * 1024
MAX_COORDINATE_MM = 1_000_000_000

_UNIT_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0,
            "inch": 25.4, "foot": 304.8, "meter": 1000.0}

MAX_COMPONENT_DEPTH = 8
"""Cap on nested 3MF ``<components>``; deeper trees are refused, not flattened."""

MODEL_MEMBER = "3D/3dmodel.model"
MODEL_SETTINGS_MEMBER = "Metadata/model_settings.config"

CORE_NAMESPACE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
DEFAULT_UNIT = "millimeter"

_HEADER_BYTES = 80            # STL header, immediately followed by the uint32 triangle count
_BINARY_RECORD = struct.Struct("<12fH")
"""One binary STL facet: normal (3 floats), three vertices (9 floats), attribute word."""

_IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)

# bbox.size[i] is the span of transformed vertex component i.
# Axis map: width=bbox[0], length=bbox[1], height=bbox[2].
SCALE_AXES = {"x": 0, "y": 1, "z": 2}

_SUPPORT_WORDS = ("fin", "support")

_SUPPORTED_SUFFIXES = (".3mf", ".stl", ".obj")


class PreviewError(RuntimeError):
    """The preview cannot be built; the message names the file and the reason."""


# --------------------------------------------------------------------------- scene


def read_scene(path: Path) -> dict:
    """Read the geometry at ``path`` as ``{unit, bbox, objects}``.

    Raises :class:`PreviewError` for an unsupported suffix, an unreadable or oversized
    file, a malformed mesh, or a scene over the triangle cap.
    """
    suffix = path.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise PreviewError(f"{path.name}: the preview reads .3mf, .stl and .obj geometry only")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PreviewError(f"{path.name}: cannot read the file ({exc.strerror})") from exc
    if size > MAX_BYTES:
        raise PreviewError(
            f"{path.name} is {size} bytes; the preview cap is {MAX_BYTES} bytes")

    if suffix == ".3mf":
        unit, leaves = _read_3mf(path)
    elif suffix == ".stl":
        unit, leaves = DEFAULT_UNIT, _read_stl(path)
    else:
        unit, leaves = DEFAULT_UNIT, _read_obj(path)
    return _scene(path.name, unit, leaves)


def _scene(name: str, unit: str, leaves: list[dict]) -> dict:
    """Assemble the response: rounded vertices, a combined box, objects by size."""
    objects = []
    lo = [math.inf, math.inf, math.inf]
    hi = [-math.inf, -math.inf, -math.inf]
    total = 0
    for index, leaf in enumerate(leaves, start=1):
        vertices: list[float] = []
        for x, y, z in leaf["vertices"]:
            if not all(math.isfinite(v) and abs(v) <= MAX_COORDINATE_MM for v in (x, y, z)):
                raise PreviewError(f"{name} has non-finite or out-of-range coordinates")
            point = (_round(x), _round(y), _round(z))
            vertices.extend(point)
            for axis, value in enumerate(point):
                if value < lo[axis]:
                    lo[axis] = value
                if value > hi[axis]:
                    hi[axis] = value
        triangles = leaf["triangles"]
        total += len(triangles) // 3
        objects.append({
            "id": index,
            "name": leaf["name"],
            "role": leaf["role"],
            "vertices": vertices,
            "triangles": triangles,
            "count": len(triangles) // 3,
        })
    if not total:
        raise PreviewError(f"{name} has no triangles to preview")
    objects.sort(key=lambda obj: obj["count"], reverse=True)
    return {
        "unit": unit,
        "bbox": {
            "min": [float(value) for value in lo],
            "max": [float(value) for value in hi],
            "size": [float(hi[axis] - lo[axis]) for axis in range(3)],
        },
        "objects": objects,
    }


def _round(value: float) -> float:
    """3 decimal places, with ``-0.0`` folded to ``0.0`` so JSON stays clean."""
    rounded = round(value, 3)
    return 0.0 if rounded == 0 else rounded


class _Budget:
    """Running triangle count for one scene; raising as soon as the cap is passed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.triangles = 0

    def add(self, count: int) -> None:
        self.triangles += count
        if self.triangles > MAX_TRIANGLES:
            raise PreviewError(
                f"{self.name} has at least {self.triangles} triangles; "
                f"the preview cap is {MAX_TRIANGLES} triangles")


# ----------------------------------------------------------------------------- 3MF


def _local(tag: str) -> str:
    """Element tag without its XML namespace (``{ns}object`` -> ``object``)."""
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == name]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _floats(raw: str | None) -> list[float] | None:
    """Whitespace-separated floats, or None when the attribute is absent/malformed."""
    if not raw:
        return None
    try:
        return [float(token) for token in raw.split()]
    except ValueError:
        return None


def _transform(raw: str | None, *, name: str) -> tuple[float, ...]:
    """A 3MF affine transform as 12 floats (row-major, row-vector convention)."""
    if not raw or not raw.strip():
        return _IDENTITY
    values = _floats(raw)
    if values is None or len(values) not in (9, 12) or not all(math.isfinite(v) for v in values):
        raise PreviewError(f"{name} has a malformed 3MF transform ({raw!r})")
    if len(values) == 9:  # a bare 3x3 rotation/scale writers sometimes emit
        return (*values, 0.0, 0.0, 0.0)
    return tuple(values)


def _compose(inner: tuple[float, ...], outer: tuple[float, ...]) -> tuple[float, ...]:
    """Matrix of ``v -> v·inner·outer``: a child transform applied, then its parent's."""
    out = [
        inner[row * 3] * outer[col]
        + inner[row * 3 + 1] * outer[3 + col]
        + inner[row * 3 + 2] * outer[6 + col]
        for row in range(3)
        for col in range(3)
    ]
    tx, ty, tz = inner[9], inner[10], inner[11]
    out.extend([
        tx * outer[0] + ty * outer[3] + tz * outer[6] + outer[9],
        tx * outer[1] + ty * outer[4] + tz * outer[7] + outer[10],
        tx * outer[2] + ty * outer[5] + tz * outer[8] + outer[11],
    ])
    return tuple(out)


def _apply(matrix: tuple[float, ...], point: tuple[float, float, float]) \
        -> tuple[float, float, float]:
    x, y, z = point
    return (
        x * matrix[0] + y * matrix[3] + z * matrix[6] + matrix[9],
        x * matrix[1] + y * matrix[4] + z * matrix[7] + matrix[10],
        x * matrix[2] + y * matrix[5] + z * matrix[8] + matrix[11],
    )


class _Object:
    """One ``<object>`` of a 3MF ``<resources>`` block."""

    __slots__ = ("id", "name", "vertices", "triangles", "components")

    def __init__(self, object_id: int, name: str) -> None:
        self.id = object_id
        self.name = name
        self.vertices: list[tuple[float, float, float]] | None = None
        self.triangles: list[int] = []
        self.components: list[tuple[int, tuple[float, ...]]] = []


def _parse_mesh(element: ET.Element, name: str) -> tuple[list[tuple[float, float, float]],
                                                         list[int]]:
    """``<mesh>`` -> (vertices, flat triangle indices)."""
    vertices: list[tuple[float, float, float]] = []
    raw_vertices = _child(element, "vertices")
    for vertex in (raw_vertices if raw_vertices is not None else []):
        try:
            x = float(vertex.get("x", ""))
            y = float(vertex.get("y", ""))
            z = float(vertex.get("z", ""))
        except (TypeError, ValueError):
            raise PreviewError(f"{name} has a 3MF vertex without usable coordinates") from None
        if not all(math.isfinite(value) for value in (x, y, z)):
            raise PreviewError(f"{name} has a non-finite 3MF vertex")
        vertices.append((x, y, z))
    triangles: list[int] = []
    raw_triangles = _child(element, "triangles")
    for triangle in (raw_triangles if raw_triangles is not None else []):
        try:
            corners = (int(triangle.get("v1", "")), int(triangle.get("v2", "")),
                       int(triangle.get("v3", "")))
        except (TypeError, ValueError):
            raise PreviewError(f"{name} has a 3MF triangle without usable indices") from None
        for index in corners:
            if not 0 <= index < len(vertices):
                raise PreviewError(
                    f"{name} has a 3MF triangle index ({index}) outside its "
                    f"{len(vertices)} vertices")
        triangles.extend(corners)
    if not vertices:
        raise PreviewError(f"{name} has a 3MF object without vertices")
    if not triangles:
        raise PreviewError(f"{name} has a 3MF object without triangles")
    return vertices, triangles


def _read_objects(root: ET.Element, name: str) -> tuple[dict[int, _Object], list[ET.Element]]:
    resources = _child(root, "resources")
    if resources is None:
        raise PreviewError(f"{name} has no 3MF resources")
    objects: dict[int, _Object] = {}
    for element in _children(resources, "object"):
        raw_id = element.get("id")
        try:
            object_id = int(raw_id if raw_id is not None else "")
        except ValueError:
            raise PreviewError(f"{name} has a 3MF object without a numeric id") from None
        obj = _Object(object_id, (element.get("name") or "").strip())
        mesh = _child(element, "mesh")
        if mesh is not None:
            obj.vertices, obj.triangles = _parse_mesh(mesh, name)
        for component in _children(element, "components"):
            for link in _children(component, "component"):
                raw_target = link.get("objectid")
                try:
                    target = int(raw_target if raw_target is not None else "")
                except ValueError:
                    raise PreviewError(
                        f"{name} has a 3MF component without a numeric objectid") from None
                obj.components.append((target, _transform(link.get("transform"), name=name)))
        if obj.vertices is None and not obj.components:
            raise PreviewError(f"{name} has a 3MF object with neither a mesh nor components")
        objects[object_id] = obj
    build = _child(root, "build")
    items = _children(build, "item") if build is not None else []
    if not items:
        raise PreviewError(f"{name} has no 3MF build items")
    return objects, items


def _settings_object_ids(data: bytes | None) -> set[int]:
    """Object ids in an optional slicer settings document."""
    if data is None:
        return set()
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return set()
    ids: set[int] = set()
    for element in root.iter():
        if _local(element.tag) in ("object", "part"):
            raw = (element.get("id") or "").strip()
            if raw.lstrip("+-").isdigit():
                ids.add(int(raw))
    return ids


def _archive_members(path: Path) -> list[tuple[zipfile.ZipInfo, bytes]]:
    """Read every member with declared and streamed expansion limits."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if sum(info.filename == MODEL_MEMBER for info in infos) != 1:
                raise PreviewError(f"{path.name} has no 3MF model part ({MODEL_MEMBER}) or has duplicate model parts")
            if any(info.file_size > MAX_EXPANDED_MEMBER for info in infos) or sum(
                    info.file_size for info in infos) > MAX_EXPANDED_TOTAL:
                raise PreviewError(f"{path.name} exceeds the 3MF expanded data limit")
            total = 0
            members = []
            for info in infos:
                chunks = []
                count = 0
                with archive.open(info) as source:
                    while chunk := source.read(65536):
                        count += len(chunk)
                        total += len(chunk)
                        if count > MAX_EXPANDED_MEMBER or total > MAX_EXPANDED_TOTAL:
                            raise PreviewError(f"{path.name} exceeds the 3MF expanded data limit")
                        chunks.append(chunk)
                members.append((info, b"".join(chunks)))
            return members
    except PreviewError:
        raise
    except (zipfile.BadZipFile, zlib.error, lzma.LZMAError, OSError, EOFError, RuntimeError, NotImplementedError) as exc:
        raise PreviewError(f"{path.name} is not a readable 3MF archive") from exc


def _role(obj: _Object, settings_ids: set[int]) -> str:
    """``"supports"`` for a fin/support-named object or one its slicer settings omit."""
    if any(word in obj.name.lower() for word in _SUPPORT_WORDS):
        return "supports"
    if settings_ids and obj.id not in settings_ids:
        return "supports"
    return "part"


def _read_3mf(path: Path) -> tuple[str, list[dict]]:
    """``(unit, leaves)`` from a 3MF: one leaf per build item, components flattened."""
    members = _archive_members(path)
    raw = next(data for info, data in members if info.filename == MODEL_MEMBER)
    settings_ids = _settings_object_ids(next(
        (data for info, data in members if info.filename == MODEL_SETTINGS_MEMBER), None))

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise PreviewError(f"{path.name} has a malformed 3MF model part ({exc})") from exc

    unit = (root.get("unit") or DEFAULT_UNIT).strip() or DEFAULT_UNIT
    if unit not in _UNIT_MM:
        raise PreviewError(f"{path.name} has unsupported 3MF unit {unit!r}")
    for element in root.iter():
        if _local(element.tag) in ("component", "item") and any(
                _local(attr) == "path" for attr in element.attrib):
            raise PreviewError(f"{path.name} has unsupported external 3MF references")
    objects, items = _read_objects(root, path.name)
    budget = _Budget(path.name)
    leaves: list[dict] = []
    for item in items:
        raw_target = item.get("objectid")
        try:
            target = int(raw_target if raw_target is not None else "")
        except ValueError:
            raise PreviewError(f"{path.name} has a 3MF build item without a numeric objectid") \
                from None
        if target not in objects:
            raise PreviewError(
                f"{path.name} has a 3MF build item naming object {target}, which is not defined")
        _flatten(path.name, objects, target, _transform(item.get("transform"), name=path.name),
                 (), budget, settings_ids, leaves)
    multiplier = _UNIT_MM[unit]
    for leaf in leaves:
        leaf["vertices"] = [tuple(v * multiplier for v in point) for point in leaf["vertices"]]
    return DEFAULT_UNIT, leaves


def _flatten(name: str, objects: dict[int, _Object], object_id: int,
             transform: tuple[float, ...], stack: tuple[int, ...], budget: _Budget,
             settings_ids: set[int], out: list[dict]) -> None:
    """Append one leaf per mesh reached from ``object_id``, composing ``transform``."""
    if len(stack) > MAX_COMPONENT_DEPTH:
        raise PreviewError(
            f"{name} nests 3MF components deeper than {MAX_COMPONENT_DEPTH} levels")
    if object_id in stack:
        raise PreviewError(f"{name} has a cyclic 3MF component reference (object {object_id})")
    obj = objects[object_id]
    if obj.vertices is not None:
        budget.add(len(obj.triangles) // 3)
        out.append({
            "name": obj.name or f"object {object_id}",
            "role": _role(obj, settings_ids),
            "vertices": [_apply(transform, point) for point in obj.vertices],
            "triangles": list(obj.triangles),
        })
        return
    for target, local in obj.components:
        if target not in objects:
            raise PreviewError(
                f"{name} has a 3MF component naming object {target}, which is not defined")
        _flatten(name, objects, target, _compose(local, transform), (*stack, object_id),
                 budget, settings_ids, out)


# ----------------------------------------------------------------------------- STL


def _binary_triangle_count(data: bytes) -> int:
    """Triangle count when ``data`` is a binary STL, otherwise 0.

    The declared count only counts when the file is exactly as long as that many facets
    make it — the rule from ``app/mesh.py:90-113``: never trust the ``solid`` keyword,
    binary exports routinely start with it.
    """
    if len(data) < _HEADER_BYTES + 4:
        return 0
    (count,) = struct.unpack_from("<I", data, _HEADER_BYTES)
    if count and len(data) == _HEADER_BYTES + 4 + _BINARY_RECORD.size * count:
        return count
    return 0


def _stl_points(name: str, data: bytes) -> list[tuple[float, float, float]]:
    """Three points per facet, binary body first and ASCII ``vertex`` lines second."""
    count = _binary_triangle_count(data)
    if count:
        body = data[_HEADER_BYTES + 4:_HEADER_BYTES + 4 + _BINARY_RECORD.size * count]
        points: list[tuple[float, float, float]] = []
        for record in struct.iter_unpack(_BINARY_RECORD.format, body):
            points.append(record[3:6])   # floats [0:3] are the normal, never a vertex
            points.append(record[6:9])
            points.append(record[9:12])
        return points
    points = []
    for line in data.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] != "vertex":
            continue
        try:
            points.append((float(parts[1]), float(parts[2]), float(parts[3])))
        except ValueError:
            continue
    if not points:
        raise PreviewError(_stl_no_vertices(name, data))
    return points


def _stl_no_vertices(name: str, data: bytes) -> str:
    """Name the truncation when the bytes look like a short binary file."""
    count = (struct.unpack_from("<I", data, _HEADER_BYTES)[0]
             if len(data) >= _HEADER_BYTES + 4 else 0)
    expected = _HEADER_BYTES + 4 + _BINARY_RECORD.size * count
    if count and len(data) < expected:
        return (f"{name} declares {count} binary STL triangles ({expected} bytes) but holds "
                f"{len(data)} bytes: the file is truncated")
    return f"{name} has no readable STL vertices"


# ----------------------------------------------------------------------------- OBJ


def _read_obj(path: Path) -> list[dict]:
    """One ``part`` object built from ``v``/``f`` records, polygons fan-triangulated."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PreviewError(f"{path.name}: cannot read the file ({exc.strerror})") from exc
    points: list[tuple[float, float, float]] = []
    faces: list[list[int]] = []
    for line in data.decode(errors="replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v":
            if len(parts) < 4:
                continue
            try:
                point = (float(parts[1]), float(parts[2]), float(parts[3]))
            except ValueError:
                continue
            points.append(point)
            continue
        if parts[0] != "f":
            continue
        corners: list[int] = []
        for token in parts[1:]:
            raw = token.split("/", 1)[0]
            try:
                index = int(raw)
            except ValueError:
                corners = []
                break
            # OBJ is 1-based; a negative index counts back from the newest vertex
            index = index - 1 if index > 0 else len(points) + index
            if not 0 <= index < len(points):
                raise PreviewError(
                    f"{path.name} has an OBJ face index ({token}) outside its "
                    f"{len(points)} vertices")
            corners.append(index)
        if len(corners) >= 3:
            faces.append(corners)
    if not points:
        raise PreviewError(f"{path.name} has no readable OBJ vertices")
    if not faces:
        raise PreviewError(f"{path.name} has no readable OBJ faces")
    budget = _Budget(path.name)
    triangles: list[int] = []
    for corners in faces:
        budget.add(len(corners) - 2)
        for corner in range(1, len(corners) - 1):
            triangles.extend((corners[0], corners[corner], corners[corner + 1]))
    return [{
        "name": path.stem,
        "role": "part",
        "vertices": points,
        "triangles": triangles,
    }]


def _read_stl(path: Path) -> list[dict]:
    """One ``part`` object built from binary or ASCII STL facets, vertices deduplicated."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PreviewError(f"{path.name}: cannot read the file ({exc.strerror})") from exc
    points = _stl_points(path.name, data)
    budget = _Budget(path.name)
    vertices: list[tuple[float, float, float]] = []
    triangles: list[int] = []
    index: dict[tuple[float, float, float], int] = {}
    for start in range(0, len(points) - 2, 3):
        budget.add(1)
        corners = []
        for point in points[start:start + 3]:
            x, y, z = (float(point[0]), float(point[1]), float(point[2]))
            if not all(math.isfinite(value) for value in (x, y, z)):
                raise PreviewError(f"{path.name} has a non-finite STL vertex")
            key = (_round(x), _round(y), _round(z))
            found = index.get(key)
            if found is None:
                found = len(vertices)
                index[key] = found
                vertices.append(key)
            corners.append(found)
        triangles.extend(corners)
    if not triangles:
        raise PreviewError(f"{path.name} has no readable STL triangles")
    return [{
        "name": path.stem,
        "role": "part",
        "vertices": vertices,
        "triangles": triangles,
    }]


def _scale_number(raw: str, factor: float, *, name: str) -> str:
    try:
        value = float(raw) * factor
    except ValueError:
        raise PreviewError(f"{name} has a non-numeric coordinate ({raw!r})") from None
    if not math.isfinite(value):
        raise PreviewError(f"{name} has a non-finite coordinate")
    if abs(value) > MAX_COORDINATE_MM:
        raise PreviewError(f"{name} has an out-of-range coordinate")
    return format(value, ".17g")


def _scale_model_xml(data: bytes, factor: float, name: str) -> bytes:
    """Scale vertices and transform translations, leaving linear terms unchanged."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise PreviewError(f"{name} has a malformed 3MF model part ({exc})") from exc
    for element in root.iter():
        local = _local(element.tag)
        if local == "vertex":
            for attr in ("x", "y", "z"):
                raw = element.get(attr)
                if raw is None:
                    continue
                element.set(attr, _scale_number(raw, factor, name=name))
        elif local in ("component", "item"):
            raw = element.get("transform")
            if not raw or not raw.strip():
                continue
            tokens = raw.split()
            if len(tokens) not in (9, 12):
                raise PreviewError(f"{name} has a malformed 3MF transform ({raw!r})")
            _transform(raw, name=name)
            element.set("transform", " ".join(tokens[:9] + [
                _scale_number(token, factor, name=name) for token in tokens[9:]]))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _scale_3mf(path: Path, factor: float, out_path: Path) -> None:
    """Rewrite the model XML; every other zip member is copied byte-for-byte."""
    members = _archive_members(path)
    scaled = []
    for info, data in members:
        if info.filename == MODEL_MEMBER:
            data = _scale_model_xml(data, factor, path.name)
        scaled.append((info, data))
    with zipfile.ZipFile(out_path, "w") as dst:
        for info, data in scaled:
            cloned = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
            cloned.compress_type = info.compress_type
            cloned.external_attr = info.external_attr
            cloned.create_system = info.create_system
            dst.writestr(cloned, data)


def geometry_capability(path: Path) -> dict:
    """Return bounded preview/resize capability and millimetre dimensions, or a reason.

    Zero-size axes cannot be used as resize inputs; other axes may still be usable.
    This probe does not determine whether a slicer can consume the file.
    """
    try:
        scene = read_scene(path)
    except PreviewError as exc:
        return {"preview_available": False, "resize_available": False, "reason": str(exc),
                "unit": "millimeter", "size": None}
    size = scene["bbox"]["size"]
    available = any(value > 0 for value in size)
    return {"preview_available": True, "resize_available": available,
            "reason": None if available else "geometry has zero extent",
            "unit": "millimeter", "size": size}


def scale_file(path: Path, factor: float, out_path: Path) -> None:
    """Write validated uniformly scaled geometry atomically, preserving input on failure."""
    if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0:
        raise PreviewError(f"{path.name}: scale factor must be finite and positive")
    read_scene(path)  # Validate the complete input and enforce all resource limits first.
    suffix = path.suffix.lower()
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=out_path.parent, suffix=suffix, delete=False) as f:
            tmp = Path(f.name)
        if suffix == ".3mf":
            _scale_3mf(path, factor, tmp)
        elif suffix == ".stl":
            raw = path.read_bytes()
            count = _binary_triangle_count(raw)
            if count:
                out = bytearray(raw)
                for i in range(count):
                    for j in range(3, 12):
                        offset = 84 + i * 50 + j * 4
                        value = float(_scale_number(str(struct.unpack_from("<f", raw, offset)[0]), factor, name=path.name))
                        try:
                            struct.pack_into("<f", out, offset, value)
                        except (OverflowError, struct.error) as exc:
                            raise PreviewError(f"{path.name} has an out-of-range coordinate") from exc
                tmp.write_bytes(out)
            else:
                lines = raw.decode("utf-8", errors="strict").splitlines(keepends=True)
                result = []
                for line in lines:
                    parts = line.split()
                    if parts and parts[0] == "vertex":
                        if len(parts) != 4:
                            raise PreviewError(f"{path.name} has a malformed STL vertex")
                        line = "vertex " + " ".join(_scale_number(x, factor, name=path.name) for x in parts[1:]) + "\n"
                    result.append(line)
                tmp.write_text("".join(result))
        elif suffix == ".obj":
            lines = path.read_text().splitlines(keepends=True)
            result = []
            for line in lines:
                parts = line.split()
                if parts and parts[0] == "v":
                    if len(parts) < 4:
                        raise PreviewError(f"{path.name} has a malformed OBJ vertex")
                    line = "v " + " ".join(_scale_number(x, factor, name=path.name) for x in parts[1:4])
                    line += (" " + " ".join(parts[4:]) if parts[4:] else "") + "\n"
                result.append(line)
            tmp.write_text("".join(result))
        else:
            raise PreviewError(f"{path.name}: cannot scale this file type")
        read_scene(tmp)
        os.replace(tmp, out_path)
        tmp = None
    except (OSError, UnicodeError, ValueError) as exc:
        raise PreviewError(f"{path.name}: cannot scale geometry ({exc})") from exc
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
