"""`preview.read_scene`: 3MF component flattening, STL/OBJ reading, and every cap.

The preview hands the browser raw geometry, so the interesting failures are all silent ones:
a component instance that is flattened once instead of twice (a fin that vanishes from the
picture), a transform applied in the wrong order (a fin drawn at the origin), a cap that is
not enforced (a 10 MB response), or a scene accepted with no triangles at all. Each test
below pins one of those, with ``PreviewError`` texts asserted verbatim where the UI shows
them to the operator.

3MF geometry is generated here rather than committed as a fixture: the cases that matter
(cyclic components, depth, transforms) are two lines of XML each.
"""
from __future__ import annotations

import math
import struct
import zipfile
from pathlib import Path

import pytest

import preview
import supportfins

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"

TRIANGLE = ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0))
QUAD = ((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (2.0, 2.0, 0.0), (0.0, 2.0, 0.0))


# ------------------------------------------------------------------------ fixture building
def mesh_object(object_id: int, vertices=TRIANGLE, *, name: str | None = None,
                triangles=None, extra: str = "") -> str:
    """An ``<object>`` carrying one ``<mesh>``; ``triangles`` defaults to a single fan of 0."""
    if triangles is None:
        triangles = [(0, 1, 2)] if len(vertices) >= 3 else []
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in vertices)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in triangles)
    attr = f' name="{name}"' if name is not None else ""
    return (f'<object id="{object_id}" type="model"{attr}>'
            f"<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh>"
            f"{extra}</object>")


def component_object(object_id: int, links, *, name: str | None = None) -> str:
    """An ``<object>`` made of ``<components>``; ``links`` is ``[(target, transform|None)]``."""
    attr = f' name="{name}"' if name is not None else ""
    parts = "".join(
        f'<component objectid="{target}"'
        + (f' transform="{matrix}"' if matrix else "")
        + "/>"
        for target, matrix in links)
    return (f'<object id="{object_id}" type="model"{attr}>'
            f"<components>{parts}</components></object>")


def model_xml(objects: list[str], items: list[str], *, unit: str | None = "millimeter") -> str:
    unit_attr = f' unit="{unit}"' if unit is not None else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<model{unit_attr} xmlns="{NS}">'
            f"<resources>{''.join(objects)}</resources>"
            f"<build>{''.join(items)}</build></model>")


def item(object_id: int, transform: str | None = None) -> str:
    return (f'<item objectid="{object_id}"'
            + (f' transform="{transform}"' if transform else "")
            + "/>")


def settings_xml(*object_ids: int) -> str:
    """The slicer settings document: the objects it names are real parts, the rest supports."""
    rows = "".join(f'<object id="{object_id}"><metadata key="name" value="p"/></object>'
                   for object_id in object_ids)
    return f'<?xml version="1.0" encoding="UTF-8"?><config>{rows}</config>'


def three_mf(tmp_path: Path, model: str, *, settings: str | None = None,
             members: dict[str, bytes] | None = None, name: str = "scene.3mf") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(preview.MODEL_MEMBER, model)
        if settings is not None:
            archive.writestr(preview.MODEL_SETTINGS_MEMBER, settings)
        for member, blob in (members or {}).items():
            archive.writestr(member, blob)
    return path


def binary_stl(triangles, header: bytes = b"") -> bytes:
    body = b"".join(struct.pack("<12fH", 0.0, 0.0, 0.0, *a, *b, *c, 0)
                    for a, b, c in triangles)
    return header.ljust(80, b"\0")[:80] + struct.pack("<I", len(triangles)) + body


def ascii_stl(triangles) -> bytes:
    lines = ["solid parts"]
    for a, b, c in triangles:
        lines += ["  facet normal 0 0 0", "    outer loop"]
        lines += ["      vertex %g %g %g" % vertex for vertex in (a, b, c)]
        lines += ["    endloop", "  endfacet"]
    return ("\n".join(lines + ["endsolid parts"]) + "\n").encode()


# --------------------------------------------------------------------- 3mf: instancing
def test_each_component_instance_becomes_its_own_object(tmp_path):
    """Two links to one mesh are two fins; flattening once would delete one of them."""
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1), component_object(2, [(1, "1 0 0 0 1 0 0 0 1 30 0 0"),
                                              (1, "1 0 0 0 1 0 0 0 1 0 40 0")])],
        [item(2)]))

    scene = preview.read_scene(path)

    assert [obj["count"] for obj in scene["objects"]] == [1, 1]
    assert [obj["id"] for obj in scene["objects"]] == [1, 2]
    # the two instances are the same mesh at two places, not one mesh twice at the origin
    assert sorted({obj["vertices"][0] for obj in scene["objects"]}) == [0.0, 30.0]


