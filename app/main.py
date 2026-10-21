#!/usr/bin/env python3
"""print-portal — upload a model, slice it with OrcaSlicer, print it over Moonraker.

Slicing runs on the host serving this app; the printer only receives the finished gcode
through Moonraker's HTTP API, so slicing never competes with klippy for CPU.

Endpoints
    GET    /                     upload UI
    GET    /api/health           process readiness — never touches the printer
    GET    /api/printer          live printer snapshot
    POST   /api/upload           multipart file -> creates a job, queues the slice
    GET    /api/jobs             job list
    GET    /api/jobs/{id}        job status + stats
    GET    /api/jobs/{id}/gcode  download the sliced gcode
    POST   /api/jobs/{id}/print  upload the gcode to Moonraker and start it
    DELETE /api/jobs/{id}        remove job directory
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

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

import config
import moonraker
import profiles as prof
from gcode import parse_gcode_stats

CONFIG = config.load_config()
APP_DIR = Path(__file__).resolve().parent
ALLOWED = {".stl", ".3mf", ".obj"}
PROFILE_BUNDLE_MAX = prof.BUNDLE_MAX_BYTES
PROFILE_BUNDLE_SUFFIXES = (".orca_printer", ".json", ".zip")
JOB_ID_RE = re.compile(r"[0-9a-f]{12}")
ACTIVE_STATUSES = ("queued", "slicing")

JOBS = CONFIG.jobs_dir
TMP_DIR = CONFIG.data_dir / "tmp"
JOBS.mkdir(parents=True, exist_ok=True)
CONFIG.home_dir.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

client = moonraker.MoonrakerClient(CONFIG.printer_url, CONFIG.moonraker_api_key,
                                   CONFIG.moonraker_root)
job_queue: "queue.Queue[str]" = queue.Queue()


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
def run_slice(jid: str) -> None:
    d = job_dir(jid)
    src = d / "upload" / read_job(jid)["filename"]
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
        write_job(jid, status="sliced", elapsed=elapsed, gcode=gcode.name,
                  gcode_bytes=gcode.stat().st_size, stats=stats,
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
                        ("datadir", CONFIG.orca_datadir)):
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
    return body


@app.get("/api/printer")
def api_printer():
    info = client.printer_state()
    info["host"] = CONFIG.printer_url
    return info


@app.post("/api/upload")
async def api_upload(request: Request, file: UploadFile = File(...)):
    reject_oversize(request, CONFIG.max_upload_bytes,
                    f"file larger than {CONFIG.max_upload_bytes // (1024*1024)} MB")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(400, f"unsupported file type '{suffix}' (allowed: {', '.join(sorted(ALLOWED))})")
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
    write_job(jid, status="queued", filename=dest.name, upload_bytes=size,
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
    gcode = job_dir(jid) / "out" / (job.get("gcode") or "")
    if not job.get("gcode") or not gcode.exists():
        raise HTTPException(409, "not sliced yet")
    return FileResponse(gcode, filename=Path(job["filename"]).stem + ".gcode")


@app.post("/api/jobs/{jid}/print")
def api_print(jid: str):
    job = read_job(jid)
    if job.get("status") != "sliced":
        raise HTTPException(409, f"job is '{job.get('status')}', not sliced")

    try:
        info = client.printer_status()
    except moonraker.MoonrakerError as exc:
        raise HTTPException(exc.http_status, str(exc)) from exc
    if info["webhooks"] != "ready":
        raise HTTPException(409, f"klipper not ready: {info['webhooks']}")
    if info["state"] not in ("standby", "complete"):
        raise HTTPException(409, f"printer busy: state={info['state']}")

    gcode = job_dir(jid) / "out" / job["gcode"]
    remote_name = f"portal_{jid}_{Path(job['filename']).stem}.gcode"[:90]
    try:
        client.upload(gcode, remote_name)
        client.start_print(remote_name)
    except moonraker.MoonrakerError as exc:
        write_job(jid, print_started=False, print_error=str(exc)[-300:])
        raise HTTPException(exc.http_status, str(exc)) from exc

    write_job(jid, printed=datetime.now(timezone.utc).isoformat(timespec="seconds"),
              remote_file=remote_name, print_started=True, print_error=None)
    return {"started": True, "remote_file": remote_name}


@app.delete("/api/jobs/{jid}")
def api_delete(jid: str):
    shutil.rmtree(job_dir(jid), ignore_errors=True)
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
