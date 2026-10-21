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
    GET    /api/profile          active presets + labels
    GET    /api/profiles         selectable presets per type
    POST   /api/profile/upload   import an .orca_printer bundle or a single preset
    POST   /api/profile/select   choose the active presets
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

import config
import moonraker
import printers
import profiles as prof
from gcode import parse_gcode_stats

# The env printer is optional here: printers normally live in printers.json. An unconfigured
# instance still boots so the UI can be used, and printer traffic fails closed per request.
CONFIG = config.load_config(printer_seed_required=False)
APP_DIR = Path(__file__).resolve().parent
ALLOWED = {".stl", ".3mf", ".obj"}
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


def active_paths() -> dict[str, Path]:
    """Which presets the slicer loads: profile.json over the shipped defaults.

    The machine default comes from profiles.machine_slot() so it resolves on any datadir
    (MACHINE_SLOT env -> datadir default -> first instantiated machine).
    """
    datadir = CONFIG.orca_datadir
    slot = prof.machine_slot(datadir, CONFIG.machine_slot)
    chosen = {**prof.DEFAULT_PROFILE, "machine": relative_to_datadir(slot)}
    if CONFIG.profile_file.exists():
        try:
            stored = json.loads(CONFIG.profile_file.read_text())
        except json.JSONDecodeError:
            stored = {}
        chosen.update({k: v for k, v in stored.items() if k in chosen and v})
    return {k: datadir / v for k, v in chosen.items()}


def profile_labels() -> dict:
    """The extra (non-preset) keys stored next to the selection, e.g. machine_label."""
    if not CONFIG.profile_file.exists():
        return {}
    try:
        stored = json.loads(CONFIG.profile_file.read_text())
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in stored.items() if k not in prof.DEFAULT_PROFILE}


def write_profile_file(data: dict) -> None:
    CONFIG.profile_file.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.profile_file.write_text(json.dumps(data, indent=2))


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


# ---------------------------------------------------------------- slicer
def run_slice(jid: str) -> None:
    job = read_job(jid)
    d = job_dir(jid)
    src = d / "upload" / job["filename"]
    out = d / "out"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir()

    write_job(jid, status="slicing", error=None, started=time.time())
    paths = active_paths()
    cmd = [
        str(CONFIG.orca_bin),
        "--datadir", str(CONFIG.orca_datadir),
        "--load-settings", f"{paths['machine']};{paths['process']}",
        "--load-filaments", str(paths["filament"]),
        "--slice", "0",
        "--outputdir", str(out),
        str(src),
    ]
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
    try:
        proc = subprocess.run(
            cmd, cwd=str(d), env=env, capture_output=True, text=True,
            timeout=CONFIG.slice_timeout,
        )
        elapsed = round(time.time() - t0, 1)
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
    except subprocess.TimeoutExpired:
        write_job(jid, status="failed", error=f"slice timed out after {CONFIG.slice_timeout}s")
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
    printer = printer_call(STORE.add, body.get("name"), body.get("url"), body.get("api_key"))
    return {"printer": printer_view(printer.id), "printers": STORE.views(SEED),
            "selected_id": STORE.selected_id()}


@app.patch("/api/printers/{pid}")
def api_update_printer(pid: str, body: dict):
    printer = printer_call(STORE.update, pid, name=body.get("name"), url=body.get("url"),
                           api_key=body.get("api_key"))
    return {"printer": printer_view(printer.id), "printers": STORE.views(SEED),
            "selected_id": STORE.selected_id()}


@app.delete("/api/printers/{pid}")
def api_delete_printer(pid: str):
    selected = printer_call(STORE.delete, pid)
    return {"deleted": pid, "printers": STORE.views(SEED), "selected_id": selected}


@app.post("/api/printers/{pid}/select")
def api_select_printer(pid: str):
    printer_call(select_printer, pid)
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


@app.post("/api/upload")
async def api_upload(request: Request, file: UploadFile = File(...),
                     output_name: str | None = Form(None)):
    reject_oversize(request, CONFIG.max_upload_bytes,
                    f"file larger than {CONFIG.max_upload_bytes // (1024*1024)} MB")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(400, f"unsupported file type '{suffix}' (allowed: {', '.join(sorted(ALLOWED))})")
    stem = Path(file.filename or f"model{suffix}").stem
    name = normalise_output_name(output_name, stem)
    jid = uuid.uuid4().hex[:12]
    d = JOBS / jid
    (d / "upload").mkdir(parents=True)
    dest = d / "upload" / Path(file.filename or f"model{suffix}").name
    size = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > CONFIG.max_upload_bytes:
                shutil.rmtree(d, ignore_errors=True)
                raise HTTPException(413, f"file larger than {CONFIG.max_upload_bytes // (1024*1024)} MB")
            fh.write(chunk)
    write_job(jid, status="queued", filename=dest.name, output_name=name, upload_bytes=size,
              created=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    job_queue.put(jid)
    return {"id": jid, "status": "queued", "queue_position": job_queue.qsize()}


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
    return {
        "source": "uploaded" if CONFIG.profile_file.exists() else "bundled default",
        "machine_label": profile_labels().get("machine_label"),
        "paths": {k: relative_to_datadir(v) for k, v in paths.items()},
        "presets": {k: prof.summarise(v, k, datadir) for k, v in paths.items()},
    }


@app.get("/api/profiles")
def api_profiles():
    return prof.list_presets(CONFIG.orca_datadir)


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
    datadir = CONFIG.orca_datadir
    chosen: dict[str, str] = {}
    for kind in ("machine", "process", "filament"):
        rel = body.get(kind)
        if not rel:
            continue
        target = (datadir / rel).resolve()
        if not target.is_relative_to(datadir.resolve()) or not target.is_file():
            raise HTTPException(400, f"{kind}: '{rel}' is not a preset inside the datadir")
        chosen[kind] = rel
    if not chosen:
        raise HTTPException(400, "nothing to select")
    merged = {k: relative_to_datadir(v) for k, v in active_paths().items()}
    merged.update(profile_labels())
    merged.update(chosen)
    write_profile_file(merged)
    return {"ok": True, "active": merged}
