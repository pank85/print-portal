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

PRINTERS_VERSION = 1
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
READ_ONLY_ERROR = ("the environment printer is read-only: change PRINTER_URL and "
                   "MOONRAKER_API_KEY in the deployment instead")


class PrinterError(RuntimeError):
    """A printer field the operator can fix (HTTP 400)."""


class PrinterNotFound(PrinterError):
    """No persisted printer with that id (HTTP 404)."""


class PrinterReadOnly(PrinterError):
    """The environment printer cannot be edited from the UI (HTTP 409)."""


class PrinterStoreError(RuntimeError):
    """``printers.json`` or ``secret.key`` is unusable (HTTP 409)."""


@dataclass(frozen=True, slots=True)
class Printer:
    """One persisted printer. ``api_key_enc`` is a Fernet token, never the key itself."""
    id: str
    name: str
    url: str
    api_key_enc: str
    created: str
    updated: str


@dataclass(frozen=True, slots=True)
class SeededPrinter:
    """Read-only printer that comes from the environment; never persisted."""
    id: str
    name: str
    url: str
    api_key: str


@dataclass(frozen=True, slots=True)
class PrintTarget:
    """Request-scoped resolution of the active printer (plaintext key)."""
    id: str
    name: str
    url: str
    api_key: str
    source: str


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


def _parse_printer(entry: object) -> Printer | None:
    """Validate one stored printer; ``None`` means the document is unusable."""
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
    return Printer(**fields)


def _parse_document(data: object) -> tuple[list[Printer], str | None, str | None]:
    """Return ``(printers, selected_id, error)`` for a decoded ``printers.json``."""
    if not isinstance(data, dict):
        return [], None, "printers.json must contain a JSON object"
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return [], None, f"printers.json has no integer version (found {version!r})"
    if version > PRINTERS_VERSION:
        return [], None, f"printers.json version {version} is newer than this build"
    if version != PRINTERS_VERSION:
        return [], None, (f"printers.json version {version} is not supported "
                          f"(this build writes version {PRINTERS_VERSION})")
    selected = data.get("selected_id")
    if selected == "":
        selected = None
    if selected is not None and not isinstance(selected, str):
        return [], None, "printers.json selected_id must be a string or null"
    entries = data.get("printers", [])
    if not isinstance(entries, list):
        return [], None, "printers.json printers must be a list"
    printers = []
    for entry in entries:
        printer = _parse_printer(entry)
        if printer is None:
            return [], None, "printers.json contains a printer record with missing fields"
        printers.append(printer)
    return printers, selected, None


# ---------------------------------------------------------------- store
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
        printers, selected, error = _parse_document(data)
        if error is not None:
            self.corrupt = error
            return
        self._printers = printers
        self._selected_id = selected
        self.corrupt = None

    def _require_writable(self) -> None:
        if self.corrupt is not None:
            raise PrinterStoreError(self.corrupt)

    def _write(self) -> None:
        self._require_writable()
        _atomic_write(self.path, {
            "version": PRINTERS_VERSION,
            "selected_id": self._selected_id,
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
        """The browser-safe shape: store printers first, then the read-only env seed.

        ``selected`` marks the row ``active()`` resolves to, not the raw stored
        ``selected_id``: a missing, env, or stale selection still points at whichever
        printer a request would use, so the UI's active chip never disappears.
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
                    "api_key_masked": self._masked(printer),
                    "has_api_key": True,
                    "selected": printer.id == selected,
                    "source": "store",
                    "editable": True,
                    "created": printer.created,
                    "updated": printer.updated,
                })
            if seed is not None:
                views.append({
                    "id": ENV_PRINTER_ID,
                    "name": seed.name,
                    "url": seed.url,
                    "api_key_masked": mask_key(seed.api_key),
                    "has_api_key": True,
                    "selected": selected == ENV_PRINTER_ID,
                    "source": "env",
                    "editable": False,
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
                           source="store")

    def _masked(self, printer: Printer) -> str:
        """Bullets only when the token does not match this key file, never a failure."""
        try:
            return mask_key(decrypt_secret(printer.api_key_enc, self._key))
        except PrinterStoreError:
            return MASK

    # -- mutations
    def add(self, name: object, url: object, api_key: object) -> Printer:
        """Append a printer; the first one in an empty store becomes the selection."""
        name = normalise_name(name)
        url = normalise_url(url)
        api_key = normalise_api_key(api_key)
        with self._lock:
            self._read()
            self._require_writable()
            stamp = _timestamp()
            printer = Printer(id=self._new_id(), name=name, url=url,
                              api_key_enc=encrypt_secret(api_key, self._key),
                              created=stamp, updated=stamp)
            first = not self._printers
            self._printers.append(printer)
            if first:
                self._selected_id = printer.id
            self._write()
            return printer

    def update(self, printer_id: str, *, name: object = None, url: object = None,
               api_key: object = None) -> Printer:
        """Change the supplied fields; an omitted API key keeps the stored token."""
        with self._lock:
            self._read()
            self._require_writable()
            if printer_id == ENV_PRINTER_ID:
                raise PrinterReadOnly(READ_ONLY_ERROR)
            index = self._index_of(printer_id)
            if name is None and url is None and api_key is None:
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


def _seed_target(seed: SeededPrinter) -> PrintTarget:
    return PrintTarget(id=seed.id, name=seed.name, url=seed.url, api_key=seed.api_key,
                       source="env")
