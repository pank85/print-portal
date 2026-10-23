"""Optional designed-in support fins via the upstream support-fins CLI.

This is an opt-in preprocessor: the original upload is preserved and, when enabled,
a job-local finned 3MF is produced and used as the slicer input. Nothing is sent
outside the container; the bundled CLI runs locally under Node.

Two properties matter more than the geometry here:

* **Serialised.** A ``node`` run of the bundled bundle holds ~300 MB RSS, so uploads
  that ask for fins queue on a module-level ``BoundedSemaphore(1)`` instead of
  spawning one process per concurrent request.
* **Bounded and honest.** The CLI is given ``--json`` so the caller can record what it
  actually did (``report`` + ``stats``), and ``--json`` is the only thing parsed: a
  non-zero exit is reported with the CLI's own text (exit ``1`` = a file failed,
  exit ``2`` = bad arguments) instead of a generic failure.

Nothing here writes outside ``dest``, and the CLI itself refuses to overwrite its input.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import threading
from pathlib import Path

CLI = Path(__file__).with_name("support_fins.mjs")

# Formats the upstream CLI can read as a single part. Project/preserve modes are
# intentionally not exposed here.
SUPPORTED = {".stl", ".3mf", ".obj"}

DEFAULT_TIMEOUT = 300.0
"""Seconds a single fin run may take; ``SUPPORT_FINS_TIMEOUT`` overrides it."""

_RUN_LOCK = threading.BoundedSemaphore(1)
"""One CLI process at a time: a wasm-backed run is ~300 MB, so concurrency is a risk."""

_NODE = "node"


class SupportFinsError(RuntimeError):
    """The fin run could not produce a usable 3MF; the message is shown to the operator."""


class SupportFinsUnavailable(SupportFinsError):
    """Node or the bundled CLI is missing, so fins cannot be offered at all."""


def available() -> bool:
    """True when this image can actually run the bundled CLI."""
    return CLI.is_file() and shutil.which(_NODE) is not None


def unavailable_reason() -> str:
    """Why :func:`available` is False, or ``""`` when fins are available."""
    if not CLI.is_file():
        return "support-fins CLI is missing from the image"
    if shutil.which(_NODE) is None:
        return "node runtime is not available"
    return ""


def _timeout_value(timeout: float | None) -> float:
    """``timeout`` in seconds, defaulted and validated (finite and positive)."""
    if timeout is None:
        return DEFAULT_TIMEOUT
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        raise SupportFinsError(
            f"support fins timeout must be a positive number (got {timeout!r})") from None
    if not 0 < value < math.inf:
        raise SupportFinsError(
            f"support fins timeout must be a positive number (got {timeout!r})")
    return value


def _detail(proc: subprocess.CompletedProcess[str]) -> str:
    """The CLI's own failure text, stderr preferred, stripped of blank lines."""
    for blob in (proc.stderr, proc.stdout):
        text = (blob or "").strip()
        if text:
            return text
    return ""


def _result(stdout: str) -> dict:
    """``{"report": str, "stats": dict}`` parsed from the CLI's ``--json`` line.

    ``--json`` writes exactly one object per file, but a warning printed before it must
    not turn a successful run into an error: the last line that parses as an object with
    a ``stats`` or ``report`` key wins, and an unparsable stream degrades to the plain
    text the CLI would have printed.
    """
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        stats = payload.get("stats")
        report = payload.get("report")
        if stats is None and report is None:
            continue
        return {
            "report": report if isinstance(report, str) else "",
            "stats": stats if isinstance(stats, dict) else {},
        }
    lines = (stdout or "").strip().splitlines()
    return {"report": lines[-1].strip() if lines else "", "stats": {}}


def fin_model(src: Path, dest: Path, *, timeout: float | None = None) -> dict:
    """Run the bundled CLI on ``src`` and write the finned 3MF to ``dest``.

    Returns ``{"report": str, "stats": {...}}`` — what the CLI says it did. Raises
    :class:`SupportFinsUnavailable` when node/the CLI is missing, and
    :class:`SupportFinsError` for a bad suffix, a timeout, a non-zero exit or an empty
    output file.
    """
    reason = unavailable_reason()
    if reason:
        raise SupportFinsUnavailable(reason)
    if src.suffix.lower() not in SUPPORTED:
        raise SupportFinsError(f"support fins does not support '{src.suffix}' uploads")
    limit = _timeout_value(timeout)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with _RUN_LOCK:
        if dest.exists():
            dest.unlink()
        try:
            proc = subprocess.run(
                [_NODE, str(CLI), str(src), "-o", str(dest), "--json"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=limit,
            )
        except subprocess.TimeoutExpired as exc:
            raise SupportFinsError(f"support fins timed out after {limit:g}s") from exc
        except FileNotFoundError as exc:  # node vanished between available() and the run
            raise SupportFinsUnavailable("node runtime is not available") from exc
        except OSError as exc:
            raise SupportFinsError(f"support fins could not start the CLI: {exc}") from exc

    if proc.returncode == 2:
        raise SupportFinsError(_detail(proc) or "support-fins rejected its arguments")
    if proc.returncode != 0:
        # exit 1 = this file failed; the CLI's own text names the reason
        raise SupportFinsError(_detail(proc) or "support-fins failed")
    if not dest.is_file() or dest.stat().st_size == 0:
        raise SupportFinsError(_detail(proc) or "support-fins produced no output")
    return _result(proc.stdout)
