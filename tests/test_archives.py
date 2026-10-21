"""Zip upload rules: one deterministic model member, everything else fail-closed.

Every hostile case also asserts the destination directory is untouched and free of
``.part`` leftovers - the checks exist precisely so that nothing is written before the
archive is proven acceptable.
"""
from __future__ import annotations

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
             allowed: set[str] = MODELS) -> archives.Selection:
    return archives.extract_model(_upload(tmp_path, blob), _dest(tmp_path, dest_name),
                                  allowed=allowed, max_uncompressed=cap)


def _assert_untouched(dest: Path) -> None:
    assert list(dest.rglob("*")) == []


def _rejected(tmp_path: Path, blob: bytes, status: int, message: str) -> None:
    dest = _dest(tmp_path)
    with pytest.raises(archives.ArchiveError) as err:
        archives.extract_model(_upload(tmp_path, blob), dest, allowed=MODELS,
                               max_uncompressed=CAP)
    assert err.value.status == status
    assert str(err.value) == message
    _assert_untouched(dest)


def test_zip_with_one_stl_is_extracted(zip_bytes, tmp_path):
    selection = _extract(tmp_path, zip_bytes({"part.stl": b"solid cube\n"}))

    assert selection.member == "part.stl"
    assert selection.written == tmp_path / "out" / "part.stl"
    assert selection.written.read_bytes() == b"solid cube\n"
    assert selection.written.stat().st_mode & 0o777 == 0o644
    assert selection.candidates == ["part.stl"]
    assert selection.skipped == []
    assert selection.members == 1


def test_zip_picks_the_shallowest_alphabetically_first_model(zip_bytes, tmp_path):
    # same depth, case-insensitively equal neighbours ("sub/Beta.stl" / "sub/beta.stl")
    # prove the bytewise tie-break; "sub/deep/x.stl" proves depth comes first
    blob = zip_bytes({"sub/deep/x.stl": b"x", "sub/zeta.stl": b"z", "sub/Beta.stl": b"B",
                      "sub/beta.stl": b"b", "notes.txt": b"n", "top.stl": b"t"})

    first = _extract(tmp_path, blob)
    assert first.member == "top.stl"
    assert first.written.read_bytes() == b"t"
    assert first.candidates == ["top.stl", "sub/Beta.stl", "sub/beta.stl",
                                "sub/zeta.stl", "sub/deep/x.stl"]
    assert first.skipped == ["notes.txt", "sub/Beta.stl", "sub/beta.stl",
                             "sub/zeta.stl", "sub/deep/x.stl"]
    assert first.members == 6

    again = _extract(tmp_path, blob, dest_name="out-again")
    assert (again.member, again.candidates, again.skipped) == (
        first.member, first.candidates, first.skipped)


def test_zip_skips_macosx_and_dotfiles(zip_bytes, tmp_path):
    blob = zip_bytes({"__MACOSX/part.stl": b"junk", "__MACOSX/._part.stl": b"junk",
                      "._part.stl": b"junk", ".DS_Store": b"junk", "assets/": b"",
                      "part.stl": b"solid\n"})

    selection = _extract(tmp_path, blob)

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
    selection = _extract(tmp_path, zip_bytes(at_cap), dest_name="at-cap")
    assert selection.member == "part.stl"
    assert selection.members == archives.ZIP_MAX_MEMBERS


def test_zip_member_beyond_the_cap_is_rejected_while_streaming(zip_bytes, tmp_path):
    # one byte over a deliberately tiny cap. The header check and the streaming count
    # raise the same 413 (zipfile never hands out more than the declared size), so the
    # assertions are the status, the message and the empty destination.
    _rejected(tmp_path, zip_bytes({"part.stl": b"x" * (CAP + 1)}), 413,
              f"archive member expands to more than {CAP} bytes")

    # exactly at the cap is extracted, so the bound is "more than", not "at least"
    selection = _extract(tmp_path, zip_bytes({"part.stl": b"y" * CAP}), dest_name="at-cap")
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
    selection = _extract(tmp_path, zip_bytes({"nested/deeper/part.stl": b"solid\n"}))

    assert selection.member == "nested/deeper/part.stl"
    assert selection.written == tmp_path / "out" / "part.stl"
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == ["part.stl"]
    assert list((tmp_path / "out").glob("*.part")) == []
    assert not (tmp_path / "out" / "nested").exists()
    assert not (tmp_path / "nested").exists()
