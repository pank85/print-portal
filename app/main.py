#!/usr/bin/env python3
"""print-portal — upload a model, slice it with OrcaSlicer, print it over Moonraker.

Slicing runs on the host serving this app; the printer only receives the finished gcode
through Moonraker's HTTP API, so slicing never competes with klippy for CPU.

Endpoints
    GET    /                     upload UI
    GET    /api/health           process readiness — never touches the printer
    GET    /api/printer          live printer snapshot of the selected printer
    GET    /api/printers         configured printers (masked keys) + selection
    POST   /api/printers         add a printer
    PATCH  /api/printers/{id}    change name/url/api_key
    DELETE /api/printers/{id}    remove a printer
    POST   /api/printers/{id}/select  make it the active printer
    POST   /api/printers/{id}/test    reachability probe (never raises)
    POST   /api/upload           multipart file (+ optional output_name) -> queues the slice
    GET    /api/jobs             job list
    GET    /api/jobs/{id}        job status + stats
    GET    /api/jobs/{id}/gcode  download the sliced gcode
    POST   /api/jobs/{id}/print  upload the gcode to Moonraker and start it
    DELETE /api/jobs/{id}        remove job directory and its sliced output
    DELETE /api/jobs             remove every finished job (skips active ones)
    GET    /api/profile          active presets + labels + effective settings
    GET    /api/profiles         selectable presets per type (``?machine=<rel>`` filters
                                 the process list to that machine's compatible presets)
    POST   /api/profile/upload   import an .orca_printer bundle or a single preset
    POST   /api/profile/select   choose the active presets
    POST   /api/profile/options  set the basic slicing options (supports, brim, hide selectors)
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

import archives
import config
import moonraker
import printers
import profiles as prof
import threemf
from gcode import parse_gcode_stats

# The env printer is optional here: printers normally live in printers.json. An unconfigured
# instance still boots so the UI can be used, and printer traffic fails closed per request.
CONFIG = config.load_config(printer_seed_required=False)
APP_DIR = Path(__file__).resolve().parent
ALLOWED = {".stl", ".3mf", ".obj", ".zip"}
PROFILE_BUNDLE_MAX = prof.BUNDLE_MAX_BYTES
PROFILE_BUNDLE_SUFFIXES = (".orca_printer", ".json", ".zip")
JOB_ID_RE = re.compile(r"[0-9a-f]{12}")
ACTIVE_STATUSES = ("queued", "slicing")
OUTPUT_NAME_MAX = 60
OUTPUT_NAME_ALLOWED_RE = re.compile(r"[A-Za-z0-9 ._-]+")
OUTPUT_NAME_SEPARATORS = "output_name must be a plain file name without path separators"
WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL",
     *(f"COM{index}" for index in range(1, 10)),
     *(f"LPT{index}" for index in range(1, 10))})
NO_PRINTER = ("no printer configured: add one in Settings or set PRINTER_URL and "
              "MOONRAKER_API_KEY")

JOBS = CONFIG.jobs_dir
TMP_DIR = CONFIG.data_dir / "tmp"
SLICED = CONFIG.sliced_dir
JOBS.mkdir(parents=True, exist_ok=True)
CONFIG.home_dir.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)
SLICED.mkdir(parents=True, exist_ok=True)

STORE = printers.load_printers(CONFIG.printers_file, CONFIG.secret_key_file)
SEED = (printers.SeededPrinter(id=printers.ENV_PRINTER_ID, name=CONFIG.env_printer_name,
                               url=CONFIG.env_printer_url, api_key=CONFIG.env_printer_api_key)
        if CONFIG.env_printer_api_key else None)
job_queue: "queue.Queue[str]" = queue.Queue()

# The derived process preset is shared state: `run_slice` writes it and launches the slicer
# with it, `regenerate_derived` rewrites it from the options. Held across that whole span so
# a concurrent option save cannot replace the file the running slicer is loading, which
# would silently slice with the live options instead of the job's snapshot.
DERIVED_LOCK = threading.Lock()


# ---------------------------------------------------------------- printers
# One resolution path for every printer-touching request: the store is reloaded and the
# client is rebuilt per call, so switching printers in Settings takes effect immediately.
def active_target() -> printers.PrintTarget | None:
    """The printer this request should use, or None when nothing is configured."""
    try:
        return STORE.active(SEED)
    except printers.PrinterStoreError as exc:
        raise HTTPException(409, str(exc)) from exc


def current_target() -> printers.PrintTarget:
    target = active_target()
    if target is None:
        raise HTTPException(503, NO_PRINTER)
    return target


def current_client() -> moonraker.MoonrakerClient:
    """A fresh client for the selected printer — never cached across requests."""
    target = current_target()
    return moonraker.MoonrakerClient(target.url, target.api_key, CONFIG.moonraker_root)


def printer_call(callback, *args, **kwargs):
    """Run one store operation, mapping its errors onto the documented HTTP codes."""
    try:
        return callback(*args, **kwargs)
    except printers.PrinterNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except printers.PrinterReadOnly as exc:
        raise HTTPException(409, str(exc)) from exc
    except printers.PrinterError as exc:
        raise HTTPException(400, str(exc)) from exc
    except printers.PrinterStoreError as exc:
        raise HTTPException(409, str(exc)) from exc


def printer_view(printer_id: str) -> dict:
    """One PrinterView, taken from the same list GET /api/printers returns."""
    for view in STORE.views(SEED):
        if view["id"] == printer_id:
            return view
    raise HTTPException(404, f"printer {printer_id!r} not found")


def select_printer(printer_id: str) -> None:
    """Persist the selection; the env id only exists while an env seed is configured.

    Without the seed there is no ``env`` row to select, and persisting it would leave a
    selection that resolves to no printer at all.
    """
    if printer_id == printers.ENV_PRINTER_ID and SEED is None:
        raise printers.PrinterNotFound(f"printer {printer_id!r} not found")
    STORE.select(printer_id)


def printer_target(printer_id: str) -> printers.PrintTarget:
    """Resolve one printer by id (env seed included) for the test-connection endpoint."""
    if printer_id == printers.ENV_PRINTER_ID and SEED is not None:
        return printers.PrintTarget(id=SEED.id, name=SEED.name, url=SEED.url,
                                    api_key=SEED.api_key, source="env")
    stored = printer_call(STORE.get, printer_id)
    return printers.PrintTarget(id=stored.id, name=stored.name, url=stored.url,
                                api_key=printer_call(STORE.api_key, stored), source="store")


# ---------------------------------------------------------------- active presets
def relative_to_datadir(path: Path) -> str:
    """Preset paths are persisted relative to the datadir; keep absolutes for foreign files."""
    try:
        return str(path.relative_to(CONFIG.orca_datadir))
    except ValueError:
        return str(path)


def _preset_path(rel: object) -> Path | None:
    """The datadir file a relative preset path names, or None when it is not one."""
    if not isinstance(rel, str) or not rel:
        return None
    datadir = CONFIG.orca_datadir.resolve()
    try:
        target = (datadir / rel).resolve()
    except OSError:
        return None
    if not target.is_relative_to(datadir) or not target.is_file():
        return None
    return target


def _preset_type(path: Path) -> str | None:
    """The `type` a preset file declares, or None when it is unreadable or undeclared."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data.get("type") if isinstance(data, dict) else None


