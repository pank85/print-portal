#!/usr/bin/env python3
"""Printer registry for print-portal: Fernet-encrypted API keys in a JSON file.

``printers.json`` holds the printers the operator configured in Settings. Each API key is
stored as a Fernet token encrypted with ``secret.key`` (both live next to each other in
``DATA_DIR``); the browser only ever sees a masked view. This module owns the whole
lifecycle: key creation, validation, atomic persistence, masking, and resolving the
printer a request should talk to.

Concurrency: one process (uvicorn ``workers=1``). A single ``RLock`` guards
reload -> mutate -> write, and every public read reloads first so a hand edit of the file
is picked up without a restart.

Failure policy: a ``printers.json`` this build cannot parse is never overwritten. The
store reports it through ``corrupt``, reads come back empty and every write raises
``PrinterStoreError`` — the app still boots so the operator can fix the file.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

PRINTERS_VERSION = 2
PROFILE_SLOTS = ("machine", "process")
NAME_MAX = 60
URL_MAX = 300
API_KEY_MAX = 256
ENV_PRINTER_ID = "env"
KEY_FILE_MODE = 0o600
MASK = "\u2022" * 8
PRINTER_ID_RE = re.compile(r"[0-9a-f]{12}")
SCHEMES = ("http", "https")

DECRYPT_ERROR = ("stored API key cannot be decrypted with this key file; "
                 "re-enter the key in Settings")
READ_ONLY_ERROR = ("the environment printer is read-only: change PRINTER_URL, "
                   "MOONRAKER_API_KEY and PRINTER_UI_URL in the deployment instead")
PROFILES_SHAPE_ERROR = "profiles must be an object with machine/process paths"
PROFILE_PATH_ERROR = "profile path must be relative to the datadir"


class PrinterError(RuntimeError):
    """A printer field the operator can fix (HTTP 400)."""


class PrinterNotFound(PrinterError):
    """No persisted printer with that id (HTTP 404)."""


class PrinterReadOnly(PrinterError):
    """The environment printer's credentials cannot be edited from the UI (HTTP 409)."""


class PrinterStoreError(RuntimeError):
    """``printers.json`` or ``secret.key`` is unusable (HTTP 409)."""


@dataclass(frozen=True, slots=True)
class Printer:
    """One persisted printer. ``api_key_enc`` is a Fernet token, never the key itself.

    ``profiles`` is the optional machine/process pairing as datadir-relative ``.json``
    paths; ``None`` means the printer inherits the global selection in ``profile.json``.
    ``ui_url`` is the optional Fluidd/Mainsail page the **Printer** tab frames in the
    browser; ``""`` means the printer has no embedded UI of its own.
    """
    id: str
    name: str
    url: str
    api_key_enc: str
    created: str
    updated: str
    profiles: dict[str, str] | None = None
    ui_url: str = ""


@dataclass(frozen=True, slots=True)
class SeededPrinter:
    """Read-only printer that comes from the environment; never persisted."""
    id: str
    name: str
    url: str
    api_key: str
    ui_url: str = ""


@dataclass(frozen=True, slots=True)
class PrintTarget:
    """Request-scoped resolution of the active printer (plaintext key)."""
    id: str
    name: str
    url: str
    api_key: str
    source: str
    ui_url: str = ""


# ---------------------------------------------------------------- crypto / masking
def encrypt_secret(plaintext: str, key: bytes) -> str:
    return Fernet(key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str, key: bytes) -> str:
    """Decrypt a stored key; a token from another key file is an operator error, not a bug."""
    try:
        return Fernet(key).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise PrinterStoreError(DECRYPT_ERROR) from exc


