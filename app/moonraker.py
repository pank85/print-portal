#!/usr/bin/env python3
"""Moonraker HTTP client: reachability, live printer status, gcode upload, print start.

Everything here is stdlib ``urllib`` - the container needs no SSH key and no access to
the printer host's filesystem.

Endpoints
    GET  {base}/server/info
    GET  {base}/printer/objects/query?print_stats&webhooks&virtual_sdcard&extruder&heater_bed&toolhead
    POST {base}/server/files/upload            (multipart: root=<root>, file=<gcode>)
    POST {base}/printer/print/start?filename=<name>

Every request carries ``X-Api-Key``. Success is Moonraker's ``result`` envelope; failures
are mapped onto ``MoonrakerError`` subclasses whose ``http_status`` the API layer returns
unchanged, so the UI stays actionable instead of seeing a generic 500.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "API_KEY_HEADER",
    "DEFAULT_OBJECTS",
    "DEFAULT_TIMEOUT",
    "UPLOAD_TIMEOUT",
    "MoonrakerError",
    "MoonrakerUnreachable",
    "MoonrakerAuthError",
    "MoonrakerFileMissing",
    "MoonrakerBusy",
    "MoonrakerClient",
]

DEFAULT_OBJECTS = ("print_stats", "webhooks", "virtual_sdcard", "extruder", "heater_bed", "toolhead")
DEFAULT_TIMEOUT = 10.0
UPLOAD_TIMEOUT = 300.0
API_KEY_HEADER = "X-Api-Key"


class MoonrakerError(Exception):
    """Any Moonraker failure. ``http_status`` is what the endpoint returns to the UI."""

    http_status: int = 502

    def __init__(self, message: str, *, http_code: int | None = None) -> None:
        super().__init__(message)
        self.http_code = http_code


class MoonrakerUnreachable(MoonrakerError):
    http_status = 503


class MoonrakerAuthError(MoonrakerError):
    http_status = 502


class MoonrakerFileMissing(MoonrakerError):
    http_status = 502


class MoonrakerBusy(MoonrakerError):
    http_status = 409


def _quote_query(params: dict[str, str] | None) -> str:
    """``{"filename": "a b.gcode"}`` -> ``filename=a%20b.gcode`` (quote with safe="")."""
    if not params:
        return ""
    return "&".join(
        f"{key}={urllib.parse.quote(str(value), safe='')}" for key, value in params.items()
    )


def _payload_error(payload: dict[str, Any]) -> str:
    """Message text out of Moonraker's ``{"error": {...}}`` object."""
    err = payload.get("error")
    if isinstance(err, dict):
        text = err.get("message") or err.get("error") or json.dumps(err)[:300]
        code = err.get("code")
        return f"Moonraker error {code}: {text}" if code is not None else f"Moonraker error: {text}"
    return f"Moonraker error: {err}"


def _http_detail(exc: urllib.error.HTTPError) -> str:
    """Best-effort error text from a non-2xx response body (Moonraker sends JSON)."""
    try:
        raw = exc.read()
    except OSError:
        raw = b""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return str(exc.reason or "")[:300]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:300]
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("error") or json.dumps(err))[:300]
        if err is not None:
            return str(err)[:300]
    return text[:300]


class MoonrakerClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        root: str = "gcodes",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.root = root
        self.timeout = timeout

    def __repr__(self) -> str:
        # The API key must never reach a log line.
        return f"MoonrakerClient({self.base_url!r}, root={self.root!r})"

    # ------------------------------------------------------------ transport
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Perform one request and return the unwrapped payload; raises ``MoonrakerError``."""
        url = f"{self.base_url}{path}"
        query = _quote_query(params)
        if query:
            url += ("&" if "?" in path else "?") + query

        headers = {API_KEY_HEADER: self.api_key}
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout if timeout is not None else self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise self._error_from_http(exc) from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise MoonrakerUnreachable(
                f"printer unreachable at {self.base_url}: {type(exc).__name__}: {reason}"
            ) from exc

        text = raw.decode("utf-8", "replace").strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MoonrakerError(
                f"{method} {path} returned invalid JSON: {text[:200]!r}", http_code=502
            ) from exc
        if not isinstance(payload, dict):
            return {"result": payload}
        if "error" in payload:
            raise MoonrakerError(_payload_error(payload), http_code=502)
        return payload["result"] if "result" in payload else payload

    def _error_from_http(self, exc: urllib.error.HTTPError) -> MoonrakerError:
        code = exc.code
        detail = _http_detail(exc)
        suffix = f": {detail}" if detail else ""
        if code == 401:
            return MoonrakerAuthError(
                f"Moonraker rejected the API key (HTTP 401){suffix}. Set MOONRAKER_API_KEY to the "
                "key from Moonraker's [authorization] section (see docs/printer-setup.md).",
                http_code=code,
            )
        if code == 403:
            return MoonrakerAuthError(
                f"Moonraker denied access (HTTP 403){suffix}. Add this host to Moonraker's "
                "trusted_clients or send a valid API key.",
                http_code=code,
            )
        if code == 409:
            return MoonrakerBusy(f"printer is busy (HTTP 409){suffix}", http_code=code)
        return MoonrakerError(f"Moonraker returned HTTP {code}{suffix}", http_code=code)

    # ------------------------------------------------------------ endpoints
    def server_info(self) -> dict[str, Any]:
        return self._request("GET", "/server/info")

    def query_objects(self, objects: Sequence[str] = DEFAULT_OBJECTS) -> dict[str, Any]:
        """Object status. The query uses bare names (``?print_stats&webhooks...``, no ``=``)."""
        query = "&".join(objects)
        payload = self._request("GET", "/printer/objects/query" + (f"?{query}" if query else ""))
        status = payload.get("status") if isinstance(payload, dict) else None
        return status if isinstance(status, dict) else {}

    def printer_status(self) -> dict[str, Any]:
        """UI snapshot. Raises a ``MoonrakerError`` subclass when the printer cannot answer."""
        self.server_info()
        status = self.query_objects()
        print_stats = status.get("print_stats") or {}
        virtual_sdcard = status.get("virtual_sdcard") or {}
        extruder = status.get("extruder") or {}
        bed = status.get("heater_bed") or {}
        toolhead = status.get("toolhead") or {}
        return {
            "ok": True,
            "state": print_stats.get("state", "unknown"),
            "webhooks": (status.get("webhooks") or {}).get("state", "unknown"),
            "file": Path(virtual_sdcard.get("file_path") or "").name or None,
            "progress": round(_as_float(virtual_sdcard.get("progress")) * 100, 1),
            "duration_min": round(_as_float(print_stats.get("print_duration")) / 60, 1),
            "extruder": {
                "temp": round(_as_float(extruder.get("temperature")), 1),
                "target": round(_as_float(extruder.get("target")), 1),
            },
            "bed": {
                "temp": round(_as_float(bed.get("temperature")), 1),
                "target": round(_as_float(bed.get("target")), 1),
            },
            "homed": toolhead.get("homed_axes", ""),
            # `mcu` is deliberately not queried; keep the key so the UI shape is stable.
            "mcu_version": (status.get("mcu") or {}).get("mcu_version") or None,
            "message": (print_stats.get("message") or "")[:200],
        }

    def printer_state(self) -> dict[str, Any]:
        """Never raises: an offline/unauthorised printer becomes an actionable error string."""
        try:
            return self.printer_status()
        except MoonrakerError as exc:
            return {"ok": False, "state": "offline", "error": str(exc)}

    def upload(
        self,
        path: Path,
        remote_name: str | None = None,
        timeout: float = UPLOAD_TIMEOUT,
    ) -> dict[str, Any]:
        """Multipart upload into ``self.root``; the body is built by hand (no `requests`)."""
        name = remote_name or path.name
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise MoonrakerError(f"cannot read {path}: {exc}") from exc

        boundary = uuid.uuid4().hex
        parts = [
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="root"\r\n\r\n'
                f"{self.root}\r\n"
            ).encode(),
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + data
            + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        return self._request(
            "POST",
            "/server/files/upload",
            body=b"".join(parts),
            extra_headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=timeout,
        )

    def start_print(self, filename: str) -> dict[str, Any]:
        try:
            return self._request(
                "POST", "/printer/print/start", params={"filename": filename}
            )
        except MoonrakerError as exc:
            if exc.http_code == 404:
                raise MoonrakerFileMissing(
                    f"file did not land in the '{self.root}' root: {filename} ({exc})",
                    http_code=404,
                ) from exc
            raise


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
