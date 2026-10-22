"""Environment configuration for print-portal.

This module is the single source of truth for the env-var table, its defaults and
its validation; every other module reads ``config.load_config()`` and nothing else
touches ``os.environ``.

The strict CLI (``python -m config``) fails closed without ``MOONRAKER_API_KEY``;
the app itself boots without it so printers can be managed in the UI. The API key
is never printed or logged.
"""
from __future__ import annotations

import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

VERSION = "2.1.0"  # single source of truth; .gitlab-ci.yml APP_VERSION must match

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
    "ENV_PRINTER_NAME": "Default (environment)",
}

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
    moonraker_api_key: str | None
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
    # new (T2): UI-managed printers + mountable sliced output
    sliced_dir: Path           # SLICED_DIR or data_dir / "sliced"
    printers_file: Path        # PRINTERS_FILE or data_dir / "printers.json"
    secret_key_file: Path      # SECRET_KEY_FILE or data_dir / "secret.key"
    env_printer_name: str      # ENV_PRINTER_NAME or "Default (environment)"
    env_printer_url: str       # PRINTER_URL (default http://127.0.0.1:7125, rstrip("/"))
    env_printer_api_key: str | None  # None when the env seed is not configured


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


def load_config(env: Mapping[str, str] | None = None, *, printer_seed_required: bool = True) -> Config:
    """Build a :class:`Config` from ``env`` (``os.environ`` when ``None``).

    Validation only — no directories are created and nothing is written.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    api_key_raw = (source.get("MOONRAKER_API_KEY") or "").strip()
    explicit_printer_url = bool((source.get("PRINTER_URL") or "").strip()) and "PRINTER_URL" in source

    if printer_seed_required:
        if not api_key_raw:
            raise ConfigError(f"MOONRAKER_API_KEY is required: {REQUIRED_HINTS['MOONRAKER_API_KEY']}")
    else:
        if explicit_printer_url and not api_key_raw:
            raise ConfigError(
                "PRINTER_URL is set but MOONRAKER_API_KEY is empty: "
                "set both (environment printer) or neither (manage printers in Settings)"
            )

    printer_url_raw = source.get("PRINTER_URL")
    if printer_url_raw is None or not str(printer_url_raw).strip():
        printer_url = DEFAULTS["PRINTER_URL"]
    else:
        printer_url = str(printer_url_raw)
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

    sliced_raw = source.get("SLICED_DIR")
    sliced_dir = Path(_expand_path(sliced_raw, source)) if (sliced_raw or "").strip() else data_dir / "sliced"

    printers_raw = source.get("PRINTERS_FILE")
    printers_file = Path(_expand_path(printers_raw, source)) if (printers_raw or "").strip() else data_dir / "printers.json"

    secret_raw = source.get("SECRET_KEY_FILE")
    secret_key_file = Path(_expand_path(secret_raw, source)) if (secret_raw or "").strip() else data_dir / "secret.key"

    env_printer_name = (source.get("ENV_PRINTER_NAME") or "").strip()
    if not env_printer_name:
        env_printer_name = DEFAULTS["ENV_PRINTER_NAME"]

    env_printer_api_key = api_key_raw if api_key_raw else None

    return Config(
        printer_url=printer_url,
        # trimmed of surrounding whitespace; becomes the X-Api-Key header, never printed
        moonraker_api_key=env_printer_api_key,
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
        sliced_dir=sliced_dir,
        printers_file=printers_file,
        secret_key_file=secret_key_file,
        env_printer_name=env_printer_name,
        env_printer_url=printer_url,
        env_printer_api_key=env_printer_api_key,
    )


def main(argv: list[str] | None = None) -> None:
    """``python -m config``: print a one-line summary, never the API key."""
    args = sys.argv[1:] if argv is None else argv
    allow_unconfigured = "--allow-unconfigured" in args
    try:
        cfg = load_config(printer_seed_required=not allow_unconfigured)
    except ConfigError as exc:
        print(f"print-portal: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    if allow_unconfigured and cfg.env_printer_api_key is None:
        print(
            "print-portal: no PRINTER_URL/MOONRAKER_API_KEY set; "
            "manage printers in Settings",
            file=sys.stderr,
        )
    width, height = cfg.bed_size
    print(
        f"print-portal: {cfg.printer_url} root={cfg.moonraker_root} "
        f"data={cfg.data_dir} slicer={cfg.orca_bin} bed={width:g}x{height:g}"
    )


if __name__ == "__main__":
    main()
