#!/usr/bin/env python3
"""Pick one model out of an uploaded .zip and write only its bytes.

An uploaded archive is untrusted input, so every rule here is fail-closed:

  * the archive has to parse (``zipfile.BadZipFile`` is a 400 - a truncated upload is
    this case) and its entry count is capped before anything is inspected (413);
  * **every** member name is validated, not just the chosen one: an absolute path, a
    ``..`` component or a symlink entry rejects the whole archive (400). A hostile
    archive must not "mostly work", so nothing is silently skipped;
  * the extracted file is ``dest_dir/<basename of the member>``: the member path is
    never used as a path, so no member can direct the write outside ``dest_dir``;
  * the bytes are written through a ``.part`` sibling and streamed in 1 MiB chunks,
    counting them against ``max_uncompressed``. The per-member ``file_size`` in the
    header is attacker-controlled, so it only serves as an early sanity check; the
    running count is what bounds the write. A rejected member leaves no partial file.

Nested archives are not recursed into: ``.zip`` is removed from ``allowed`` before the
candidate suffixes are computed, so a zip of zips is rejected with the "contains no
model" error instead of being unpacked recursively.

The pick is deterministic (see :func:`_sort_key`): the shallowest model wins, ties are
broken case-insensitively and then bytewise, so the same archive always yields the same
member - which is what makes the UI's "part.stl (1 of 3 model files)" wording stable.
"""
from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Entry cap for an uploaded archive: a model zip is a handful of files plus maybe a
# README, so anything this large is either junk or an attack.
ZIP_MAX_MEMBERS = 64

CHUNK_BYTES = 1 << 20
PART_SUFFIX = ".part"
FILE_MODE = 0o644

MACOS_METADATA = "__macosx"


class ArchiveError(Exception):
    """A rejected archive. ``status`` is the HTTP status to answer with (400 or 413)."""

    status: int

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Selection:
    """The member that was extracted, plus what the archive offered instead."""

    member: str
    written: Path
    candidates: list[str]
    skipped: list[str]
    members: int


def _posix_name(member: str) -> str:
    """The member name with Windows separators folded to '/'.

    ZIP entries are supposed to use '/', but a hostile archive may use '\\' to hide an
    absolute path or a traversal from a '/'-only check.
    """
    return member.replace("\\", "/")


def _is_absolute(name: str) -> bool:
    """True for '/x', '//host/share' and 'C:\\x' (after :func:`_posix_name`)."""
    if name.startswith("/"):
        return True
    head = name.split("/", 1)[0]
    return len(head) == 2 and head[0].isalpha() and head[1] == ":"


def _is_unsafe(member: str) -> bool:
    name = _posix_name(member)
    return _is_absolute(name) or ".." in PurePosixPath(name).parts


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _is_skipped(member: str) -> bool:
    """Entries that are noise rather than models: directories and macOS metadata.

    Dotfiles and ``._*`` basenames are macOS resource forks, which would otherwise win
    the pick for an archive exported from Finder.
    """
    name = _posix_name(member)
    parts = PurePosixPath(name).parts
    if name.endswith("/") or not parts:
        return True
    if parts[0].casefold() == MACOS_METADATA:
        return True
    return parts[-1].startswith(".")


def _basename(member: str) -> str:
    return PurePosixPath(_posix_name(member)).name


def _sort_key(member: str) -> tuple[int, str, str]:
    name = _posix_name(member)
    return (len(PurePosixPath(name).parts), name.casefold(), name)


def extract_model(archive: Path, dest_dir: Path, *, allowed: set[str],
                  max_uncompressed: int, max_members: int = ZIP_MAX_MEMBERS) -> Selection:
    """Extract the one model member of ``archive`` into ``dest_dir``.

    ``allowed`` is the caller's model suffix set (``.zip`` is dropped here, so nested
    archives are never followed). Raises :class:`ArchiveError` for anything rejected and
    leaves ``dest_dir`` untouched in that case.
    """
    models = {suffix.lower() for suffix in allowed} - {".zip"}
    try:
        handle = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(400, "not a readable .zip archive") from exc

    with handle:
        infos = handle.infolist()
        if len(infos) > max_members:
            raise ArchiveError(413, f"archive has more than {max_members} entries")
        for info in infos:
            if _is_unsafe(info.filename):
                raise ArchiveError(400, f"archive member is not safe: {info.filename}")
            if _is_symlink(info):
                raise ArchiveError(400, f"archive member is a symlink: {info.filename}")

        names = [info.filename for info in infos]
        considered = [name for name in names if not _is_skipped(name)]
        candidates = sorted(
            (name for name in considered
             if PurePosixPath(_posix_name(name)).suffix.lower() in models),
            key=_sort_key)
        if not candidates:
            found = ", ".join(considered) or "nothing"
            raise ArchiveError(
                400, "archive contains no .stl, .3mf or .obj file "
                     f"(found: {found})")

        member = candidates[0]
        info = next(info for info in infos if info.filename == member)
        try:
            written = _extract(handle, info, dest_dir, max_uncompressed)
        except zipfile.BadZipFile as exc:
            # a member stream that is truncated or fails its CRC is the same 400 as an
            # archive that does not parse at all
            raise ArchiveError(400, "not a readable .zip archive") from exc

    return Selection(member=member, written=written, candidates=candidates,
                     skipped=sorted((name for name in names if name != member), key=_sort_key),
                     members=len(infos))


def _extract(handle: zipfile.ZipFile, info: zipfile.ZipInfo, dest_dir: Path,
             max_uncompressed: int) -> Path:
    basename = _basename(info.filename)
    if not basename:
        raise ArchiveError(400, f"archive member is not safe: {info.filename}")
    # the header size is attacker-controlled, so it only lets an obviously oversized
    # member fail before the first read; the running count below is the real bound
    if info.file_size > max_uncompressed:
        raise ArchiveError(413, f"archive member expands to more than {max_uncompressed} bytes")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    written = dest_dir / basename
    part = dest_dir / (basename + PART_SUFFIX)
    total = 0
    try:
        with handle.open(info) as src, part.open("wb") as out:
            while chunk := src.read(CHUNK_BYTES):
                total += len(chunk)
                if total > max_uncompressed:
                    raise ArchiveError(
                        413, f"archive member expands to more than {max_uncompressed} bytes")
                out.write(chunk)
        os.chmod(part, FILE_MODE)
        os.replace(part, written)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return written