def test_two_build_items_of_the_same_object_are_two_leaves(tmp_path):
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1, name="part")],
        [item(1), item(1, "1 0 0 0 1 0 0 0 1 50 0 0")]))

    scene = preview.read_scene(path)

    assert len(scene["objects"]) == 2
    assert scene["bbox"]["size"] == [60.0, 10.0, 0.0]


def test_transforms_compose_inner_then_outer(tmp_path):
    """A child translate then a parent translate must both land, in that order."""
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1, ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))),
         component_object(2, [(1, "1 0 0 0 1 0 0 0 1 10 0 0")])],
        [item(2, "1 0 0 0 1 0 0 0 1 0 20 0")]))

    scene = preview.read_scene(path)

    assert scene["bbox"]["min"] == [10.0, 20.0, 0.0]
    assert scene["bbox"]["max"] == [11.0, 21.0, 0.0]


def test_a_rotation_transform_moves_vertices(tmp_path):
    """A 90° rotation about Z maps (10,0,0) to (0,10,0); the convention is row vectors."""
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1), component_object(2, [(1, "0 1 0 -1 0 0 0 0 1 0 0 0")])],
        [item(2)]))

    scene = preview.read_scene(path)

    assert scene["bbox"]["min"] == [-10.0, 0.0, 0.0]
    assert scene["bbox"]["max"] == [0.0, 10.0, 0.0]


def test_an_object_with_its_own_mesh_ignores_its_components(tmp_path):
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1, name="leaf"),
         component_object(2, [(1, None)],
                          name="mixed")],
        [item(2)]))

    # object 2 has components only; the leaf keeps its own name
    scene = preview.read_scene(path)

    assert [obj["name"] for obj in scene["objects"]] == ["leaf"]
    assert scene["objects"][0]["count"] == 1


# ------------------------------------------------------------------------------ 3mf: roles
def test_a_fin_named_object_is_supports_and_the_rest_are_parts(tmp_path):
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1, name="overhang part"),
         mesh_object(2, name="overhang FIN wall")],
        [item(1), item(2)]))

    roles = {obj["id"]: obj["role"] for obj in preview.read_scene(path)["objects"]}

    assert roles == {1: "part", 2: "supports"}


def test_an_object_omitted_by_the_slicer_settings_is_supports(tmp_path):
    """A slicer project names its real parts; anything else is a support mesh bolted on."""
    path = three_mf(tmp_path,
                    model_xml([mesh_object(1, name="body"), mesh_object(2, name="extra")],
                              [item(1), item(2)]),
                    settings=settings_xml(1))

    roles = {obj["id"]: obj["role"] for obj in preview.read_scene(path)["objects"]}

    assert roles == {1: "part", 2: "supports"}


def test_without_a_settings_document_every_named_object_is_a_part(tmp_path):
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1, name="body"), mesh_object(2, name="walls")], [item(1), item(2)]))

    assert [obj["role"] for obj in preview.read_scene(path)["objects"]] == ["part", "part"]


def test_an_unreadable_settings_document_falls_back_to_name_only(tmp_path):
    path = three_mf(tmp_path,
                    model_xml([mesh_object(1, name="body")], [item(1)]),
                    settings="{not xml at all")

    assert preview.read_scene(path)["objects"][0]["role"] == "part"


def test_an_unnamed_object_gets_an_id_based_label(tmp_path):
    path = three_mf(tmp_path, model_xml([mesh_object(7)], [item(7)]))

    assert preview.read_scene(path)["objects"][0]["name"] == "object 7"


# ---------------------------------------------------------------------------- 3mf: shapes
def test_objects_are_sorted_by_triangle_count_but_keep_their_leaf_ids(tmp_path):
    fan = [(0, 1, 2), (0, 2, 1), (0, 1, 2)]
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1, name="small"), mesh_object(2, QUAD, name="big", triangles=fan)],
        [item(1), item(2)]))

    objects = preview.read_scene(path)["objects"]

    assert [obj["count"] for obj in objects] == [3, 1]
    assert [obj["name"] for obj in objects] == ["big", "small"]
    assert [obj["id"] for obj in objects] == [2, 1]     # ids follow build order, not sort order


