"""Offline test bootstrap: environment first, app imports second.

``app/main.py`` calls ``config.load_config()`` at import time and creates ``jobs/`` and
``home/`` next to it, so the test environment has to exist before anything imports it —
otherwise a developer's real ``DATA_DIR``/``/data`` would be used. ``PRINTER_URL`` points
at a ``.invalid`` host: nothing in the suite resolves or contacts it, and every test that
needs a printer answer monkeypatches ``urllib.request.urlopen`` instead.

``TestClient`` is used without its context manager everywhere, so FastAPI's lifespan never
runs and the slicing worker thread never starts.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
SEED_DATADIR = ROOT / "presets" / "datadir"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# production runs uvicorn with WORKDIR=/app and flat imports (`import config`), so the tests
# put both the repo root and app/ on sys.path
for _entry in (ROOT, APP_DIR):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

SESSION_DATA_DIR = Path(tempfile.mkdtemp(prefix="print-portal-tests-"))
shutil.copytree(SEED_DATADIR, SESSION_DATA_DIR / "orca-datadir")

os.environ.update({
    "PRINTER_URL": "http://moonraker.invalid:7125",
    "MOONRAKER_API_KEY": "test-key",
    "DATA_DIR": str(SESSION_DATA_DIR),
    "ORCA_DATADIR": str(SESSION_DATA_DIR / "orca-datadir"),
    "MAX_UPLOAD_MB": "1",          # keeps the oversize test at ~1 MB of request body
    "KEEP_JOBS": "5",
    "SLICE_TIMEOUT": "5",
    "BED_SIZE": "220x220",
    "MACHINE_SLOT": "",
})


@pytest.fixture(scope="session")
def data_dir() -> Path:
    """The session-wide DATA_DIR the app imported itself with."""
    return SESSION_DATA_DIR


@pytest.fixture
def datadir(tmp_path: Path) -> Path:
    """A private copy of the shipped preset seed, safe for import_into() to write into."""
    dest = tmp_path / "orca-datadir"
    shutil.copytree(SEED_DATADIR, dest)
    return dest


@pytest.fixture
def env() -> dict[str, str]:
    """Minimal environment for ``config.load_config(env)``; mutate it freely."""
    return {"MOONRAKER_API_KEY": "test-key"}


@pytest.fixture
def jobs_dir(tmp_path: Path) -> Path:
    return tmp_path / "jobs"


@pytest.fixture
def printers_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test printers.json location; resets main.STORE."""
    import main
    import printers

    p = tmp_path / "printers.json"
    monkeypatch.setattr(main, "STORE", printers.PrinterStore.open(p, tmp_path / "secret.key"))
    return p


@pytest.fixture
def sliced_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "sliced"
    d.mkdir(parents=True, exist_ok=True)
    import main
    monkeypatch.setattr(main, "SLICED", d)
    return d


@pytest.fixture
def client(jobs_dir: Path, printers_file: Path, sliced_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """API client with its own job storage and no lifespan (so no slicer worker)."""
    import main
    import printers

    jobs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main, "JOBS", jobs_dir)
    monkeypatch.setattr(main, "STORE", printers.PrinterStore.open(printers_file, printers_file.parent / "secret.key"))
    monkeypatch.setattr(main, "SLICED", sliced_dir)
    return TestClient(main.app)


@pytest.fixture(scope="session")
def cube_stl() -> bytes:
    return (FIXTURES / "cube.stl").read_bytes()