def load_or_create_key(path: Path) -> bytes:
    """Return the Fernet key at ``path``, creating it once with mode 0600 if missing.

    Two processes may race on first start: the loser of ``O_EXCL`` reads the winner's key
    instead of overwriting it, so keys already in ``printers.json`` stay decryptable.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _read_key(path)
    except FileNotFoundError:
        pass
    key = Fernet.generate_key()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_FILE_MODE)
    except FileExistsError:
        return _read_key(path)
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, KEY_FILE_MODE)
    return key


def _read_key(path: Path) -> bytes:
    """Read and validate an existing key file; ``FileNotFoundError`` reaches the caller."""
    key = path.read_bytes()
    try:
        Fernet(key)
    except (ValueError, TypeError) as exc:
        raise PrinterStoreError(
            f"{path.name} is not a valid Fernet key: restore the original file or remove it "
            "to generate a new one (every stored API key then has to be re-entered)"
        ) from exc
    os.chmod(path, KEY_FILE_MODE)
    return key


def mask_key(key: str | None) -> str:
    """The only shape a key may take outside the process: fixed mask + last 4 characters."""
    text = key or ""
    if len(text) <= 8:
        return MASK
    return MASK + text[-4:]


# ---------------------------------------------------------------- input normalisation
def normalise_name(raw: object) -> str:
    """Collapse whitespace, then require 1-60 printable characters.

    Control characters are rejected with the same message as an empty name: the UI shows
    one line per field, and ``\\n``/``\\t`` are already folded by the whitespace collapse.
    """
    name = " ".join(str(raw or "").split())
    if not name or len(name) > NAME_MAX or _has_control(name):
        raise PrinterError(f"name must be 1-{NAME_MAX} characters")
    return name


def normalise_url(raw: object) -> str:
    """A Moonraker base URL: http(s), a host, no credentials, no query or fragment.

    Path prefixes are kept (``https://host/moonraker``); one trailing slash is dropped so
    the stored value matches what ``MoonrakerClient`` builds its requests from.
    """
    url = str(raw or "").strip()
    if not url:
        raise PrinterError("url is required")
    if any(char.isspace() for char in url):
        raise PrinterError("url must not contain whitespace")
    if len(url) > URL_MAX:
        raise PrinterError(f"url must be at most {URL_MAX} characters")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise PrinterError("url is not a valid URL") from exc
    if parts.scheme.lower() not in SCHEMES:
        raise PrinterError("url must start with http:// or https://")
    if parts.username or parts.password:
        raise PrinterError("url must not contain credentials")
    if parts.query or parts.fragment:
        raise PrinterError("url must not have a query or fragment")
    if not parts.hostname:
        raise PrinterError("url must include a host")
    return url.rstrip("/")


def normalise_ui_url(raw: object, *, field: str = "ui_url") -> str:
    """The browser-facing Fluidd/Mainsail page framed by the **Printer** tab.

    Blank is a valid value (no embedded UI). A non-blank one must be ``http(s)`` with a
    host and without credentials, a query or a fragment, because it is handed to an
    ``<iframe src>`` in the operator's browser — the portal never fetches it, so this is
    shape-only validation. One trailing slash is dropped so the stored value matches what
    the tab navigates to. ``field`` only names the value in the message, which lets the
    environment parser (``config.parse_ui_url``) share this single implementation.
    """
    url = str(raw or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        raise PrinterError(f"{field} must start with http:// or https:// (got {raw!r})")
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError as exc:
        raise PrinterError(f"{field} is not a usable URL: {raw!r} ({exc})") from None
    if not parts.hostname:
        raise PrinterError(f"{field} must name a host (got {raw!r})")
    if parts.username is not None or parts.password is not None:
        raise PrinterError(f"{field} must not embed credentials (got {raw!r})")
    if parts.query or "?" in url:
        raise PrinterError(f"{field} must not carry a query string (got {raw!r})")
    if parts.fragment or "#" in url:
        raise PrinterError(f"{field} must not carry a fragment (got {raw!r})")
    return url.rstrip("/")


def normalise_api_key(raw: object) -> str:
    key = str(raw or "").strip()
    if not key:
        raise PrinterError("api_key is required")
    if _has_control(key):
        raise PrinterError("api_key must not contain control characters")
    if len(key) > API_KEY_MAX:
        raise PrinterError(f"api_key must be at most {API_KEY_MAX} characters")
    return key


def _has_control(text: str) -> bool:
    return any(char < " " or char == "\x7f" for char in text)


def normalise_profiles(raw: object) -> dict[str, str] | None:
    """Shape-only validation of a printer's profile pairing (this module has no datadir).

    ``None``, ``{}`` and a dict whose slots are all ``None`` all mean "inherit the global
    profile selection", so they normalise to ``None``. A slot value has to be a usable
    datadir-relative ``.json`` path; whether that preset exists is checked by the API
    layer, which knows ``CONFIG.orca_datadir``.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PrinterError(PROFILES_SHAPE_ERROR)
    unknown = [key for key in raw if key not in PROFILE_SLOTS]
    if unknown:
        raise PrinterError(f"unknown profile slot: {unknown[0]!r}")
    profiles: dict[str, str] = {}
    for slot in PROFILE_SLOTS:
        value = raw.get(slot)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise PrinterError(PROFILES_SHAPE_ERROR)
        profiles[slot] = normalise_profile_path(value)
    return profiles or None


def normalise_profile_path(raw: str) -> str:
    """A preset path relative to the datadir: POSIX separators, no ``..``, ends in ``.json``."""
    if (raw.startswith("/") or "\\" in raw or not raw.endswith(".json")
            or any(part in ("", ".", "..") for part in raw.split("/"))):
        raise PrinterError(PROFILE_PATH_ERROR)
    return raw


# ---------------------------------------------------------------- persistence helpers
def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON through a unique sibling temp file, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp",
                                         delete=False) as handle:
            tmp = Path(handle.name)
            os.chmod(tmp, KEY_FILE_MODE)
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise


def _parse_printer(entry: object, version: int) -> Printer | None:
    """Validate one stored printer; ``None`` means the document is unusable.

    Only version 2 records carry a pairing: a version 1 record parses with
    ``profiles=None``, and a stray ``profiles`` key there is dropped, not trusted.
    """
    if not isinstance(entry, dict):
        return None
    fields: dict[str, str] = {}
    for field in ("id", "name", "url", "api_key_enc", "created", "updated"):
        value = entry.get(field)
        if not isinstance(value, str) or not value:
            return None
        fields[field] = value
    if not PRINTER_ID_RE.fullmatch(fields["id"]):
        return None
    profiles = normalise_profiles(entry.get("profiles")) if version >= 2 else None
    return Printer(**fields, profiles=profiles, ui_url=_stored_ui_url(entry.get("ui_url")))


def _stored_ui_url(raw: object) -> str:
    """The stored embedded-UI address, ``""`` when it is absent or unusable.

    Absent is the normal case: every ``printers.json`` written before this field existed
    has no ``ui_url`` key, and that must read as "no embedded UI" rather than as a corrupt
    document. An unusable value is treated the same way, for the reason ``env_profiles``
    is: it is one optional address, and blanking Settings (or refusing every write) over
    it is worse than publishing no frame. Retyping it in Settings replaces it.
    """
    if not isinstance(raw, str):
        return ""
    try:
        return normalise_ui_url(raw)
    except PrinterError:
        return ""


def _parse_env_profiles(data: dict, version: int) -> dict[str, str] | None:
    """The optional ``env_profiles`` pairing, or ``None`` when it is absent or unusable.

    Unlike a stored printer's pairing, a malformed ``env_profiles`` is not a corrupt
    document: it is one user preference, and reading it has to stay read-safe rather than
    blank the whole Settings page (and block every write) over it.
    """
    if version < 2:
        return None
    try:
        return normalise_profiles(data.get("env_profiles"))
    except PrinterError:
        return None


def _parse_document(
        data: object) -> tuple[list[Printer], str | None, dict[str, str] | None, str | None]:
    """Return ``(printers, selected_id, env_profiles, error)`` for a decoded ``printers.json``.

    Versions 1 and 2 load; a version 1 document reads with ``profiles=None`` and is only
    rewritten as version 2 by the next mutation, so reading never migrates the file.
    """
    if not isinstance(data, dict):
        return [], None, None, "printers.json must contain a JSON object"
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return [], None, None, f"printers.json has no integer version (found {version!r})"
    if version > PRINTERS_VERSION:
        return [], None, None, f"printers.json version {version} is newer than this build"
    if version not in (1, PRINTERS_VERSION):
        return [], None, None, (f"printers.json version {version} is not supported "
                                f"(this build writes version {PRINTERS_VERSION})")
    selected = data.get("selected_id")
    if selected == "":
        selected = None
    if selected is not None and not isinstance(selected, str):
        return [], None, None, "printers.json selected_id must be a string or null"
    entries = data.get("printers", [])
    if not isinstance(entries, list):
        return [], None, None, "printers.json printers must be a list"
    printers = []
    for entry in entries:
        try:
            printer = _parse_printer(entry, version)
        except PrinterError as exc:
            return [], None, None, ("printers.json contains an invalid profile pairing: "
                                    f"{exc}")
        if printer is None:
            return [], None, None, (
                "printers.json contains a printer record with missing fields")
        printers.append(printer)
    return printers, selected, _parse_env_profiles(data, version), None