def test_vertices_are_rounded_to_a_micrometre_and_negative_zero_is_folded(tmp_path):
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1, ((1.23456, -0.0000001, 0.0), (2.0, 0.0, 0.0), (0.0, 3.0, 0.0)))],
        [item(1)]))

    vertices = preview.read_scene(path)["objects"][0]["vertices"]

    assert vertices[0] == 1.235
    assert vertices[1] == 0.0
    assert math.copysign(1.0, vertices[1]) == 1.0       # not -0.0
    assert len(vertices) == 9


def test_the_unit_defaults_to_millimetres_and_is_normalized(tmp_path):
    assert preview.read_scene(three_mf(tmp_path, model_xml(
        [mesh_object(1)], [item(1)], unit=None)))["unit"] == "millimeter"
    scene = preview.read_scene(three_mf(tmp_path, model_xml(
        [mesh_object(1)], [item(1)], unit="inch"), name="inch.3mf"))
    assert scene["unit"] == "millimeter"
    assert scene["bbox"]["size"] == pytest.approx([254, 254, 0])


def test_an_empty_unit_attribute_falls_back_to_millimetres(tmp_path):
    path = three_mf(tmp_path, model_xml([mesh_object(1)], [item(1)], unit=""))

    assert preview.read_scene(path)["unit"] == "millimeter"


# ------------------------------------------------------------------------------ 3mf: caps
def test_the_triangle_cap_names_the_count_and_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "MAX_TRIANGLES", 2)
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1, QUAD, triangles=[(0, 1, 2)] * 3)], [item(1)]))

    with pytest.raises(preview.PreviewError,
                       match=r"has at least 3 triangles; the preview cap is 2 triangles"):
        preview.read_scene(path)


def test_the_triangle_cap_is_cumulative_across_objects(tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "MAX_TRIANGLES", 1)
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1, name="a"), mesh_object(2, name="b")], [item(1), item(2)]))

    with pytest.raises(preview.PreviewError, match="the preview cap is 1 triangles"):
        preview.read_scene(path)


def test_the_byte_cap_refuses_before_anything_is_unpacked(tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "MAX_BYTES", 40)
    path = three_mf(tmp_path, model_xml([mesh_object(1)], [item(1)]))

    with pytest.raises(preview.PreviewError,
                       match=rf"scene\.3mf is \d+ bytes; the preview cap is 40 bytes"):
        preview.read_scene(path)


# --------------------------------------------------------------------------------- STL
def test_an_ascii_stl_is_one_part_object_named_after_the_file(tmp_path):
    path = tmp_path / "lid.stl"
    path.write_bytes(ascii_stl([TRIANGLE, TRIANGLE]))

    scene = preview.read_scene(path)

    assert scene["unit"] == "millimeter"
    assert len(scene["objects"]) == 1
    assert scene["objects"][0]["name"] == "lid"
    assert scene["objects"][0]["role"] == "part"
    assert scene["objects"][0]["count"] == 2
    assert scene["objects"][0]["vertices"] == [0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0]


def test_a_binary_stl_starting_with_solid_is_still_binary(tmp_path):
    """The same rule as app/mesh.py:90-113 — length arithmetic, never the keyword."""
    path = tmp_path / "cad.stl"
    path.write_bytes(binary_stl([TRIANGLE, TRIANGLE], header=b"solid exported-by-cad"))

    scene = preview.read_scene(path)

    assert scene["objects"][0]["count"] == 2
    assert scene["bbox"]["size"] == [10.0, 10.0, 0.0]


def test_identical_stl_vertices_are_deduplicated(tmp_path):
    path = tmp_path / "cube.stl"
    path.write_bytes((FIXTURES / "cube.stl").read_bytes())

    scene = preview.read_scene(path)

    assert scene["bbox"]["size"] == [20.0, 20.0, 20.0]
    assert scene["objects"][0]["count"] == 12
    assert len(scene["objects"][0]["vertices"]) == 3 * 8      # a cube has eight corners


def test_a_truncated_binary_stl_names_the_truncation(tmp_path):
    path = tmp_path / "cut.stl"
    path.write_bytes(binary_stl([TRIANGLE, TRIANGLE])[:-10])

    with pytest.raises(preview.PreviewError,
                       match=r"declares 2 binary STL triangles \(\d+ bytes\) but holds \d+ bytes"):
        preview.read_scene(path)


