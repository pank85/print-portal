"""Environment-only configuration for print-portal.

This module is the single source of truth for the env-var table, its defaults and
its validation; every other module reads ``config.load_config()`` and nothing else
touches ``os.environ``.

It fails closed: without ``MOONRAKER_API_KEY`` nothing starts, and the API key is
never printed or logged.
"""
from __future__ import annotations

import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

VERSION = "2.0.0"  # single source of truth; .gitlab-ci.yml APP_VERSION must match

DEFAULTS: dict[str, str] = {
    "PRINTER_URL": "http://127.0.0.1:7125",
    "MOONRAKER_ROOT": "gcodes",
    "PORT": "8088",
    "DATA_DIR": "/data",
    "ORCA_BIN": "/opt/orca/bin/orca-slicer",
    "MAX_UPLOAD_MB": "150",
    "KEEP_JOBS": "25",
    "SLICE_TIMEOUT": "900",
    "BED_SIZE": "220x220",
}

REQUIRED = ("MOONRAKER_API_KEY",)

REQUIRED_HINTS: dict[str, str] = {
    "MOONRAKER_API_KEY": "set it to the key from Moonraker's [authorization] section (see README)",
}

MAX_PORT = 65535
MIB = 1024 * 1024

_VAR_RE = re.compile(r"\$(?:(\w+)|\{(\w+)\})")


class ConfigError(RuntimeError):
    """Raised when the environment does not describe a runnable instance."""


@dataclass(frozen=True, slots=True)
class Config:
    printer_url: str            # no trailing slash, scheme http:// or https://
    moonraker_api_key: str
    moonraker_root: str
    port: int
    data_dir: Path
    orca_bin: Path
    orca_datadir: Path
    jobs_dir: Path              # data_dir / "jobs"
    home_dir: Path              # data_dir / "home" (HOME for the slicer subprocess)
    profile_file: Path          # data_dir / "profile.json"
    max_upload_bytes: int       # MAX_UPLOAD_MB * 1024 * 1024
    keep_jobs: int
    slice_timeout: int
    bed_size: tuple[float, float]
    machine_slot: str | None    # raw MACHINE_SLOT, or None when unset/blank


def parse_bed_size(value: str) -> tuple[float, float]:
    """Parse "220x220" / "300X250" into (220.0, 220.0)."""
    parts = value.strip().lower().split("x")
    if len(parts) != 2:
        raise ConfigError(f"BED_SIZE must look like 220x220 (got {value!r})")
    try:
        width, height = (float(part.strip()) for part in parts)
    except ValueError:
        raise ConfigError(f"BED_SIZE must look like 220x220 (got {value!r})") from None
    # 0 < x < inf also rejects nan and an infinite bed
    if not 0 < width < math.inf or not 0 < height < math.inf:
        raise ConfigError(f"BED_SIZE must look like 220x220 (got {value!r})")
    return width, height


def _value(env: Mapping[str, str], name: str) -> str:
    """Env value for ``name``; a variable that is absent gets its default."""
    return str(env.get(name, DEFAULTS[name]))


def _expand_path(value: str, env: Mapping[str, str]) -> str:
    """Expand ``~`` and ``$VAR`` / ``${VAR}`` against the effective environment.

    ``os.path.expandvars`` only knows ``os.environ``, so variables are resolved
    from the mapping ``load_config`` was given first (that mapping *is*
    ``os.environ`` in production). Unknown variables are left untouched.
    """
    def lookup(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        for source in (env, os.environ):
            if name in source:
                return str(source[name])
        return match.group(0)

    return _VAR_RE.sub(lookup, os.path.expanduser(value))


def _positive_int(name: str, value: str) -> int:
    try:
        number = int(value.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a positive integer (got {value!r})") from None
    if number <= 0:
        raise ConfigError(f"{name} must be a positive integer (got {value!r})")
    return number


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a :class:`Config` from ``env`` (``os.environ`` when ``None``).

    Validation only — no directories are created and nothing is written.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    for name in REQUIRED:
        if not (source.get(name) or "").strip():
            raise ConfigError(f"{name} is required: {REQUIRED_HINTS[name]}")

    printer_url = _value(source, "PRINTER_URL")
    if not printer_url.startswith(("http://", "https://")):
        raise ConfigError(f"PRINTER_URL must start with http:// or https:// (got {printer_url!r})")
    printer_url = printer_url.rstrip("/")

    data_dir = Path(_value(source, "DATA_DIR"))
    given_datadir = source.get("ORCA_DATADIR")
    orca_datadir = (
        Path(_expand_path(given_datadir, source)) if given_datadir else data_dir / "orca-datadir"
    )

    port = _positive_int("PORT", _value(source, "PORT"))
    if port > MAX_PORT:
        raise ConfigError(f"PORT must be a positive integer <= {MAX_PORT} (got {_value(source, 'PORT')!r})")

    return Config(
        printer_url=printer_url,
        # stored verbatim: it becomes the X-Api-Key header, never printed
        moonraker_api_key=source["MOONRAKER_API_KEY"],
        moonraker_root=_value(source, "MOONRAKER_ROOT"),
        port=port,
        data_dir=data_dir,
        orca_bin=Path(_value(source, "ORCA_BIN")),
        orca_datadir=orca_datadir,
        jobs_dir=data_dir / "jobs",
        home_dir=data_dir / "home",
        profile_file=data_dir / "profile.json",
        max_upload_bytes=_positive_int("MAX_UPLOAD_MB", _value(source, "MAX_UPLOAD_MB")) * MIB,
        keep_jobs=_positive_int("KEEP_JOBS", _value(source, "KEEP_JOBS")),
        slice_timeout=_positive_int("SLICE_TIMEOUT", _value(source, "SLICE_TIMEOUT")),
        bed_size=parse_bed_size(_value(source, "BED_SIZE")),
        # a blank MACHINE_SLOT means "no override", so imports fall back to the
        # datadir default machine preset
        machine_slot=(source.get("MACHINE_SLOT") or None),
    )


def main() -> None:
    """``python -m config``: print a one-line summary, never the API key."""
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"print-portal: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    width, height = cfg.bed_size
    print(
        f"print-portal: {cfg.printer_url} root={cfg.moonraker_root} "
        f"data={cfg.data_dir} slicer={cfg.orca_bin} bed={width:g}x{height:g}"
    )


if __name__ == "__main__":
    main()