# ---------------------------------------------------------------- store
_UNCHANGED = object()      # ``update(profiles=...)`` without a pairing argument


def load_printers(path: Path, key_file: Path | None = None) -> PrinterStore:
    """Open the store at ``path``; ``key_file`` defaults to ``secret.key`` next to it."""
    return PrinterStore.open(path, key_file)


class PrinterStore:
    """CRUD over ``printers.json``: reload, validate, mutate in memory, write atomically."""

    def __init__(self, path: Path, key: bytes) -> None:
        self.path = Path(path)
        self.corrupt: str | None = None
        self._lock = threading.RLock()
        self._printers: list[Printer] = []
        self._selected_id: str | None = None
        self._env_profiles: dict[str, str] | None = None
        self._key = bytes(key)
        try:
            Fernet(self._key)
        except (ValueError, TypeError) as exc:
            raise PrinterStoreError(
                "secret.key is not a valid Fernet key: restore the original file "
                "(every stored API key then has to be re-entered)") from exc
        self._read()

    @classmethod
    def open(cls, path: Path, key_file: Path | None = None) -> PrinterStore:
        path = Path(path)
        key_path = Path(key_file) if key_file is not None else path.with_name("secret.key")
        return cls(path, load_or_create_key(key_path))

    # -- loading
    def reload(self) -> PrinterStore:
        """Re-read the file. An unreadable file keeps the last good state and blocks writes."""
        with self._lock:
            self._read()
        return self

    def _read(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.corrupt = None
            self._printers = []
            self._selected_id = None
            self._env_profiles = None
            return
        except OSError as exc:
            self.corrupt = f"printers.json cannot be read: {exc.strerror or exc}"
            return
        try:
            data = json.loads(raw)
        except ValueError:
            self.corrupt = ("printers.json is not valid JSON: fix or remove it in the data "
                            "directory (it is never overwritten while unreadable)")
            return
        printers, selected, env_profiles, error = _parse_document(data)
        if error is not None:
            self.corrupt = error
            return
        self._printers = printers
        self._selected_id = selected
        self._env_profiles = env_profiles
        self.corrupt = None

    def _require_writable(self) -> None:
        if self.corrupt is not None:
            raise PrinterStoreError(self.corrupt)

    def _write(self) -> None:
        self._require_writable()
        _atomic_write(self.path, {
            "version": PRINTERS_VERSION,
            "selected_id": self._selected_id,
            "env_profiles": self._env_profiles,
            "printers": [asdict(printer) for printer in self._printers],
        })

    def _index_of(self, printer_id: str) -> int:
        for index, printer in enumerate(self._printers):
            if printer.id == printer_id:
                return index
        raise PrinterNotFound(f"printer {printer_id!r} not found")

    def _new_id(self) -> str:
        taken = {printer.id for printer in self._printers}
        while True:
            candidate = secrets.token_hex(6)
            if candidate not in taken:
                return candidate

    # -- reads
    def all(self) -> list[Printer]:
        """Persisted printers in file order; a corrupt file reads as empty."""
        with self._lock:
            self._read()
            if self.corrupt is not None:
                return []
            return list(self._printers)

    def get(self, printer_id: str) -> Printer:
        with self._lock:
            self._read()
            for printer in self._printers:
                if printer.id == printer_id:
                    return printer
        raise PrinterNotFound(f"printer {printer_id!r} not found")

    def selected_id(self) -> str | None:
        with self._lock:
            self._read()
            if self.corrupt is not None:
                return None
            return self._selected_id

    def api_key(self, printer: Printer) -> str:
        """Decrypt one stored key; fails closed when the file or the key file is unusable."""
        with self._lock:
            self._read()
            self._require_writable()
            return decrypt_secret(printer.api_key_enc, self._key)

    def views(self, seed: SeededPrinter | None = None) -> list[dict]:
        """The browser-safe shape: store printers first, then the env seed.

        ``selected`` marks the row ``active()`` resolves to, not the raw stored
        ``selected_id``: a missing, env, or stale selection still points at whichever
        printer a request would use, so the UI's active chip never disappears.
        ``editable`` gates the deployment-owned fields, ``pairing_editable`` the
        machine/process pairing — which the operator owns on the env row too.
        """
        with self._lock:
            self._read()
            if self.corrupt is not None:
                return []
            selected = self._active_id(seed)
            views = []
            for printer in self._printers:
                views.append({
                    "id": printer.id,
                    "name": printer.name,
                    "url": printer.url,
                    "ui_url": printer.ui_url,
                    "api_key_masked": self._masked(printer),
                    "has_api_key": True,
                    "selected": printer.id == selected,
                    "source": "store",
                    "editable": True,
                    "pairing_editable": True,
                    "profiles": self._pairing(printer),
                    "created": printer.created,
                    "updated": printer.updated,
                })
            if seed is not None:
                views.append({
                    "id": ENV_PRINTER_ID,
                    "name": seed.name,
                    "url": seed.url,
                    "ui_url": seed.ui_url,
                    "api_key_masked": mask_key(seed.api_key),
                    "has_api_key": True,
                    "selected": selected == ENV_PRINTER_ID,
                    "source": "env",
                    "editable": False,
                    "pairing_editable": True,
                    "profiles": _pairing_shape(self._env_profiles),
                    "created": None,
                    "updated": None,
                })
            return views

    def active(self, seed: SeededPrinter | None = None) -> PrintTarget | None:
        """The printer a request should use: selection, then env selection, then fallbacks."""
        with self._lock:
            self._read()
            active_id = self._active_id(seed)
            if active_id is None:
                return None
            for printer in self._printers:
                if printer.id == active_id:
                    return self._target(printer)
            if seed is not None:               # the only other id is the env seed
                return _seed_target(seed)
            return None

    def _active_id(self, seed: SeededPrinter | None) -> str | None:
        """The one resolution rule behind both ``active()`` and ``views()``.

        Caller holds the lock and has reloaded. Stored selection first, then the env
        selection (only while a seed exists), then the first stored printer, then the
        seed. Computed from ids alone so masking a view never has to decrypt a key.
        """
        if self.corrupt is not None:
            return ENV_PRINTER_ID if seed is not None else None
        for printer in self._printers:
            if printer.id == self._selected_id:
                return printer.id
        if self._selected_id == ENV_PRINTER_ID and seed is not None:
            return ENV_PRINTER_ID
        if self._printers:
            return self._printers[0].id
        if seed is not None:
            return ENV_PRINTER_ID
        return None

    def _target(self, printer: Printer) -> PrintTarget:
        return PrintTarget(id=printer.id, name=printer.name, url=printer.url,
                           api_key=decrypt_secret(printer.api_key_enc, self._key),
                           source="store", ui_url=printer.ui_url)

    def _pairing(self, printer: Printer) -> dict[str, str | None]:
        return _pairing_shape(printer.profiles)

    def _masked(self, printer: Printer) -> str:
        """Bullets only when the token does not match this key file, never a failure."""
        try:
            return mask_key(decrypt_secret(printer.api_key_enc, self._key))
        except PrinterStoreError:
            return MASK

    # -- mutations
    def add(self, name: object, url: object, api_key: object, *, profiles: object = None,
            ui_url: object = "") -> Printer:
        """Append a printer; the first one in an empty store becomes the selection.

        ``profiles`` is the optional machine/process pairing (datadir-relative paths);
        ``None`` or an all-``None`` dict stores no pairing, i.e. the printer inherits the
        global profile selection. ``ui_url`` is the optional embedded Fluidd/Mainsail page
        (``""`` = none).
        """
        name = normalise_name(name)
        url = normalise_url(url)
        api_key = normalise_api_key(api_key)
        pairing = normalise_profiles(profiles)
        embedded_ui = normalise_ui_url(ui_url)
        with self._lock:
            self._read()
            self._require_writable()
            stamp = _timestamp()
            printer = Printer(id=self._new_id(), name=name, url=url,
                              api_key_enc=encrypt_secret(api_key, self._key),
                              created=stamp, updated=stamp, profiles=pairing,
                              ui_url=embedded_ui)
            first = not self._printers
            self._printers.append(printer)
            if first:
                self._selected_id = printer.id
            self._write()
            return printer

    def update(self, printer_id: str, *, name: object = None, url: object = None,
               api_key: object = None, profiles: object = _UNCHANGED,
               ui_url: object = _UNCHANGED) -> Printer:
        """Change the supplied fields; an omitted API key keeps the stored token.

        ``profiles`` tells "leave the pairing alone" apart from "clear it": the
        ``_UNCHANGED`` sentinel default keeps the stored pairing, ``None`` / ``{}`` / an
        all-``None`` dict clears it to ``None`` (the printer then inherits the global
        profile selection), and a supplied dict *is* the complete pairing — a slot left
        out of it is cleared, so this is a replace, not a merge. ``ui_url`` follows the
        same sentinel rule, with ``""`` the explicit "no embedded UI".
        """
        with self._lock:
            self._read()
            self._require_writable()
            if printer_id == ENV_PRINTER_ID:
                raise PrinterReadOnly(READ_ONLY_ERROR)
            index = self._index_of(printer_id)
            if (name is None and url is None and api_key is None
                    and profiles is _UNCHANGED and ui_url is _UNCHANGED):
                raise PrinterError("nothing to update")
            current = self._printers[index]
            updated = Printer(
                id=current.id,
                name=normalise_name(name) if name is not None else current.name,
                url=normalise_url(url) if url is not None else current.url,
                api_key_enc=(encrypt_secret(normalise_api_key(api_key), self._key)
                             if api_key is not None else current.api_key_enc),
                created=current.created,
                updated=_timestamp(),
                profiles=(current.profiles if profiles is _UNCHANGED
                          else normalise_profiles(profiles)),
                ui_url=(current.ui_url if ui_url is _UNCHANGED
                        else normalise_ui_url(ui_url)),
            )
            self._printers[index] = updated
            self._write()
            return updated

    def delete(self, printer_id: str) -> str | None:
        """Remove a printer and return the selection that is in force afterwards."""
        with self._lock:
            self._read()
            self._require_writable()
            if printer_id == ENV_PRINTER_ID:
                raise PrinterReadOnly(READ_ONLY_ERROR)
            removed = self._printers.pop(self._index_of(printer_id))
            if self._selected_id == removed.id:
                self._selected_id = self._printers[0].id if self._printers else None
            self._write()
            return self._selected_id

    def select(self, printer_id: str | None) -> None:
        """Persist the active printer; ``None`` clears it, ``env`` selects the seed."""
        with self._lock:
            self._read()
            self._require_writable()
            if printer_id is not None and printer_id != ENV_PRINTER_ID:
                self._index_of(printer_id)
            if printer_id == self._selected_id:
                return
            self._selected_id = printer_id
            self._write()

    def set_env_pairing(self, profiles: object) -> dict[str, str | None]:
        """Store the env seed's machine/process pairing and return it as viewed.

        Only the pairing is writable: the seed's name, URL and API key come from the
        deployment, so ``update()`` keeps refusing the env id. ``profiles`` follows the
        same replace-not-merge rule as ``update(profiles=...)``; the stored pairing comes
        back in the every-slot shape ``views()`` reports, ``None`` meaning "inherit".
        """
        pairing = normalise_profiles(profiles)
        with self._lock:
            self._read()
            self._require_writable()
            self._env_profiles = pairing
            self._write()
        return _pairing_shape(pairing)

    def merge_env_pairing(self, slots: object) -> dict[str, str | None]:
        """Update the supplied slots of the env pairing in one locked read-modify-write.

        The dashboard write-back touches one slot at a time on top of what is already
        stored, so a separate read followed by ``set_env_pairing()`` let a concurrent
        ``PATCH`` of that row land between the two and be overwritten with the copy the
        reader had already seen. Here the read, the merge and the write share one critical
        section, as in ``update()``; slots the caller leaves out keep their stored value.
        A corrupt document is refused, never repaired over.
        """
        pairing = normalise_profiles(slots) or {}
        with self._lock:
            self._read()
            self._require_writable()
            merged = {**(self._env_profiles or {}), **pairing}
            self._env_profiles = merged or None
            self._write()
            return _pairing_shape(self._env_profiles)


def _pairing_shape(stored: dict[str, str] | None) -> dict[str, str | None]:
    """The pairing shape the UI edits: every slot present, ``None`` = inherit."""
    stored = stored or {}
    return {slot: stored.get(slot) for slot in PROFILE_SLOTS}


def _seed_target(seed: SeededPrinter) -> PrintTarget:
    return PrintTarget(id=seed.id, name=seed.name, url=seed.url, api_key=seed.api_key,
                       source="env", ui_url=seed.ui_url)
