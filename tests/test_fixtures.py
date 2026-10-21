import json
import zipfile
from io import BytesIO

import pytest


def test_three_mf_member_list_and_json(three_mf_bytes):
    blob = three_mf_bytes()
    with zipfile.ZipFile(BytesIO(blob)) as z:
        names = z.namelist()
        assert "[Content_Types].xml" in names
        assert "_rels/.rels" in names
        assert "3D/3dmodel.model" in names
        assert "Metadata/project_settings.config" in names
        assert "Metadata/model_settings.config" in names
        assert z.testzip() is None
        data = json.loads(z.read("Metadata/project_settings.config"))
        assert isinstance(data, dict)
        assert data["printer_settings_id"] == ["Generic Klipper 0.4 nozzle"]


def test_slic3r_pe_ini(three_mf_bytes):
    blob = three_mf_bytes(settings_member="Slic3r_PE.config")
    with zipfile.ZipFile(BytesIO(blob)) as z:
        assert "Metadata/Slic3r_PE.config" in z.namelist()
        text = z.read("Metadata/Slic3r_PE.config").decode()
        assert "printer_settings_id = Generic Klipper 0.4 nozzle" in text


def test_settings_member_none_omits(three_mf_bytes):
    blob = three_mf_bytes(settings_member=None)
    with zipfile.ZipFile(BytesIO(blob)) as z:
        assert not any("project_settings" in n or "Slic3r_PE" in n for n in z.namelist())


def test_zip_bytes_with_traversal_and_symlink(zip_bytes_with):
    blob = zip_bytes_with({}, traversal_name="../evil.stl", symlink_name="link.stl")
    with zipfile.ZipFile(BytesIO(blob)) as z:
        assert "../evil.stl" in z.namelist()
        info = z.getinfo("link.stl")
        assert (info.external_attr >> 16) == 0o120777


def test_zip_bytes_roundtrip(zip_bytes):
    members = {"a.txt": b"hello", "b.bin": b"\x00\x01"}
    blob = zip_bytes(members)
    with zipfile.ZipFile(BytesIO(blob)) as z:
        for n, d in members.items():
            assert z.read(n) == d


def test_write_bytes_fixture(write_bytes_fixture, tmp_path):
    blob = b"abc"
    p = write_bytes_fixture(tmp_path, "x.bin", blob)
    assert p.read_bytes() == blob
