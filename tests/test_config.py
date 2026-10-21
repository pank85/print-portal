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