def _profile_rel(kind: str, rel: object) -> str | None:
    """A validated datadir-relative preset path, or None for "inherit the global profile".

    Shared by `/api/profile/select` and the printer pairing: the file has to exist inside
    the datadir *and* be a preset of the slot's own kind, so a bad value is a 400 here
    instead of a slicer failure (or a path outside the datadir) at slice time. Only the
    location and the declared type are checked; what a preset may contain is
    `app/printers.py`'s business.
    """
    if rel is None or rel == "":
        return None
    target = _preset_path(rel)
    if not isinstance(rel, str) or target is None or _preset_type(target) != kind:
        raise HTTPException(400, f"{kind}: '{rel}' is not a preset inside the datadir")
    return rel


def _preset_row(kind: str, rel: object) -> dict | None:
    """The summarised row of one datadir preset, or None when the path is not one."""
    target = _preset_path(rel)
    return prof.summarise(target, kind, CONFIG.orca_datadir) if target is not None else None


def _permits(machine: dict, process: dict | None) -> bool:
    """Whether ``process`` may be sliced with ``machine`` — the preset picker's own rule.

    A process that is missing or unreadable counts as not permitted, so the caller falls
    back rather than persisting a pairing the slicer would refuse with `-17`. An unreadable
    preset reaches this function as the error row `summarise` returns — no ``rel`` to
    persist and, with no ``compatible_printers`` either, a machine filter would call it
    universal — so the shape is checked before the compatibility rule.
    """
    if not isinstance(process, dict) or "rel" not in process:
        return False
    return bool(prof.filter_process_by_machine([process], machine))


def _paired_process(machine: dict | None, process: dict | None,
                    current_rel: object) -> dict | None:
    """The process to persist next to ``machine``: the 3MF's own, else the current one.

    ``None`` means no process this machine permits, which is the caller's cue to leave the
    stored pairing alone instead of writing a combination the slicer refuses.
    """
    if not isinstance(machine, dict):
        return None
    for candidate in (process, _preset_row("process", current_rel)):
        if _permits(machine, candidate):
            return candidate
    return None