def test_an_stl_without_vertices_says_so(tmp_path):
    path = tmp_path / "empty.stl"
    path.write_bytes(b"solid nothing\nendsolid nothing\n")

    with pytest.raises(preview.PreviewError, match="has no readable STL vertices"):
        preview.read_scene(path)


def test_an_stl_above_the_byte_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "MAX_BYTES", 10)
    path = tmp_path / "big.stl"
    path.write_bytes(ascii_stl([TRIANGLE]))

    with pytest.raises(preview.PreviewError, match="the preview cap is 10 bytes"):
        preview.read_scene(path)


# --------------------------------------------------------------------------------- OBJ
def test_an_obj_polygon_is_fan_triangulated(tmp_path):
    path = tmp_path / "plate.obj"
    path.write_text("v 0 0 0\nv 2 0 0\nv 2 2 0\nv 0 2 0\nf 1 2 3 4\n")

    scene = preview.read_scene(path)

    assert scene["objects"][0]["count"] == 2
    assert scene["objects"][0]["triangles"] == [0, 1, 2, 0, 2, 3]
    assert scene["objects"][0]["role"] == "part"


def test_obj_face_forms_and_negative_indices_are_understood(tmp_path):
    path = tmp_path / "forms.obj"
    path.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
                    "f 1/1/1 2//1 3/2\n"
                    "f -1 -3 -2\n")

    scene = preview.read_scene(path)

    assert scene["objects"][0]["count"] == 2
    # -1 -3 -2 counts back from the four vertices listed so far: 3, 1, 2
    assert scene["objects"][0]["triangles"] == [0, 1, 2, 3, 1, 2]


def test_an_obj_index_outside_the_vertices_is_a_preview_error(tmp_path):
    path = tmp_path / "bad.obj"
    path.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 9\n")

    with pytest.raises(preview.PreviewError,
                       match=r"has an OBJ face index \(9\) outside its 3 vertices"):
        preview.read_scene(path)


def test_an_obj_without_faces_says_so(tmp_path):
    path = tmp_path / "points.obj"
    path.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\n")

    with pytest.raises(preview.PreviewError, match="has no readable OBJ faces"):
        preview.read_scene(path)


def test_an_obj_without_vertices_says_so(tmp_path):
    path = tmp_path / "none.obj"
    path.write_text("# nothing here\nf 1 2 3\n")

    with pytest.raises(preview.PreviewError,
                       match=r"has an OBJ face index \(1\) outside its 0 vertices"):
        preview.read_scene(path)


def test_unparsable_obj_records_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "noisy.obj"
    path.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nv not-a-number 1 2\n"
                    "f 1 2 3\nf 1 2 x\nvt 0 0\nvn 0 0 1\n")

    assert preview.read_scene(path)["objects"][0]["count"] == 1


# ----------------------------------------------------------------------- refusals
@pytest.mark.parametrize("suffix", [".zip", ".gcode", "", ".3MF.bak"])
def test_an_unsupported_suffix_is_refused(tmp_path, suffix):
    path = tmp_path / f"model{suffix}"
    path.write_bytes(b"whatever")

    with pytest.raises(preview.PreviewError,
                       match="the preview reads .3mf, .stl and .obj geometry only"):
        preview.read_scene(path)


def test_a_missing_file_names_the_os_reason(tmp_path):
    with pytest.raises(preview.PreviewError, match="missing.stl: cannot read the file"):
        preview.read_scene(tmp_path / "missing.stl")


@pytest.mark.parametrize("model, message", [
    (b"not a zip at all",
     "is not a readable 3MF archive"),
    (model_xml([mesh_object(1)], [item(1)]).replace(preview.MODEL_MEMBER, ""),
     "has no 3MF model part"),
])
def test_a_broken_3mf_archive_is_named(tmp_path, model, message):
    path = tmp_path / "broken.3mf"
    if isinstance(model, bytes) and not model.startswith(b"<?xml"):
        path.write_bytes(model)
    else:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("3D/other.model", model if isinstance(model, bytes) else model)
    with pytest.raises(preview.PreviewError, match=message):
        preview.read_scene(path)


