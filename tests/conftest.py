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

import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
SEED_DATADIR = ROOT / "presets" / "datadir"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PRESET_DATADIR = FIXTURES / "preset-datadir"

# production runs uvicorn with WORKDIR=/app and flat imports (`import config`), so the tests
# put both the repo root and app/ on sys.path
for _entry in (ROOT, APP_DIR):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

# the real seed ships only system/Custom.json, so the session datadir starts from the
# trimmed fixture copy of the presets the old seed used to ship
SESSION_DATA_DIR = Path(tempfile.mkdtemp(prefix="print-portal-tests-"))
shutil.copytree(PRESET_DATADIR, SESSION_DATA_DIR / "orca-datadir")

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

# Tests never consult a host Orca installation; these small synthetic parents make
# the trimmed historical presets resolvable and keep the suite deterministic.
import profiles
profiles.ORCA_PROFILES = FIXTURES / "orca-resources"


@pytest.fixture(scope="session")
def data_dir() -> Path:
    """The session-wide DATA_DIR the app imported itself with."""
    return SESSION_DATA_DIR


@pytest.fixture
def datadir(tmp_path: Path) -> Path:
    """A private copy of the presets the old seed used to ship, trimmed to what the tests
    read, safe for import_into() to write into."""
    dest = tmp_path / "orca-datadir"
    shutil.copytree(PRESET_DATADIR, dest)
    return dest


@pytest.fixture
def empty_datadir(tmp_path: Path) -> Path:
    """A private copy of the real, near-empty seed (no presets), as a fresh install has it."""
    dest = tmp_path / "orca-datadir"
    shutil.copytree(SEED_DATADIR, dest)
    return dest


@pytest.fixture
def bundle_bytes() -> bytes:
    """The committed .orca_printer fixture bundle, as the upload endpoint would receive it."""
    return (FIXTURES / "portal-import.orca_printer").read_bytes()


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


@pytest.fixture
def zip_bytes():
    def _build(members: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in members.items():
                z.writestr(name, data)
        return buf.getvalue()
    return _build


@pytest.fixture
def zip_bytes_with():
    def _build(members, *, symlink_name=None, traversal_name=None):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in members.items():
                z.writestr(name, data)
            if symlink_name is not None:
                zi = zipfile.ZipInfo(symlink_name)
                zi.external_attr = (0o120777 << 16)
                z.writestr(zi, b"")
            if traversal_name is not None:
                z.writestr(traversal_name, b"evil")
        return buf.getvalue()
    return _build


@pytest.fixture
def write_bytes_fixture():
    def _write(tmp_path, name, blob) -> Path:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
        return p
    return _write


@pytest.fixture
def three_mf_bytes():
    def _build(*, printer="Generic Klipper 0.4 nozzle", printer_model="Generic Klipper Printer",
               variant="0.4", process="0.20mm Standard @Generic Klipper",
               filaments=("Generic PLA",), settings_member="project_settings.config",
               extra=None, model_members=True) -> bytes:
        settings_obj = {
            "printer_settings_id": [printer],
            "printer_model": [printer_model],
            "printer_variant": [variant],
            "print_settings_id": [process],
            "filament_settings_id": [f for f in filaments],
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'></Types>")
            z.writestr("_rels/.rels", "<?xml version='1.0'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'></Relationships>")
            z.writestr("3D/3dmodel.model", "<?xml version='1.0'?><model unit='millimeter' xmlns='http://schemas.microsoft.com/3dmanufacturing/core/2015/02'></model>")
            if settings_member is not None:
                name = settings_member if "/" in settings_member else "Metadata/" + settings_member
                if settings_member.endswith("Slic3r_PE.config") or settings_member == "Slic3r_PE.config":
                    lines = [f"printer_settings_id = {printer}", f"printer_model = {printer_model}",
                             f"printer_variant = {variant}", f"print_settings_id = {process}",
                             f"filament_settings_id = {filaments[0] if filaments else ''}"]
                    data = ("\n".join(lines) + "\n").encode()
                else:
                    data = json.dumps(settings_obj).encode()
                z.writestr(name, data)
            if model_members:
                z.writestr("Metadata/model_settings.config", "<?xml version='1.0'?><config></config>")
            if extra:
                for n, d in extra.items():
                    z.writestr(n, d)
        return buf.getvalue()
    return _build
