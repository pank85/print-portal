"""mesh.py: the bounding box of an STL or OBJ, and the formats it refuses.

The binary/ASCII STL split is the reason this file exists. `test_binary_stl_header_starting
_with_solid_is_parsed_as_binary` is the regression that a keyword check ("does it start with
``solid``?") would fail on a real CAD export, and
`test_binary_stl_is_detected_by_size_not_keyword` pins the positive half of the same rule:
the header text is irrelevant, only the length arithmetic decides.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

import mesh

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# a triangle whose nine coordinates are all exactly representable in float32, so the parsed
# box can be compared for equality instead of with a tolerance
TRIANGLE = ((1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.5, 9.25))


def binary_stl(triangles, header: bytes = b"") -> bytes:
    """A binary STL: 80-byte header, uint32 facet count, then 50-byte facet records."""
    body = b"".join(
        struct.pack("<12fH", 0.0, 0.0, 0.0, *a, *b, *c, 0) for a, b, c in triangles
    )
    return header.ljust(80, b"\0")[:80] + struct.pack("<I", len(triangles)) + body


def ascii_stl(triangles, normal: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> bytes:
    lines = ["solid parts"]
    for a, b, c in triangles:
        lines.append("  facet normal %g %g %g" % normal)
        lines.append("    outer loop")
        for vertex in (a, b, c):
            lines.append("      vertex %g %g %g" % vertex)
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid parts")
    return ("\n".join(lines) + "\n").encode()


def test_ascii_stl_box_is_exact() -> None:
    box = mesh.model_bounding_box(FIXTURES / "cube.stl")
    assert box.size == (20.0, 20.0, 20.0)
    assert box.is_degenerate is False


def test_binary_stl_header_starting_with_solid_is_parsed_as_binary() -> None:
    data = binary_stl([TRIANGLE], header=b"solid exported-by-some-cad")
    assert data.startswith(b"solid")
    box = mesh.parse_stl(data)
    assert box == mesh.BoundingBox(1.0, 2.0, 3.0, 7.0, 8.5, 9.25)
    assert box.size == (6.0, 6.5, 6.25)


def test_binary_stl_is_detected_by_size_not_keyword() -> None:
    with_solid = binary_stl([TRIANGLE], header=b"solid exported-by-some-cad")
    without_solid = binary_stl([TRIANGLE], header=b"parts")
    assert b"solid" not in without_solid
    assert mesh.parse_stl(without_solid) == mesh.parse_stl(with_solid)
    assert mesh.parse_stl(without_solid).size == (6.0, 6.5, 6.25)


def test_truncated_binary_stl_raises() -> None:
    valid = binary_stl([TRIANGLE, TRIANGLE])
    with pytest.raises(mesh.MeshError):
        mesh.parse_stl(valid[:-10])


def test_ascii_facet_normals_are_not_vertices() -> None:
    data = ascii_stl([TRIANGLE], normal=(999.0, 999.0, 999.0))
    assert b"999" in data
    assert mesh.parse_stl(data).size == (6.0, 6.5, 6.25)


def test_obj_vertices() -> None:
    obj = b"\n".join([
        b"# a wedge, with the noise a real export carries",
        b"o wedge",
        b"v 0 0 0",
        b"v 10 0 0",
        b"v 10 20 0",
        b"v 10 20 30",
        b"vn 100 100 100",
        b"vt 0.5 0.5",
        b"f 1//1 2//1 3//1 4//1",
        b"",
    ])
    assert mesh.parse_obj(obj) == mesh.BoundingBox(0.0, 0.0, 0.0, 10.0, 20.0, 30.0)


def test_3mf_is_refused_with_a_message() -> None:
    with pytest.raises(mesh.MeshError) as excinfo:
        mesh.model_bounding_box(Path("model.3mf"))
    assert "3D/3dmodel.model" in str(excinfo.value)


def test_empty_raises_and_a_zero_area_triangle_is_degenerate_not_an_error() -> None:
    with pytest.raises(mesh.MeshError):
        mesh.parse_stl(b"")

    box = mesh.parse_stl(ascii_stl([((0.0, 0.0, 0.0),) * 3]))
    assert box.size == (0.0, 0.0, 0.0)
    assert box.is_degenerate is True


def test_units_are_not_converted() -> None:
    corners = [
        (x, y, z)
        for x in (0.0, 100.0)
        for y in (0.0, 100.0)
        for z in (0.0, 100.0)
    ]
    obj = "\n".join("v %g %g %g" % corner for corner in corners) + "\n"
    box = mesh.parse_obj(obj.encode())
    assert box.size == (100.0, 100.0, 100.0)
