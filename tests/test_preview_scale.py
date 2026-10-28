"""Uniform scale of a staged support-fins job, and the 3MF transform bake."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import preview

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Axis map: width=bbox[0], length=bbox[1], height=bbox[2].
WIDTH, LENGTH, HEIGHT = 10.0, 30.0, 40.0


def _box_stl() -> bytes:
    """One triangle whose bbox is width x length x height (10 x 30 x 40)."""
    return (
        "solid box\n"
        "  facet normal 0 0 0\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        f"      vertex {WIDTH:g} 0 0\n"
        f"      vertex {WIDTH:g} {LENGTH:g} {HEIGHT:g}\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid box\n"
    ).encode()


def _stage(jobs_dir: Path, blob: bytes, *, status: str = "awaiting_confirm",
           filename: str = "box.stl", jid: str = "aabbccddeeff") -> str:
    upload = jobs_dir / jid / "upload"
    upload.mkdir(parents=True)
    (upload / filename).write_bytes(blob)
    (jobs_dir / jid / "job.json").write_text(json.dumps({
        "id": jid,
        "status": status,
        "filename": filename,
        "support_fins": True,
    }))
    return jid


def test_scale_while_awaiting_confirm_uses_one_factor(client, jobs_dir):
    jid = _stage(jobs_dir, _box_stl())

    response = client.post(f"/api/jobs/{jid}/scale", json={"axis": "width", "value": 20})

    assert response.status_code == 200, response.text
    body = response.json()
    # width was bbox[0]; the other two axes move by the same factor.
    assert body["filename"] == "box.stl"
    assert body["factor"] == pytest.approx(2)
    assert body["size"] == pytest.approx([20.0, 60.0, 80.0])
    upload = jobs_dir / jid / "upload"
    assert sorted(p.name for p in upload.iterdir()) == ["box.stl"]


def test_a_second_scale_does_not_create_a_second_file(client, jobs_dir):
    jid = _stage(jobs_dir, _box_stl())
    first = client.post(f"/api/jobs/{jid}/scale", json={"axis": "height", "value": 80})
    assert first.status_code == 200, first.text

    second = client.post(f"/api/jobs/{jid}/scale", json={"axis": "length", "value": 30})

    assert second.status_code == 200, second.text
    assert second.json()["filename"] == "box.stl"
    assert second.json()["size"] == pytest.approx([10.0, 30.0, 40.0])
    upload = jobs_dir / jid / "upload"
    assert sorted(p.name for p in upload.iterdir()) == ["box.stl"]
    assert not list(upload.glob("*.scaled_tmp*"))


def test_scale_refuses_a_job_that_is_not_awaiting_confirm(client, jobs_dir):
    jid = _stage(jobs_dir, _box_stl(), status="queued")

    response = client.post(f"/api/jobs/{jid}/scale", json={"axis": "height", "value": 80})

    assert response.status_code == 409
    assert "not awaiting confirmation" in response.json()["detail"]


def _tiny_3mf() -> bytes:
    model = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" name="part">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/>
          <vertex x="1" y="0" z="0"/>
          <vertex x="0" y="2" z="3"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
        </triangles>
      </mesh>
    </object>
    <object id="2" name="assembly">
      <components>
        <component objectid="1" transform="2 0 0 0 3 0 0 0 4 5 6 7"/>
      </components>
    </object>
  </resources>
  <build>
    <item objectid="2"/>
  </build>
</model>
"""
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("3D/3dmodel.model", model)
        archive.writestr("Metadata/note.txt", b"keep-me")
    return buf.getvalue()