def pairing_from_body(raw: object) -> dict[str, str | None]:
    """The ``{slot: rel}`` pairing in a request body, checked against the datadir.

    Every slot is returned, ``None`` meaning "inherit"; a slot the body leaves out is
    cleared, because a pairing is a complete statement about the printer, not a patch.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(400, printers.PROFILES_SHAPE_ERROR)
    return {slot: _profile_rel(slot, raw.get(slot)) for slot in printers.PROFILE_SLOTS}


def read_profile_document() -> dict:
    """The stored profile document; a missing, unreadable or non-object file reads as {}."""
    try:
        stored = json.loads(CONFIG.profile_file.read_text())
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def active_paths() -> dict[str, Path]:
    """Which presets the slicer loads: profile.json over the shipped defaults.

    The machine default comes from profiles.machine_slot() so it resolves on any datadir
    (MACHINE_SLOT env -> datadir default -> first instantiated machine).
    """
    datadir = CONFIG.orca_datadir
    slot = prof.machine_slot(datadir, CONFIG.machine_slot)
    chosen = {**prof.DEFAULT_PROFILE, "machine": relative_to_datadir(slot)}
    stored = read_profile_document()
    chosen.update({k: v for k, v in stored.items() if k in chosen and v})
    return {k: datadir / v for k, v in chosen.items()}


def profile_labels() -> dict:
    """The extra (non-preset) keys stored next to the selection, e.g. machine_label."""
    return {k: v for k, v in read_profile_document().items() if k not in prof.DEFAULT_PROFILE}


def profile_options() -> dict[str, bool]:
    """The basic slicing options, always fully populated (defaults applied)."""
    return prof.read_options(read_profile_document())


def write_profile_file(data: dict) -> None:
    prof.write_json_atomic(CONFIG.profile_file, data)


def apply_pairing(view: dict) -> dict | None:
    """Copy one printer's pairing onto the global profile selection.

    A printer without a pairing inherits whatever profile.json already holds, so the file
    is left untouched — byte-identical — rather than rewritten with the values it happens
    to have. That keeps "this printer's own presets" and "the global selection" two
    distinct states the operator can switch between.
    """
    profiles = view.get("profiles")
    if not isinstance(profiles, dict):
        return None
    chosen = {slot: profiles.get(slot) for slot in printers.PROFILE_SLOTS
              if isinstance(profiles.get(slot), str) and profiles.get(slot)}
    if not chosen:
        return None
    merged = {k: relative_to_datadir(v) for k, v in active_paths().items()}
    merged.update(profile_labels())
    merged.update(chosen)
    machine = _preset_path(chosen.get("machine"))
    if machine is not None:
        merged["machine_label"] = prof.summarise(machine, "machine", CONFIG.orca_datadir)["name"]
    write_profile_file(merged)
    return merged


def write_pairing_back(chosen: dict[str, str]) -> None:
    """Record a dashboard selection on the printer it now applies to.

    The pairing belongs to the printer, so a selection made on the dashboard has to reach
    the selected printer or the two views of "the active profile" drift apart. Only the
    slots that were chosen are touched, and the env seed is skipped: it is read-only and
    has no store record to write.
    """
    slots = {slot: chosen[slot] for slot in printers.PROFILE_SLOTS if chosen.get(slot)}
    if not slots:
        return
    try:
        target = STORE.active(SEED)
        if target is None or target.source != "store":
            return
        stored = STORE.get(target.id)
        pairing = {slot: (stored.profiles or {}).get(slot)
                   for slot in printers.PROFILE_SLOTS}
        pairing.update(slots)
        STORE.update(target.id, profiles=pairing)
    except (printers.PrinterError, printers.PrinterStoreError):
        return      # a selection that cannot be written back must still succeed


def regenerate_derived(document: dict) -> None:
    """Refresh the options-carrying process preset; never fails the request.

    Saving options is a UI action that has to succeed even when the selected process
    preset was deleted or the datadir is read-only: the stale preset then shows up as
    ``derived.stale`` on `GET /api/profile`, which is diagnosable, instead of a 500.

    Takes `DERIVED_LOCK` so it cannot rewrite the shared preset between `run_slice`
    writing it and the slicer reading it; both endpoints here are plain `def`, so the
    wait happens on the request's thread-pool thread, not on the event loop.
    """
    try:
        with DERIVED_LOCK:
            paths = active_paths()
            prof.write_derived_process(paths["process"], CONFIG.orca_datadir,
                                       prof.read_options(document))
    except Exception:  # noqa: BLE001 - best effort, reported through /api/profile
        pass


def profile_snapshot() -> dict:
    """The profile a job is queued with, frozen into job.json.

    Uploading a 3MF switches the global selection, so a queued job has to carry its own
    copy of the presets and options; otherwise it would be sliced with whatever was
    uploaded after it.
    """
    snapshot = {k: relative_to_datadir(v) for k, v in active_paths().items()}
    snapshot["options"] = profile_options()
    return snapshot


# ---------------------------------------------------------------- job storage
def job_dir(jid: str) -> Path:
    """Resolve a job directory. Only the 12-hex ids this app generates are accepted.

    The id arrives from the URL, so validating its shape here is what keeps `..` and
    friends from resolving outside JOBS before any caller can remove or read anything.
    """
    if not JOB_ID_RE.fullmatch(jid):
        raise HTTPException(404, "job not found")
    d = JOBS / jid
    if not d.is_dir():
        raise HTTPException(404, "job not found")
    return d


def read_job(jid: str) -> dict:
    try:
        return json.loads((job_dir(jid) / "job.json").read_text())
    except (OSError, ValueError):
        raise HTTPException(404, "job not found") from None


def write_job(jid: str, **fields) -> dict:
    """Merge ``fields`` into job.json through a sibling temp file + os.replace.

    A reader (the UI, `/api/jobs`) therefore sees either the old or the new file, never a
    half-written one.
    """
    meta = job_dir(jid) / "job.json"
    try:
        data = json.loads(meta.read_text())
    except (OSError, ValueError):
        data = {"id": jid}
    data.update(fields)
    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = meta.with_name(meta.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, meta)
    return data


# ---------------------------------------------------------------- slicer
def job_output_name(job: dict) -> str:
    """The gcode stem for a job: its output_name, else the upload stem for older jobs."""
    return job.get("output_name") or Path(job.get("filename") or "").stem or "model"


def gcode_path(jid: str, job: dict) -> Path | None:
    """SLICED/<jid>/<basename> when present, else the legacy JOBS/<jid>/out/<basename>.

    ``Path(name).name`` keeps a corrupted or hand-edited job.json from resolving outside
    the two directories this app owns.
    """
    name = Path(job.get("gcode") or "").name
    if not name:
        return None
    for candidate in (SLICED / jid / name, JOBS / jid / "out" / name):
        if candidate.is_file():
            return candidate
    return None


def slice_profile(job: dict) -> tuple[dict[str, Path], dict[str, bool]]:
    """The presets and options to slice with: the job's snapshot, else the live profile.

    A snapshot is only trusted when all three presets still resolve inside the datadir —
    job.json is a file on disk, so a hand-edited one must not point the slicer at
    /etc/passwd — and it falls back to the live selection otherwise, which is exactly what
    every job written before the snapshot existed does.
    """
    snapshot = job.get("profile_snapshot")
    if isinstance(snapshot, dict):
        paths: dict[str, Path] = {}
        for kind in ("machine", "process", "filament"):
            target = _preset_path(snapshot.get(kind))
            if target is None:
                break
            paths[kind] = target
        else:
            return paths, prof.read_options(snapshot)
    return active_paths(), profile_options()


# ---------------------------------------------------------------- slicer
def run_slice(jid: str) -> None:
    job = read_job(jid)
    d = job_dir(jid)
    # the 3MF pipeline may queue a rewritten copy; ``Path(...).name`` keeps a corrupted
    # job.json from resolving outside the job's own upload dir
    src = d / "upload" / Path(job.get("slicer_input") or job["filename"]).name
    out = d / "out"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir()

    write_job(jid, status="slicing", error=None, started=time.time())
    paths, options = slice_profile(job)
    # Hand-built environment: the slicer runs headless and must not inherit the Moonraker
    # API key (or anything else) from this process.
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(CONFIG.home_dir),
        "QT_QPA_PLATFORM": "offscreen",
        "DISPLAY": "",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(TMP_DIR),
    }
    t0 = time.time()
    # The derived preset is shared with `regenerate_derived`, so the lock spans writing it,
    # building the argv around it and running the slicer that loads it: otherwise a
    # concurrent option save could replace the file and slice with the live options instead
    # of this job's snapshot.
    with DERIVED_LOCK:
        try:
            process = prof.write_derived_process(paths["process"], CONFIG.orca_datadir, options)
        except Exception as exc:  # noqa: BLE001 - a job error, never a stale-options slice
            write_job(jid, status="failed", error=f"profile: {type(exc).__name__}: {exc}")
            return
        cmd = [
            str(CONFIG.orca_bin),
            "--datadir", str(CONFIG.orca_datadir),
            "--load-settings", f"{paths['machine']};{process}",
            "--load-filaments", str(paths["filament"]),
            "--slice", "0",
            "--outputdir", str(out),
            str(src),
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=str(d), env=env, capture_output=True, text=True,
                timeout=CONFIG.slice_timeout,
            )
        except subprocess.TimeoutExpired:
            write_job(jid, status="failed", error=f"slice timed out after {CONFIG.slice_timeout}s")
            return
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            write_job(jid, status="failed", error=f"{type(exc).__name__}: {exc}")
            return
    elapsed = round(time.time() - t0, 1)
    try:
        result_file = out / "result.json"
        result = json.loads(result_file.read_text()) if result_file.exists() else {}
        gcodes = sorted(out.glob("*.gcode"))
        if not gcodes:
            write_job(jid, status="failed", elapsed=elapsed,
                      error=result.get("error_string") or (proc.stderr or proc.stdout or "")[-600:])
            return
        gcode = gcodes[0]
        stats = parse_gcode_stats(gcode)
        # The sliced dir is the single home for gcode; shutil.move survives a separate
        # SLICED_DIR mount, where os.replace would fail with EXDEV.
        target = SLICED / jid / f"{job_output_name(job)}.gcode"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(gcode), str(target))
        write_job(jid, status="sliced", elapsed=elapsed, gcode=target.name,
                  gcode_dir="sliced", gcode_bytes=target.stat().st_size, stats=stats,
                  layer_height=(result.get("layer_height") or stats.get("layer_height")))
    except Exception as exc:  # noqa: BLE001 - surface anything to the UI
        write_job(jid, status="failed", error=f"{type(exc).__name__}: {exc}")


def worker() -> None:
    while True:
        jid = job_queue.get()
        try:
            run_slice(jid)
        except Exception as exc:  # noqa: BLE001
            try:
                write_job(jid, status="failed", error=f"worker: {exc}")
            except Exception:
                pass
        finally:
            job_queue.task_done()
            try:
                prune()   # oldest finished jobs only; the queue never prunes its own work
            except OSError:
                pass


def prune() -> None:
    """Drop all but the KEEP_JOBS newest finished jobs. Queued/slicing jobs never go."""
    def is_active(d: Path) -> bool:
        try:
            return json.loads((d / "job.json").read_text()).get("status") in ACTIVE_STATUSES
        except (OSError, ValueError, AttributeError):
            return False        # a missing or corrupt job.json is not a reason to keep the dir

    dirs = sorted([d for d in JOBS.iterdir() if d.is_dir()],
                  key=lambda p: p.stat().st_mtime, reverse=True)
    finished = [d for d in dirs if not is_active(d)]
    for old in finished[CONFIG.keep_jobs:]:
        shutil.rmtree(old, ignore_errors=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The slicer worker is the only background thread; the printer is never contacted here.
    threading.Thread(target=worker, daemon=True).start()
    yield


app = FastAPI(title="print-portal", lifespan=lifespan)


def reject_oversize(request: Request, limit: int, message: str) -> None:
    """413 straight from the declared Content-Length, before any body byte is buffered.

    The streaming cap inside the upload loop stays as the second line of defence: a
    chunked request declares no length at all.
    """
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:
        return
    if declared > limit:
        raise HTTPException(413, message)


# ---------------------------------------------------------------- API
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (APP_DIR / "index.html").read_text()


@app.get("/api/health")
def api_health():
    """Readiness only: config is loaded by import, and our own dirs must be writable."""
    problems = []
    for label, path in (("jobs", CONFIG.jobs_dir), ("home", CONFIG.home_dir),
                        ("datadir", CONFIG.orca_datadir), ("sliced", SLICED)):
        if not path.is_dir():
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                problems.append(f"{label} {path} is not creatable: {exc}")
                continue
        if not os.access(path, os.W_OK):
            problems.append(f"{label} {path} is not writable")
    body = {"status": "ok", "app": "print-portal", "version": config.VERSION}
    if problems:
        raise HTTPException(503, "; ".join(problems))
    if active_target() is None:
        raise HTTPException(503, NO_PRINTER)
    return body


@app.get("/api/printer")
def api_printer():
    target = current_target()
    info = current_client().printer_state()
    info["host"] = target.url
    info["printer"] = {"id": target.id, "name": target.name, "source": target.source}
    return info


# ---------------------------------------------------------------- printers API
@app.get("/api/printers")
def api_printers():
    """Masked printer views plus the selection; ``warning`` reports a corrupt file."""
    views = STORE.views(SEED)          # reloads the file, so STORE.corrupt is current too
    return {"printers": views, "selected_id": STORE.selected_id(),
            "env_seed": SEED is not None, "warning": STORE.corrupt}


@app.post("/api/printers", status_code=201)
def api_add_printer(body: dict):
    profiles = pairing_from_body(body.get("profiles")) if "profiles" in body else None
    printer = printer_call(STORE.add, body.get("name"), body.get("url"), body.get("api_key"),
                           profiles=profiles)
    return {"printer": printer_view(printer.id), "printers": STORE.views(SEED),
            "selected_id": STORE.selected_id()}


@app.patch("/api/printers/{pid}")
def api_update_printer(pid: str, body: dict):
    fields = {"name": body.get("name"), "url": body.get("url"),
              "api_key": body.get("api_key")}
    if "profiles" in body:
        # an omitted `profiles` keeps the stored pairing; a supplied one replaces it
        fields["profiles"] = pairing_from_body(body["profiles"])
    printer = printer_call(STORE.update, pid, **fields)
    return {"printer": printer_view(printer.id), "printers": STORE.views(SEED),
            "selected_id": STORE.selected_id()}


@app.delete("/api/printers/{pid}")
def api_delete_printer(pid: str):
    selected = printer_call(STORE.delete, pid)
    return {"deleted": pid, "printers": STORE.views(SEED), "selected_id": selected}


@app.post("/api/printers/{pid}/select")
def api_select_printer(pid: str):
    printer_call(select_printer, pid)
    apply_pairing(printer_view(pid))
    return {"selected_id": STORE.selected_id(), "printers": STORE.views(SEED)}


@app.post("/api/printers/{pid}/test")
def api_test_printer(pid: str):
    """Probe one printer without touching the selection; an offline printer is a 200."""
    target = printer_target(pid)
    started = time.monotonic()
    info = moonraker.MoonrakerClient(target.url, target.api_key,
                                     CONFIG.moonraker_root).printer_state()
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {"ok": bool(info.get("ok")), "state": info.get("state") or "unknown",
            "webhooks": info.get("webhooks"), "error": info.get("error"),
            "url": target.url, "ms": elapsed_ms}


# ---------------------------------------------------------------- upload
def normalise_output_name(raw: object, fallback_stem: str) -> str:
    """A plain gcode stem: no separators, no reserved device names, at most 60 characters.

    An empty ``raw`` falls back to the uploaded file's stem, which is cleaned the same way;
    a stem that cannot be saved falls back to ``model``.
    """
    name = str(raw or "").strip()
    if name:
        return _validated_output_name(name)
    try:
        return _validated_output_name(str(fallback_stem or "").strip())
    except HTTPException:
        return "model"


def _validated_output_name(name: str) -> str:
    """Raise HTTP 400 for anything that is not a plain, portable file stem."""
    if (not name or name in (".", "..") or name.startswith(".")
            or "/" in name or "\\" in name
            or any(char < " " or char == "\x7f" for char in name)):
        raise HTTPException(400, OUTPUT_NAME_SEPARATORS)
    if name.split(".", 1)[0].strip().upper() in WINDOWS_RESERVED:
        raise HTTPException(400, OUTPUT_NAME_SEPARATORS)
    if name.lower().endswith(".gcode"):
        name = name[:-len(".gcode")]
    name = name.rstrip(" .")
    if not name:
        raise HTTPException(400, OUTPUT_NAME_SEPARATORS)
    offending = sorted(set(re.sub(OUTPUT_NAME_ALLOWED_RE, "", name)))
    if offending:
        raise HTTPException(
            400, "output_name may only contain letters, digits, spaces, dots, dashes and "
                 f"underscores: remove {', '.join(repr(char) for char in offending)}")
    if len(name) > OUTPUT_NAME_MAX:
        raise HTTPException(400, f"output_name must be at most {OUTPUT_NAME_MAX} characters")
    return name


def prepare_3mf(model_path: Path) -> tuple[dict, str | None]:
    """Switch this portal to the printer/process a 3MF names; re-identify the file.

    Returns ``(record, slicer_input)``: ``record`` is stored as ``job["threemf"]``, and
    ``slicer_input`` is the job-local ``slice-<stem>.3mf`` to slice instead of the upload
    (None when the original bytes are what gets sliced).

    Everything here is best effort: a printer or process this datadir does not know keeps
    the current selection and lands in ``notes``, because the operator would rather get a
    slice attempt they can explain than a failed upload.
    """
    datadir = CONFIG.orca_datadir
    presets = prof.list_presets(datadir)
    info = threemf.read_info(model_path)
    machine = threemf.match_machine(presets["machine"], info)
    process = threemf.match_process(presets["process"], info, machine)
    chosen_id, notes = threemf.choose_printer(STORE.views(SEED), info, machine,
                                             STORE.selected_id())
    if info.process and process is None:
        notes.append(f"process '{info.process}' from the 3MF is not configured here")
    named_machine = info.printer or info.printer_model
    if machine is None and named_machine:
        notes.append(f"machine '{named_machine}' from the 3MF is not configured here")

    if chosen_id is not None:
        printer_call(select_printer, chosen_id)

    # profile.json must never hold a pairing the slicer refuses: a process whose
    # `compatible_printers` names another machine breaks this upload and every later STL
    # upload until an operator notices. So the process is applied only when the machine the
    # file actually selects permits it, and a machine this datadir does not know leaves the
    # stored selection untouched (README: "left alone") with the reason in `notes`.
    current = {k: relative_to_datadir(v) for k, v in active_paths().items()}
    paired = _paired_process(machine, process, current["process"])
    if machine is None:
        if process is not None:
            notes.append(f"process '{process.get('name')}' from the 3MF is not applied: "
                         f"its machine is not configured here")
    elif paired is None:
        notes.append(f"machine '{machine.get('name')}' from the 3MF has no process preset "
                     f"here that it permits; kept the stored pairing")
    else:
        if process is not None and paired is not process:
            notes.append(f"process '{process.get('name')}' from the 3MF is not compatible "
                         f"with machine '{machine.get('name')}'; kept '{paired['name']}'")
        merged = dict(current)
        merged.update(profile_labels())
        merged["machine"] = machine["rel"]
        merged["machine_label"] = machine.get("name")
        merged["process"] = paired["rel"]
        write_profile_file(merged)

    try:
        blob = model_path.read_bytes()
    except OSError:
        blob = b""
    persisted = machine is not None and paired is not None
    # Both the record and the rewritten copy describe the pairing the slicer will actually
    # load — the one profile.json now holds — and never the 3MF's own names: stamping an
    # incompatible process into the job-local copy would make the slicer refuse the very
    # file this rewrite exists to make acceptable. When nothing was persisted both halves
    # are left alone and the upload is sliced exactly as it arrived.
    rewritten, outcome = threemf.rewrite_identity(blob, machine if persisted else None,
                                                  paired if persisted else None)
    slicer_input: str | None = None
    if outcome != "none":
        slicer_input = f"slice-{model_path.stem}.3mf"
        (model_path.parent / slicer_input).write_bytes(rewritten)
    # `paired` is what profile.json now holds when it was switched; otherwise the record
    # keeps reporting the process the file named, with the reason in `notes`
    record = {"printer": chosen_id, "process": (paired or process or {}).get("rel"),
              "rewrite": outcome, "notes": notes}
    return record, slicer_input


@app.post("/api/upload")
async def api_upload(request: Request, file: UploadFile = File(...),
                     output_name: str | None = Form(None)):
    reject_oversize(request, CONFIG.max_upload_bytes,
                    f"file larger than {CONFIG.max_upload_bytes // (1024*1024)} MB")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(400, f"unsupported file type '{suffix}' (allowed: {', '.join(sorted(ALLOWED))})")
    upload_name = Path(file.filename or f"model{suffix}").name
    name = normalise_output_name(output_name, Path(upload_name).stem)
    jid = uuid.uuid4().hex[:12]
    d = JOBS / jid
    (d / "upload").mkdir(parents=True)
    dest = d / "upload" / upload_name
    size = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > CONFIG.max_upload_bytes:
                shutil.rmtree(d, ignore_errors=True)
                raise HTTPException(413, f"file larger than {CONFIG.max_upload_bytes // (1024*1024)} MB")
            fh.write(chunk)

    model_path = dest
    archive = None
    if suffix == ".zip":
        # the archive itself stays in the job dir: it is what the operator uploaded, and
        # the extracted member is what gets sliced
        try:
            selection = archives.extract_model(dest, d / "upload", allowed=ALLOWED,
                                               max_uncompressed=CONFIG.max_upload_bytes)
        except archives.ArchiveError as exc:
            shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(exc.status, str(exc)) from exc
        model_path = selection.written
        archive = {"name": upload_name, "member": selection.member,
                   "candidates": selection.candidates, "skipped": selection.skipped,
                   "members": selection.members}
        if not (output_name or "").strip():
            name = normalise_output_name(None, model_path.stem)

    record = None
    slicer_input = None
    try:
        if model_path.suffix.lower() == ".3mf":
            record, slicer_input = prepare_3mf(model_path)

        fields = {"status": "queued", "filename": model_path.name, "output_name": name,
                  "upload_bytes": size, "model_bytes": model_path.stat().st_size,
                  "profile_snapshot": profile_snapshot(),
                  "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if archive is not None:
            fields["archive"] = archive
        if record is not None:
            fields["threemf"] = record
        if slicer_input is not None:
            fields["slicer_input"] = slicer_input
        write_job(jid, **fields)
    except BaseException:
        # The job dir exists from here on but the job was never queued: a failed upload must
        # not leave an orphan behind (MemoryError included, and `BaseException` so an
        # interrupted request is cleaned up too). The rejection still reaches the client.
        shutil.rmtree(d, ignore_errors=True)
        raise
    job_queue.put(jid)
    body = {"id": jid, "status": "queued", "queue_position": job_queue.qsize()}
    if archive is not None:
        body["archive"] = archive
    if record is not None:
        body["threemf"] = record
    return body


@app.get("/api/jobs")
def api_jobs():
    out = []
    for d in sorted(JOBS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = d / "job.json"
        if meta.exists():
            try:
                out.append(json.loads(meta.read_text()))
            except json.JSONDecodeError:
                continue
    return out


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    return read_job(jid)


@app.get("/api/jobs/{jid}/gcode")
def api_gcode(jid: str):
    job = read_job(jid)
    gcode = gcode_path(jid, job)
    if gcode is None:
        raise HTTPException(409, "not sliced yet")
    return FileResponse(gcode, filename=job_output_name(job) + ".gcode")


@app.post("/api/jobs/{jid}/print")
def api_print(jid: str):
    job = read_job(jid)
    if job.get("status") != "sliced":
        raise HTTPException(409, f"job is '{job.get('status')}', not sliced")
    gcode = gcode_path(jid, job)
    if gcode is None:
        raise HTTPException(409, "not sliced yet")

    client = current_client()
    try:
        info = client.printer_status()
    except moonraker.MoonrakerError as exc:
        raise HTTPException(exc.http_status, str(exc)) from exc
    if info["webhooks"] != "ready":
        raise HTTPException(409, f"klipper not ready: {info['webhooks']}")
    if info["state"] not in ("standby", "complete"):
        raise HTTPException(409, f"printer busy: state={info['state']}")

    remote_name = f"portal_{jid}_{job_output_name(job)}.gcode"[:90]
    try:
        client.upload(gcode, remote_name)
        client.start_print(remote_name)
    except moonraker.MoonrakerError as exc:
        write_job(jid, print_started=False, print_error=str(exc)[-300:])
        raise HTTPException(exc.http_status, str(exc)) from exc

    write_job(jid, printed=datetime.now(timezone.utc).isoformat(timespec="seconds"),
              remote_file=remote_name, print_started=True, print_error=None)
    return {"started": True, "remote_file": remote_name}


@app.delete("/api/jobs")
def api_delete_all_jobs():
    """Bulk cleanup of finished jobs. Every path comes from JOBS itself, never the request."""
    deleted: list[str] = []
    skipped: list[str] = []
    for d in sorted(JOBS.iterdir()):
        if not d.is_dir() or not JOB_ID_RE.fullmatch(d.name):
            continue
        try:
            status = json.loads((d / "job.json").read_text()).get("status")
        except (OSError, ValueError, AttributeError):
            status = None
        if status in ACTIVE_STATUSES:
            skipped.append(d.name)
            continue
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(SLICED / d.name, ignore_errors=True)
        deleted.append(d.name)
    return {"deleted": len(deleted), "skipped": len(skipped),
            "deleted_ids": deleted, "skipped_ids": skipped}


@app.delete("/api/jobs/{jid}")
def api_delete(jid: str):
    shutil.rmtree(job_dir(jid), ignore_errors=True)
    shutil.rmtree(SLICED / jid, ignore_errors=True)
    return {"deleted": jid}


# ---------------------------------------------------------------- profiles
@app.get("/api/profile")
def api_profile():
    datadir = CONFIG.orca_datadir
    paths = active_paths()
    options = profile_options()
    return {
        "source": "uploaded" if CONFIG.profile_file.exists() else "bundled default",
        "machine_label": profile_labels().get("machine_label"),
        "paths": {k: relative_to_datadir(v) for k, v in paths.items()},
        "presets": {k: prof.summarise(v, k, datadir) for k, v in paths.items()},
        "options": options,
        # additive: what the slicer will actually use, and whether the generated process
        # preset still matches the options (stale/error instead of a silent wrong slice)
        "effective": prof.effective_process(paths["process"], datadir, options),
        "derived": prof.derived_state(paths["process"], datadir, options),
    }


@app.get("/api/profiles")
def api_profiles(machine: str | None = None):
    """Selectable presets per type, with the process list narrowed to one machine.

    ``?machine=<rel>`` is advisory: an unknown value is ignored rather than answered with
    a 400, because the UI asks for the filter while the operator is still typing and a
    hard error there would leave it stuck with no list to render.
    """
    datadir = CONFIG.orca_datadir
    presets = prof.list_presets(datadir)
    target = _preset_path(machine) if machine else None
    # a path that is not a machine preset (a process, a filament, profile.json) would be
    # summarised into a nameless pseudo-machine and hide every non-universal process, so a
    # wrong-typed target counts as "no filter", exactly like an unknown one
    if target is not None and _preset_type(target) != "machine":
        target = None
    if target is not None:
        active_rel = relative_to_datadir(active_paths()["process"])
        active_rows = [row for row in presets["process"] if row.get("rel") == active_rel]
        chosen = prof.summarise(target, "machine", datadir)
        presets["process"] = prof.filter_process_by_machine(presets["process"], chosen)
        # the active process stays visible even when the filter would hide it: the picker
        # has to show what the slicer will load, otherwise a switch to a machine with an
        # incompatible preset leaves the operator with no visible selection to fix
        presets["process"] += [row for row in active_rows
                               if row not in presets["process"]]
    return presets


@app.post("/api/profile/upload")
async def api_profile_upload(request: Request, file: UploadFile = File(...)):
    reject_oversize(request, PROFILE_BUNDLE_MAX,
                    f"profile bundle larger than {PROFILE_BUNDLE_MAX // (1024*1024)} MB")
    blob = await file.read()
    if len(blob) > PROFILE_BUNDLE_MAX:
        raise HTTPException(413, "profile bundle larger than 20 MB")
    name = file.filename or "bundle.orca_printer"
    if not name.lower().endswith(PROFILE_BUNDLE_SUFFIXES):
        raise HTTPException(400, "expected an .orca_printer bundle or a preset .json")
    try:
        slot = prof.machine_slot(CONFIG.orca_datadir, CONFIG.machine_slot)
        result = prof.import_into(CONFIG.orca_datadir, blob, name, slot, CONFIG.bed_size)
    except prof.ProfileError as exc:
        raise HTTPException(400, str(exc)) from exc
    result["ok"] = bool(result["imported"])
    machine = next((i for i in result["imported"] if i["type"] == "machine"), None)
    if machine:
        labels = profile_labels()
        labels["machine_label"] = machine["label"]
        write_profile_file(labels)
    return result


@app.post("/api/profile/select")
def api_profile_select(body: dict):
    """Switch the global selection, then refresh the derived options preset.

    The old preset's values are the derived file's source, so switching a process without
    regenerating leaves `derived` holding a copy of the process that is no longer selected
    while `derived.stale` still reads false (that flag only compares option fields).
    """
    chosen: dict[str, str] = {}
    for kind in ("machine", "process", "filament"):
        # one containment + type check for every slot, so a non-string value is a 400 here
        # rather than a 500 inside `Path`
        rel = _profile_rel(kind, body.get(kind))
        if rel is not None:
            chosen[kind] = rel
    if not chosen:
        raise HTTPException(400, "nothing to select")
    merged = {k: relative_to_datadir(v) for k, v in active_paths().items()}
    merged.update(profile_labels())
    merged.update(chosen)
    write_profile_file(merged)
    write_pairing_back(chosen)
    regenerate_derived(merged)
    return {"ok": True, "active": merged}


@app.post("/api/profile/options")
def api_profile_options(body: dict):
    """Merge a partial set of basic options into profile.json.

    ``options`` is a top-level key outside DEFAULT_PROFILE, so profile_labels() carries it
    through every write of the profile document (select, upload) untouched.
    """
    try:
        update = prof.validate_options(body)
    except prof.ProfileError as exc:
        raise HTTPException(400, str(exc)) from exc
    document = read_profile_document()
    merged = {**prof.read_options(document), **update}
    document["options"] = merged
    write_profile_file(document)
    regenerate_derived(document)
    return {"ok": True, "options": merged}
