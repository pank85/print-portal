"""Still pictures when the finned mesh is over the interactive preview cap."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import shot


def _mesh(name: str, verts, tris) -> str:
    v = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
    t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return (
        f'<object id="1" type="model" name="{name}"><mesh><vertices>{v}</vertices>'
        f"<triangles>{t}</triangles></mesh></object>"
    )


def _pack(objects: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f"<resources>{objects}</resources><build><item objectid=\"1\"/></build></model>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", xml)
    return buf.getvalue()


def test_render_paints_the_fins_green(tmp_path: Path, monkeypatch):
    part = _mesh("part", [(0, 0, 0), (10, 0, 0), (0, 0, 10)], [(0, 1, 2)])
    fins = _mesh("overhang fin", [(0, 0, 0), (40, 0, 0), (0, 0, 40)], [(0, 1, 2)])
    path = tmp_path / "fins.3mf"
    path.write_bytes(_pack(part + fins))
    seen = {}
    original = shot._png

    def grab(rgb, width, height):
        seen["rgb"] = rgb
        return original(rgb, width, height)

    monkeypatch.setattr(shot, "_png", grab)
    out = shot.render(path, tmp_path / "shots")

    assert out["front"].read_bytes().startswith(b"\x89PNG")
    assert bytes(shot._FINS) in seen["rgb"]
    assert out["side"].stat().st_size > 100
