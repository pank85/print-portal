"""config.py: defaults, env overrides, parsing, fail-closed behaviour, version sync.

Everything here drives ``load_config(env=...)`` with an explicit mapping, so the tests do
not depend on (or disturb) the process environment the rest of the suite imported with.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import config

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"


def test_only_the_api_key_is_required(env):
    cfg = config.load_config(env)

    assert cfg.printer_url == "http://127.0.0.1:7125"
    assert cfg.moonraker_api_key == "test-key"
    assert cfg.moonraker_root == "gcodes"
    assert cfg.port == 8088
    assert cfg.data_dir == Path("/data")
    assert cfg.orca_bin == Path("/opt/orca/bin/orca-slicer")
    assert cfg.orca_datadir == Path("/data/orca-datadir")
    assert cfg.jobs_dir == Path("/data/jobs")
    assert cfg.home_dir == Path("/data/home")
    assert cfg.profile_file == Path("/data/profile.json")
    assert cfg.max_upload_bytes == 150 * 1024 * 1024
    assert cfg.keep_jobs == 25
    assert cfg.slice_timeout == 900
    assert cfg.bed_size == (220.0, 220.0)
    assert cfg.machine_slot is None


def test_every_variable_can_be_overridden(env):
    env.update({
        "PRINTER_URL": "https://printer.lan:7125/",
        "MOONRAKER_API_KEY": "s3cret",
        "MOONRAKER_ROOT": "models",
        "PORT": "9000",
        "DATA_DIR": "/srv/portal",
        "ORCA_BIN": "/usr/local/bin/orca-slicer",
        "ORCA_DATADIR": "/srv/portal/custom-presets",
        "MAX_UPLOAD_MB": "3",
        "KEEP_JOBS": "7",
        "SLICE_TIMEOUT": "42",
        "BED_SIZE": "300x250",
        "MACHINE_SLOT": "system/Custom/machine/other.json",
    })

    cfg = config.load_config(env)

    assert cfg.printer_url == "https://printer.lan:7125"   # trailing slash dropped
    assert cfg.moonraker_api_key == "s3cret"
    assert cfg.moonraker_root == "models"
    assert cfg.port == 9000
    assert cfg.data_dir == Path("/srv/portal")
    assert cfg.orca_bin == Path("/usr/local/bin/orca-slicer")
    assert cfg.orca_datadir == Path("/srv/portal/custom-presets")
    assert cfg.jobs_dir == Path("/srv/portal/jobs")
    assert cfg.home_dir == Path("/srv/portal/home")
    assert cfg.profile_file == Path("/srv/portal/profile.json")
    assert cfg.max_upload_bytes == 3 * 1024 * 1024
    assert cfg.keep_jobs == 7
    assert cfg.slice_timeout == 42
    assert cfg.bed_size == (300.0, 250.0)
    assert cfg.machine_slot == "system/Custom/machine/other.json"


def test_orca_datadir_defaults_below_data_dir(env):
    env["DATA_DIR"] = "/srv/portal"

    assert config.load_config(env).orca_datadir == Path("/srv/portal/orca-datadir")


def test_empty_machine_slot_means_no_override(env):
    env["MACHINE_SLOT"] = ""

    assert config.load_config(env).machine_slot is None


@pytest.mark.parametrize("value, expected", [
    ("220x220", (220.0, 220.0)),
    ("300X250", (300.0, 250.0)),
    (" 300 x 250 ", (300.0, 250.0)),
])
def test_bed_size_parsing(value, expected):
    assert config.parse_bed_size(value) == expected


@pytest.mark.parametrize("value", ["220", "220x", "x220", "abc", "220x220x220", "0x220", "-1x220"])
def test_malformed_bed_size_raises(value):
    with pytest.raises(config.ConfigError, match="BED_SIZE must look like 220x220"):
        config.parse_bed_size(value)


@pytest.mark.parametrize("value", ["", "   "])
def test_missing_or_blank_api_key_fails_closed(env, value):
    env["MOONRAKER_API_KEY"] = value

    with pytest.raises(config.ConfigError, match="MOONRAKER_API_KEY is required"):
        config.load_config(env)


def test_no_api_key_at_all_fails_closed():
    with pytest.raises(config.ConfigError, match="MOONRAKER_API_KEY is required"):
        config.load_config({})


@pytest.mark.parametrize("value", ["printer.lan:7125", "ftp://printer", "//printer"])
def test_bad_printer_url_raises(env, value):
    env["PRINTER_URL"] = value

    with pytest.raises(config.ConfigError, match="PRINTER_URL must start with http://"):
        config.load_config(env)


@pytest.mark.parametrize("name, value", [
    ("PORT", "0"),
    ("PORT", "-1"),
    ("PORT", "abc"),
    ("PORT", "70000"),
    ("MAX_UPLOAD_MB", "0"),
    ("KEEP_JOBS", "0"),
    ("SLICE_TIMEOUT", "soon"),
])
def test_non_positive_numbers_raise(env, name, value):
    env[name] = value

    with pytest.raises(config.ConfigError, match=f"{name} must be a positive integer"):
        config.load_config(env)


def _run_config_module(
    monkeypatch: pytest.MonkeyPatch, **overrides: str
) -> subprocess.CompletedProcess:
    environ = {k: v for k, v in os.environ.items() if k != "MOONRAKER_API_KEY"}
    environ.update(overrides)
    monkeypatch.delenv("MOONRAKER_API_KEY", raising=False)
    return subprocess.run(
        [sys.executable, "-m", "config"],
        cwd=APP_DIR, env=environ, capture_output=True, text=True, timeout=60,
    )


def test_python_m_config_exits_2_without_an_api_key(monkeypatch):
    proc = _run_config_module(monkeypatch)

    assert proc.returncode == 2
    assert "print-portal:" in proc.stderr
    assert "MOONRAKER_API_KEY is required" in proc.stderr


def test_python_m_config_prints_a_summary_without_the_api_key(monkeypatch):
    proc = _run_config_module(
        monkeypatch,
        MOONRAKER_API_KEY="super-secret-key",
        PRINTER_URL="http://printer.invalid:7125",
        BED_SIZE="300x250",
    )

    assert proc.returncode == 0
    assert "print-portal: http://printer.invalid:7125" in proc.stdout
    assert "bed=300x250" in proc.stdout
    assert "super-secret-key" not in proc.stdout + proc.stderr


def test_version_matches_the_ci_app_version():
    ci = ROOT / ".gitlab-ci.yml"
    assert ci.is_file(), ".gitlab-ci.yml is part of the shipped tree"

    match = re.search(r'^\s*APP_VERSION:\s*"([^"]+)"', ci.read_text(), re.MULTILINE)
    assert match, "APP_VERSION is not declared in .gitlab-ci.yml"
    assert match.group(1) == config.VERSION


def test_seed_is_optional_for_ui_managed_printers():
    cfg = config.load_config({"MOONRAKER_API_KEY": "k"}, printer_seed_required=False)
    assert cfg.env_printer_api_key == "k"
    assert cfg.printers_file == Path("/data/printers.json")
    assert cfg.sliced_dir == Path("/data/sliced")
    assert cfg.secret_key_file == Path("/data/secret.key")


def test_printer_url_without_a_key_is_a_config_error():
    with pytest.raises(config.ConfigError, match="PRINTER_URL is set but MOONRAKER_API_KEY is empty"):
        config.load_config({"PRINTER_URL": "http://printer.local:7125"}, printer_seed_required=False)


def test_key_without_an_explicit_url_uses_the_default():
    cfg = config.load_config({"MOONRAKER_API_KEY": "k"}, printer_seed_required=False)
    assert cfg.env_printer_url == "http://127.0.0.1:7125"
    assert cfg.env_printer_api_key == "k"


def test_new_paths_default_inside_the_data_dir(env):
    env["MOONRAKER_API_KEY"] = "k"
    cfg = config.load_config(env)
    assert cfg.sliced_dir == Path("/data/sliced")
    assert cfg.printers_file == Path("/data/printers.json")
    assert cfg.secret_key_file == Path("/data/secret.key")


def test_new_paths_can_be_overridden(env):
    env.update({
        "MOONRAKER_API_KEY": "k",
        "SLICED_DIR": "/custom/sliced",
        "PRINTERS_FILE": "/custom/p.json",
        "SECRET_KEY_FILE": "/custom/s.key",
    })
    cfg = config.load_config(env)
    assert cfg.sliced_dir == Path("/custom/sliced")
    assert cfg.printers_file == Path("/custom/p.json")
    assert cfg.secret_key_file == Path("/custom/s.key")


def test_sliced_dir_expands_variables():
    cfg = config.load_config(
        {"MOONRAKER_API_KEY": "k", "DATA_DIR": "/srv/portal", "SLICED_DIR": "$DATA_DIR/sliced"},
        printer_seed_required=False,
    )
    assert cfg.sliced_dir == Path("/srv/portal/sliced")


def test_env_printer_name_override():
    cfg = config.load_config(
        {"MOONRAKER_API_KEY": "k", "ENV_PRINTER_NAME": "My Printer"},
        printer_seed_required=False,
    )
    assert cfg.env_printer_name == "My Printer"


def test_env_example_lists_only_known_variables():
    known = {
        "PRINTER_URL", "MOONRAKER_API_KEY", "MOONRAKER_ROOT", "PORT", "DATA_DIR",
        "ORCA_BIN", "ORCA_DATADIR", "MAX_UPLOAD_MB", "KEEP_JOBS", "SLICE_TIMEOUT",
        "BED_SIZE", "MACHINE_SLOT", "SLICED_DIR", "PRINTERS_FILE", "SECRET_KEY_FILE",
        "ENV_PRINTER_NAME", "PRINTER_UI_URL",
    }
    env_path = ROOT / ".env.example"
    keys = []
    example_values = {}
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
            if not stripped or "=" not in stripped:
                continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            continue
        keys.append(key)
        example_values[key] = value.strip()
    assert set(keys) <= known, set(keys) - known
    # build an env with all keys present and load successfully
    env = {"MOONRAKER_API_KEY": "k"}
    for key in keys:
        env[key] = example_values[key] if example_values[key] else "k"
    if "PRINTER_URL" not in env:
        env["PRINTER_URL"] = "http://printer.local:7125"
    config.load_config(env, printer_seed_required=False)
    print("PARSED_KEYS:", keys)


# ---------------------------------------------------------------- PRINTER_UI_URL
def test_printer_ui_url_is_unset_by_default(env):
    assert config.load_config(env).printer_ui_url == ""


@pytest.mark.parametrize("value", ["", "  ", "\n"])
def test_blank_printer_ui_url_is_treated_as_unset(env, value):
    # a blank line in .env must not turn into a frame pointing at "": no UI, not a broken one
    cfg = config.load_config({**env, "PRINTER_UI_URL": value})
    assert cfg.printer_ui_url == ""


def test_printer_ui_url_keeps_a_bare_origin_and_strips_the_trailing_slash(env):
    cfg = config.load_config({**env, "PRINTER_UI_URL": "http://printer.local/"})
    assert cfg.printer_ui_url == "http://printer.local"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("http://printer.local", "http://printer.local"),
        ("http://printer.local:8080/fluidd", "http://printer.local:8080/fluidd"),
        ("https://portal.example/mainsail", "https://portal.example/mainsail"),
    ],
)
def test_printer_ui_url_accepts_a_path_and_a_port(env, value, expected):
    cfg = config.load_config({**env, "PRINTER_UI_URL": value})
    assert cfg.printer_ui_url == expected


@pytest.mark.parametrize(
    "value, message",
    [
        ("printer.local", "PRINTER_UI_URL must start with http:// or https://"),
        ("ftp://printer.local", "PRINTER_UI_URL must start with http:// or https://"),
        ("HTTPS://printer.local", "PRINTER_UI_URL must start with http:// or https://"),
        ("http://", "PRINTER_UI_URL must name a host"),
        ("http:///fluidd", "PRINTER_UI_URL must name a host"),
        ("http://user:secret@printer.local", "PRINTER_UI_URL must not embed credentials"),
        ("http://printer.local/?token=x", "PRINTER_UI_URL must not carry a query string"),
        ("http://printer.local/#/jobs", "PRINTER_UI_URL must not carry a fragment"),
        ("http://printer.local:notaport", "PRINTER_UI_URL is not a usable URL"),
    ],
)
def test_printer_ui_url_fails_loudly_when_malformed(env, value, message):
    # fail-closed at startup: a silently dropped frame is a blank tab nobody can diagnose
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_config({**env, "PRINTER_UI_URL": value})
    assert message in str(excinfo.value)


def test_env_example_ignores_a_commented_printer_ui_url():
    # the var ships commented out, so a copy of .env.example stays frameless
    text = (ROOT / ".env.example").read_text()
    assert "# PRINTER_UI_URL=http://printer.local/" in text


def test_compose_ships_the_sliced_mount_and_env():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "SLICED_DIR" in text
    assert "./sliced:/sliced" in text


def test_config_cli_still_requires_the_api_key(monkeypatch):
    proc = _run_config_module(monkeypatch)
    assert proc.returncode == 2
    assert "MOONRAKER_API_KEY is required" in proc.stderr


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_printer_url_is_treated_as_unset(value):
    cfg = config.load_config(
        {"PRINTER_URL": value, "MOONRAKER_API_KEY": "k"},
        printer_seed_required=False,
    )
    assert cfg.printer_url == "http://127.0.0.1:7125"
    assert cfg.env_printer_url == "http://127.0.0.1:7125"


def test_config_cli_allow_unconfigured_exits_0(monkeypatch):
    proc = subprocess.run(
        [sys.executable, "-m", "config", "--allow-unconfigured"],
        cwd=APP_DIR,
        env={k: v for k, v in os.environ.items()
             if k not in ("MOONRAKER_API_KEY", "PRINTER_URL")},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    assert "no PRINTER_URL/MOONRAKER_API_KEY set" in proc.stderr
