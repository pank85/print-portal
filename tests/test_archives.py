"""Zip upload rules: every printable member, everything else fail-closed.

Every hostile case also asserts the destination directory is untouched and free of
``.part`` leftovers - the checks exist precisely so that nothing is written before the
archive is proven acceptable. A failure after a prefix has been written removes only
the ordinal directories that call created.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

import archives

MODELS = {".stl", ".3mf", ".obj", ".zip"}
CAP = 1024  # small on purpose: the oversized members stay tiny in memory


def _dest(tmp_path: Path, name: str = "out") -> Path:
    dest = tmp_path / name
    dest.mkdir(parents=True)
    return dest


def _upload(tmp_path: Path, blob: bytes, name: str = "upload.zip") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


def _extract(tmp_path: Path, blob: bytes, *, dest_name: str = "out", cap: int = CAP,
             allowed: set[str] = MODELS) -> list[archives.Selection]:
    return archives.extract_models(_upload(tmp_path, blob), _dest(tmp_path, dest_name),
                                   allowed=allowed, max_uncompressed=cap)


def _one(tmp_path: Path, blob: bytes, **kwargs) -> archives.Selection:
    found = _extract(tmp_path, blob, **kwargs)
    assert len(found) == 1
    return found[0]


def _assert_untouched(dest: Path) -> None:
    assert list(dest.rglob("*")) == []


def _rejected(tmp_path: Path, blob: bytes, status: int, message: str) -> None:
    dest = _dest(tmp_path)
    with pytest.raises(archives.ArchiveError) as err:
        archives.extract_models(_upload(tmp_path, blob), dest, allowed=MODELS,
                                max_uncompressed=CAP)
    assert err.value.status == status
    assert str(err.value) == message
    _assert_untouched(dest)


def test_zip_with_one_stl_is_extracted(zip_bytes, tmp_path):
    selection = _one(tmp_path, zip_bytes({"part.stl": b"solid cube\n"}))

    assert selection.member == "part.stl"
    assert selection.written == tmp_path / "out" / "00" / "part.stl"
    assert selection.written.read_bytes() == b"solid cube\n"
    assert selection.written.stat().st_mode & 0o777 == 0o644
    assert selection.candidates == ["part.stl"]
    assert selection.skipped == []
    assert selection.members == 1


def test_zip_extracts_every_model_in_sort_order(zip_bytes, tmp_path):
    # same depth, case-insensitively equal neighbours ("sub/Beta.stl" / "sub/beta.stl")
    # prove the bytewise tie-break; "sub/deep/x.stl" proves depth comes first.
    # Every printable member is extracted; only the non-model is skipped.
    blob = zip_bytes({"sub/deep/x.stl": b"x", "sub/zeta.stl": b"z", "sub/Beta.stl": b"B",
                      "sub/beta.stl": b"b", "notes.txt": b"n", "top.stl": b"t"})
    expected = [("top.stl", b"t"), ("sub/Beta.stl", b"B"), ("sub/beta.stl", b"b"),
                ("sub/zeta.stl", b"z"), ("sub/deep/x.stl", b"x")]

    found = _extract(tmp_path, blob)
    assert [(item.member, item.written.read_bytes()) for item in found] == expected
    assert [item.written.parent.name for item in found] == ["00", "01", "02", "03", "04"]
    assert found[0].candidates == [member for member, _payload in expected]
    assert found[0].skipped == ["notes.txt"]
    assert found[0].members == 6
    assert all(item.candidates == found[0].candidates and item.skipped == ["notes.txt"]
               for item in found)

    again = _extract(tmp_path, blob, dest_name="out-again")
    assert [(item.member, item.written.read_bytes(), item.candidates, item.skipped)
            for item in again] == [
                (item.member, item.written.read_bytes(), item.candidates, item.skipped)
                for item in found]


def test_zip_skips_macosx_and_dotfiles(zip_bytes, tmp_path):
    blob = zip_bytes({"__MACOSX/part.stl": b"junk", "__MACOSX/._part.stl": b"junk",
                      "._part.stl": b"junk", ".DS_Store": b"junk", "assets/": b"",
                      "part.stl": b"solid\n"})

    selection = _one(tmp_path, blob)

    assert selection.member == "part.stl"
    assert selection.written.read_bytes() == b"solid\n"
    assert selection.candidates == ["part.stl"]
    assert set(selection.skipped) == {"__MACOSX/part.stl", "__MACOSX/._part.stl",
                                      "._part.stl", ".DS_Store", "assets/"}
    assert selection.members == 6


def test_zip_member_with_parent_traversal_is_rejected(zip_bytes, zip_bytes_with, tmp_path):
    _rejected(tmp_path, zip_bytes_with({"part.stl": b"solid\n"}, traversal_name="../evil.stl"),
              400, "archive member is not safe: ../evil.stl")
    _rejected(tmp_path / "nested", zip_bytes({"models/../../evil.stl": b"evil"}),
              400, "archive member is not safe: models/../../evil.stl")
    assert not (tmp_path / "evil.stl").exists()


def test_zip_absolute_member_is_rejected(zip_bytes, tmp_path):
    for name in ("/abs.stl", "C:\\evil.stl"):
        _rejected(tmp_path / name.strip("/\\").replace(":", ""), zip_bytes({name: b"evil"}),
                  400, f"archive member is not safe: {name}")


def test_zip_symlink_member_is_rejected(zip_bytes_with, tmp_path):
    # the symlink sits next to a perfectly good model: the whole archive still fails
    _rejected(tmp_path, zip_bytes_with({"part.stl": b"solid\n"}, symlink_name="link.stl"),
              400, "archive member is a symlink: link.stl")


def test_zip_with_too_many_entries_is_rejected(zip_bytes, tmp_path):
    crowded = {f"f{i}.txt": b"x" for i in range(archives.ZIP_MAX_MEMBERS + 1)}
    _rejected(tmp_path, zip_bytes(crowded), 413,
              f"archive has more than {archives.ZIP_MAX_MEMBERS} entries")

    # exactly at the cap is still fine
    at_cap = {f"f{i}.txt": b"x" for i in range(archives.ZIP_MAX_MEMBERS - 1)}
    at_cap["part.stl"] = b"solid\n"
    selection = _one(tmp_path, zip_bytes(at_cap), dest_name="at-cap")
    assert selection.member == "part.stl"
    assert selection.members == archives.ZIP_MAX_MEMBERS


def test_zip_member_beyond_the_cap_is_rejected_while_streaming(zip_bytes, tmp_path):
    # one byte over a deliberately tiny cap. The header check and the streaming count
    # raise the same 413 (zipfile never hands out more than the declared size), so the
    # assertions are the status, the message and the empty destination.
    _rejected(tmp_path, zip_bytes({"part.stl": b"x" * (CAP + 1)}), 413,
              f"archive member expands to more than {CAP} bytes")

    # exactly at the cap is extracted, so the bound is "more than", not "at least"
    selection = _one(tmp_path, zip_bytes({"part.stl": b"y" * CAP}), dest_name="at-cap")
    assert selection.written.read_bytes() == b"y" * CAP


def test_empty_zip_is_rejected(zip_bytes, tmp_path):
    _rejected(tmp_path, zip_bytes({}), 400,
              "archive contains no .stl, .3mf or .obj file (found: nothing)")


def test_zip_without_models_lists_what_it_found(zip_bytes, tmp_path):
    _rejected(tmp_path, zip_bytes({"readme.txt": b"r", "photo.png": b"p"}), 400,
              "archive contains no .stl, .3mf or .obj file (found: readme.txt, photo.png)")


def test_truncated_zip_is_not_readable(zip_bytes, tmp_path):
    blob = zip_bytes({"part.stl": b"solid cube\n"})
    _rejected(tmp_path, blob[: len(blob) // 2], 400, "not a readable .zip archive")
    _rejected(tmp_path / "plain", b"definitely not a zip archive", 400,
              "not a readable .zip archive")

    with pytest.raises(zipfile.BadZipFile):
        # guard: the truncation really does break the archive, so the test cannot pass
        # vacuously
        zipfile.ZipFile(_upload(tmp_path, blob[: len(blob) // 2], "check.zip"))


def test_extraction_writes_only_the_basename_and_no_temp_files(zip_bytes, tmp_path):
    selection = _one(tmp_path, zip_bytes({"nested/deeper/part.stl": b"solid\n"}))

    assert selection.member == "nested/deeper/part.stl"
    assert selection.written == tmp_path / "out" / "00" / "part.stl"
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == ["00"]
    assert list((tmp_path / "out").rglob("*.part")) == []
    assert not (tmp_path / "out" / "nested").exists()
    assert not (tmp_path / "nested").exists()


def test_zip_extracts_mixed_extensions_and_case(zip_bytes, tmp_path):
    blob = zip_bytes({"Mesh.OBJ": b"obj", "plate.3Mf": b"3mf", "models/Part.STL": b"stl",
                      "readme.txt": b"r", "nested.zip": b"pk"})

    found = _extract(tmp_path, blob)

    assert [(item.member, item.written.name, item.written.read_bytes()) for item in found] == [
        ("Mesh.OBJ", "Mesh.OBJ", b"obj"),
        ("plate.3Mf", "plate.3Mf", b"3mf"),
        ("models/Part.STL", "Part.STL", b"stl"),
    ]
    assert found[0].skipped == ["nested.zip", "readme.txt"]
    assert found[0].candidates == ["Mesh.OBJ", "plate.3Mf", "models/Part.STL"]
    assert not (tmp_path / "out" / "00" / "inner.stl").exists()


def test_duplicate_basenames_keep_distinct_payloads(zip_bytes, tmp_path):
    found = _extract(tmp_path, zip_bytes({"a/part.stl": b"one", "b/part.stl": b"two"}))

    assert [item.member for item in found] == ["a/part.stl", "b/part.stl"]
    assert [item.written.name for item in found] == ["part.stl", "part.stl"]
    assert [item.written.read_bytes() for item in found] == [b"one", b"two"]
    assert [item.written.parent.name for item in found] == ["00", "01"]


def test_duplicate_member_names_are_opened_by_zipinfo(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("part.stl", b"first")
        archive.writestr("part.stl", b"second")

    found = _extract(tmp_path, buf.getvalue())

    assert [item.member for item in found] == ["part.stl", "part.stl"]
    assert [item.written.read_bytes() for item in found] == [b"first", b"second"]
    assert found[0].written != found[1].written
    assert found[0].members == 2


def test_nested_3mf_is_one_model_not_its_inner_stl(zip_bytes, tmp_path):
    inner = zip_bytes({"inner.stl": b"inner"})

    found = _extract(tmp_path, zip_bytes({"model.3mf": inner, "notes.txt": b"n"}))

    assert len(found) == 1
    assert found[0].member == "model.3mf"
    assert found[0].written.read_bytes() == inner
    assert found[0].skipped == ["notes.txt"]


def test_aggregate_declared_size_rejects_before_any_write(zip_bytes, tmp_path):
    dest = _dest(tmp_path)
    (dest / "keep.txt").write_text("keep")
    blob = zip_bytes({"a.stl": b"a" * 6, "b.stl": b"b" * 6})

    with pytest.raises(archives.ArchiveError) as err:
        archives.extract_models(_upload(tmp_path, blob), dest, allowed=MODELS, max_uncompressed=10)

    assert err.value.status == 413
    assert str(err.value) == "archive models expand to more than 10 bytes"
    assert [path.name for path in dest.iterdir()] == ["keep.txt"]


def test_one_member_over_the_cap_keeps_the_member_message(zip_bytes, tmp_path):
    dest = _dest(tmp_path)
    blob = zip_bytes({"huge.stl": b"h" * 11, "ok.stl": b"ok"})
    with pytest.raises(archives.ArchiveError) as err:
        archives.extract_models(_upload(tmp_path, blob), dest, allowed=MODELS, max_uncompressed=10)
    assert err.value.status == 413
    assert str(err.value) == "archive member expands to more than 10 bytes"
    assert list(dest.rglob("*")) == []


def test_aggregate_exactly_at_the_cap_is_extracted(zip_bytes, tmp_path):
    found = _extract(tmp_path, zip_bytes({"a.stl": b"a" * 6, "b.stl": b"b" * 4}), cap=10)

    assert [item.written.read_bytes() for item in found] == [b"a" * 6, b"b" * 4]
    assert [item.written.parent.name for item in found] == ["00", "01"]


class _Extra:
    """A zip member stream that yields extra bytes after the declared payload."""

    def __init__(self, raw, extra: bytes):
        self._raw = raw
        self._extra = extra
        self._sent = False

    def read(self, n: int = -1) -> bytes:
        data = self._raw.read(n)
        if data:
            return data
        if not self._sent:
            self._sent = True
            return self._extra
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._raw.close()
        return False


def test_running_byte_count_rejects_an_underdeclared_later_member(zip_bytes, tmp_path, monkeypatch):
    blob = zip_bytes({"a.stl": b"aaaaaa", "b.stl": b"bb"})  # declared 6 + 2, cap 10
    dest = _dest(tmp_path)
    (dest / "keep.txt").write_text("keep")
    real_open = zipfile.ZipFile.open

    def open_stub(self, name, *args, **kwargs):
        src = real_open(self, name, *args, **kwargs)
        info = name if isinstance(name, zipfile.ZipInfo) else self.getinfo(name)
        if info.filename == "b.stl":
            return _Extra(src, b"xxxx")  # actual 6, still within the member cap; sum is not
        return src

    monkeypatch.setattr(zipfile.ZipFile, "open", open_stub)
    with pytest.raises(archives.ArchiveError) as err:
        archives.extract_models(_upload(tmp_path, blob), dest, allowed=MODELS, max_uncompressed=10)

    assert err.value.status == 413
    assert str(err.value) == "archive models expand to more than 10 bytes"
    assert [path.name for path in dest.iterdir()] == ["keep.txt"]
    assert list(dest.rglob("*.part")) == []


def test_unreadable_later_member_removes_earlier_outputs(zip_bytes, tmp_path, monkeypatch):
    blob = zip_bytes({"a.stl": b"aaa", "b.stl": b"bbb"})
    dest = _dest(tmp_path)
    (dest / "keep.txt").write_text("keep")
    real_open = zipfile.ZipFile.open

    def open_stub(self, name, *args, **kwargs):
        info = name if isinstance(name, zipfile.ZipInfo) else self.getinfo(name)
        if info.filename == "b.stl":
            raise zipfile.BadZipFile("bad crc")
        return real_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", open_stub)
    with pytest.raises(archives.ArchiveError) as err:
        archives.extract_models(_upload(tmp_path, blob), dest, allowed=MODELS, max_uncompressed=CAP)

    assert err.value.status == 400
    assert str(err.value) == "not a readable .zip archive"
    assert [path.name for path in dest.iterdir()] == ["keep.txt"]