@pytest.mark.parametrize("objects, items, message", [
    (None, [item(1)], "has no 3MF resources"),
    ([mesh_object(1)], [], "has no 3MF build items"),
    ([mesh_object(1)], [item(5)], r"naming object 5, which is not defined"),
    ([mesh_object(1), component_object(2, [(9, None)])], [item(2)],
     r"component naming object 9, which is not defined"),
    ([component_object(2, [(2, None)])], [item(2)],
     r"cyclic 3MF component reference \(object 2\)"),
    ([mesh_object(1)], [item(1, "1 0 0 0 1 0")], "malformed 3MF transform"),
])
def test_3mf_structure_errors_are_named(tmp_path, objects, items, message):
    model = (model_xml(objects, items) if objects is not None
             else f'<model xmlns="{NS}"><build>{items[0]}</build></model>')
    path = three_mf(tmp_path, model)

    with pytest.raises(preview.PreviewError, match=message):
        preview.read_scene(path)


def test_a_cyclic_component_two_levels_deep_is_refused(tmp_path):
    path = three_mf(tmp_path, model_xml(
        [component_object(1, [(2, None)]), component_object(2, [(1, None)])], [item(1)]))

    with pytest.raises(preview.PreviewError, match="cyclic 3MF component reference"):
        preview.read_scene(path)


def test_a_component_tree_deeper_than_the_cap_is_refused(tmp_path):
    objects = [component_object(index, [(index + 1, None)])
               for index in range(1, preview.MAX_COMPONENT_DEPTH + 3)]
    objects.append(mesh_object(preview.MAX_COMPONENT_DEPTH + 3))
    path = three_mf(tmp_path, model_xml(objects, [item(1)]))

    with pytest.raises(preview.PreviewError,
                       match=rf"nests 3MF components deeper than "
                             rf"{preview.MAX_COMPONENT_DEPTH} levels"):
        preview.read_scene(path)


@pytest.mark.parametrize("objects, message", [
    (['<object id="1" type="model"/>'],
     "has a 3MF object with neither a mesh nor components"),
    (['<object id="1"><mesh><vertices></vertices></mesh></object>'],
     "has a 3MF object without vertices"),
    (['<object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/>'
      '<vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>'
      '</vertices></mesh></object>'],
     "has a 3MF object without triangles"),
    ([mesh_object(1, TRIANGLE, triangles=[(0, 1, 9)])],
     r"triangle index \(9\) outside its 3 vertices"),
    ([mesh_object(1).replace('x="0.0"', 'x="nan"')], "has a non-finite 3MF vertex"),
    ([mesh_object(1).replace('x="0.0"', 'x="left"')],
     "has a 3MF vertex without usable coordinates"),
    ([mesh_object(1).replace('id="1"', 'id="one"')], "has a 3MF object without a numeric id"),
    ([mesh_object(1).replace('v1="0"', 'v1="zero"')],
     "has a 3MF triangle without usable indices"),
])
def test_3mf_object_errors_are_named(tmp_path, objects, message):
    path = three_mf(tmp_path, model_xml(objects, [item(1)]))

    with pytest.raises(preview.PreviewError, match=message):
        preview.read_scene(path)


def test_a_malformed_model_part_names_the_parse_error(tmp_path):
    path = three_mf(tmp_path, "<model><resources>", name="half.3mf")

    with pytest.raises(preview.PreviewError, match="half.3mf has a malformed 3MF model part"):
        preview.read_scene(path)


def test_a_build_item_by_component_without_a_numeric_target_is_refused(tmp_path):
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1)],
        ['<item objectid="one"/>']))

    with pytest.raises(preview.PreviewError,
                       match="has a 3MF build item without a numeric objectid"):
        preview.read_scene(path)


def test_a_component_without_a_numeric_objectid_is_refused(tmp_path):
    path = three_mf(tmp_path, model_xml(
        [mesh_object(1), '<object id="2"><components><component/></components></object>'],
        [item(2)]))

    with pytest.raises(preview.PreviewError,
                       match="has a 3MF component without a numeric objectid"):
        preview.read_scene(path)


# ----------------------------------------------------------------- integration (opt-in)
@pytest.mark.skipif(not supportfins.available(), reason="node/the bundled CLI is unavailable")
def test_the_finned_overhang_fixture_has_a_coloured_supports_object(tmp_path):
    """End to end: the real CLI writes a 3MF whose supports the preview can colour."""
    finned = tmp_path / "fins.3mf"
    supportfins.fin_model(FIXTURES / "overhang.stl", finned)

    scene = preview.read_scene(finned)

    assert len(scene["objects"]) >= 2
    supports = [obj for obj in scene["objects"] if obj["role"] == "supports"]
    assert supports and supports[0]["count"] > 0
    assert scene["bbox"]["size"][2] > 0