def test_scale_file_round_trips_cube_and_scales_a_3mf_transform(tmp_path):
    cube = FIXTURES / "cube.stl"
    before = preview.read_scene(cube)
    scaled_stl = tmp_path / "cube.stl"
    preview.scale_file(cube, 2, scaled_stl)
    after = preview.read_scene(scaled_stl)
    assert after["bbox"]["size"] == pytest.approx([value * 2 for value in before["bbox"]["size"]])

    src = tmp_path / "part.3mf"
    src.write_bytes(_tiny_3mf())
    out = tmp_path / "part-scaled.3mf"
    preview.scale_file(src, 2, out)
    scene = preview.read_scene(out)
    before_3mf = preview.read_scene(src)
    for key in ("min", "max", "size"):
        assert scene["bbox"][key] == pytest.approx([v * 2 for v in before_3mf["bbox"][key]])
    assert len(scene["bbox"]["size"]) == 3
    assert all(value > 0 for value in scene["bbox"]["size"])
    with zipfile.ZipFile(src) as original, zipfile.ZipFile(out) as scaled:
        assert scaled.read("Metadata/note.txt") == original.read("Metadata/note.txt") == b"keep-me"
        root = __import__("xml.etree.ElementTree", fromlist=["ElementTree"]).fromstring(
            scaled.read("3D/3dmodel.model"))
    transform = None
    vertex_x = None
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "component":
            transform = [float(token) for token in element.get("transform").split()]
        if tag == "vertex" and element.get("x") == "2":
            vertex_x = 2.0
    # Linear terms are preserved; translations and vertices scale once.
    assert transform == pytest.approx([2, 0, 0, 0, 3, 0, 0, 0, 4, 10, 12, 14])
    assert vertex_x == 2.0


def test_nested_shared_instances_rotation_build_translation_and_repeated_scale(tmp_path):
    from test_preview import mesh_object, component_object, model_xml, item, three_mf
    src = three_mf(tmp_path, model_xml([
        mesh_object(1),
        component_object(2, [(1, "0 1 0 -1 0 0 0 0 1 5 6 7"),
                             (1, "1 0 0 0 1 0 0 0 1 -3 4 5")]),
        component_object(3, [(2, "1 0 0 0 1 0 0 0 1 8 9 10")]),
    ], [item(3, "1 0 0 0 1 0 0 0 1 11 12 13"), item(1)], unit="inch"))
    before = preview.read_scene(src)
    out = tmp_path / "out.3mf"
    preview.scale_file(src, 2, out)
    preview.scale_file(out, 1.5, out)
    after = preview.read_scene(out)
    for a, b in zip(before["objects"], after["objects"]):
        assert b["vertices"] == pytest.approx([v * 3 for v in a["vertices"]], abs=0.002)
    for key in ("min", "max", "size"):
        assert after["bbox"][key] == pytest.approx([v * 3 for v in before["bbox"][key]], abs=0.003)


def test_short_ascii_stl_and_whitespace_obj_scaling(tmp_path):
    stl = tmp_path / "short.stl"
    stl.write_text("vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n")
    preview.scale_file(stl, 2, stl)
    assert preview.read_scene(stl)["bbox"]["size"] == [2, 2, 0]
    obj = tmp_path / "tabs.obj"
    obj.write_text("  v\t0 0 0\nv\t1 0 0\n v 0 1 0\nf 1 2 3\n")
    preview.scale_file(obj, 3, obj)
    assert preview.read_scene(obj)["bbox"]["size"] == [3, 3, 0]


@pytest.mark.parametrize("factor", [float('nan'), float('inf'), 0, -1, True, 1e100])
def test_invalid_scale_preserves_destination(tmp_path, factor):
    src = tmp_path / "in.stl"
    src.write_bytes(_box_stl())
    out = tmp_path / "out.stl"
    out.write_bytes(b"unchanged")
    with pytest.raises(preview.PreviewError):
        preview.scale_file(src, factor, out)
    assert out.read_bytes() == b"unchanged"
    assert src.read_bytes() == _box_stl()


def test_geometry_capability_zero_and_unsupported(tmp_path):
    path = tmp_path / "zero.obj"
    path.write_text("v 0 0 0\nv 0 0 0\nv 0 0 0\nf 1 2 3\n")
    result = preview.geometry_capability(path)
    assert result["preview_available"] and not result["resize_available"]
    assert result["size"] == [0, 0, 0]
    assert not preview.geometry_capability(tmp_path / "missing.obj")["preview_available"]