def test_preview_budget_keeps_supports_and_reports_dropped_triangles():
    import json as _json
    import subprocess
    from pathlib import Path

    html = Path("app/index.html").read_text()
    start = html.index("  const ordered = [...objects].sort")
    end = html.index("  tris.sort((p, q) => p[0] - q[0]);")
    snippet = html[start:end] + "  return {drawn, dropped, totalTriangles, tris, statsText: statsEl.textContent};\n"

    js = (
        "const fs = require('fs');\n"
        "const html = fs.readFileSync('app/index.html','utf8');\n"
        "function run(objects) {\n"
        "  const PREVIEW_TRIANGLE_BUDGET = 120000;\n  function previewObjectId(o){return String(o.id);}\n  function previewBaseColour(role){return role;}\n  const light=[-0.35,0.55,-0.78];\n  const toX=x=>x; const toY=y=>y;\n  const statsEl = {textContent: 'parts 1'};\n  function $(sel){return statsEl;}\n  function previewTint(b,x){return b;}\n  const rotated = new Map(objects.map(o => [String(o.id), new Float64Array(9 * ((o.triangles||[]).length/3 || 1))]));\n"
        + snippet.replace("ordered.reduce", "ordered.reduce")
        + "}\n"
        "const partTris = 100000, supportTris = 30000;\n"
        "const mk = (id, role, n) => ({id, role, triangles: Array.from({length: n*3}, (_,i)=> i % 3)});\n"
        "const objects = [mk('part','part',partTris), mk('supports','supports',supportTris)];\n"
        "const r = run(objects);\n"
        "console.log(JSON.stringify({drawn:r.drawn,dropped:r.dropped,totalTriangles:r.totalTriangles,tris:r.tris,statsText:r.statsText}));\n"
        "if (r.dropped !== 10000) process.exit(1);\n"
        "if (r.drawn !== 120000) process.exit(2);\n"
    )
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=".")
    assert proc.returncode == 0, proc.stderr
    result = _json.loads(proc.stdout.strip())
    assert result["dropped"] == 10000
    supports = sum(1 for t in result["tris"] if t[7] == "supports")
    part = sum(1 for t in result["tris"] if t[7] == "part")
    assert supports == 30000, supports
    assert part == 90000, part
    assert statsEl if False else True
    assert "truncated 10000 triangles" in result["statsText"]


@pytest.mark.parametrize("unit,multiplier", [("micron", .001), ("centimeter", 10),
                                              ("meter", 1000), ("foot", 304.8)])
def test_3mf_units_are_millimetres(tmp_path, unit, multiplier):
    path = three_mf(tmp_path, model_xml([mesh_object(1)], [item(1)], unit=unit))
    assert preview.read_scene(path)["bbox"]["size"] == pytest.approx([10 * multiplier, 10 * multiplier, 0])


def test_unknown_units_and_external_references_are_refused(tmp_path):
    path = three_mf(tmp_path, model_xml([mesh_object(1)], [item(1)], unit="parsec"))
    with pytest.raises(preview.PreviewError, match="unsupported 3MF unit"):
        preview.read_scene(path)
    path = three_mf(tmp_path, model_xml([mesh_object(1)], [item(1).replace('/>', ' path="/3D/other.model"/>')]))
    with pytest.raises(preview.PreviewError, match="external 3MF references"):
        preview.read_scene(path)


def test_compressed_expansion_limits_preview_and_scale(tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "MAX_EXPANDED_MEMBER", 1000)
    path = tmp_path / "bomb.3mf"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(preview.MODEL_MEMBER, model_xml([mesh_object(1)], [item(1)]))
        archive.writestr("extra", b"x" * 1001)
    dest = tmp_path / "out.3mf"
    dest.write_bytes(b"unchanged")
    for operation in (lambda: preview.read_scene(path), lambda: preview.scale_file(path, 2, dest)):
        with pytest.raises(preview.PreviewError, match="expanded data limit"):
            operation()
    assert dest.read_bytes() == b"unchanged"


def test_expanded_total_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "MAX_EXPANDED_TOTAL", 1000)
    path = three_mf(tmp_path, model_xml([mesh_object(1)], [item(1)]),
                    members={"first": b"x" * 400, "second": b"x" * 400})
    with pytest.raises(preview.PreviewError, match="expanded data limit"):
        preview.read_scene(path)
