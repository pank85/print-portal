#!/usr/bin/env python3
"""Build-time smoke test: import the bundle, then slice a cube with the image's presets.

The seeded datadir ships no preset, so the machine/process/filament the CLI is given come
from importing ``--bundle`` through the same ``profiles.import_into`` the app uses.

``--fins-only`` instead exercises the opt-in support-fins path: node + ``app/support_fins.mjs``
run through ``supportfins.fin_model``, and the 3MF they write is read back with
``preview.read_scene`` the way ``GET /api/jobs/{id}/preview`` does. It needs no OrcaSlicer, so
it also runs on a dev host. Use a fixture that actually grows fins (``overhang.stl``);
``cube.stl`` is shallow enough that the CLI legitimately returns zero fins.

Exits 0 and prints a single line starting ``smoke: ok`` on success; exits 1 with
diagnostics on any failure. Runnable as uid 1000.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def fail(msg: str, proc: subprocess.CompletedProcess | None = None) -> None:
    print(msg, file=sys.stderr)
    if proc is not None:
        print(f"exit code: {proc.returncode}", file=sys.stderr)
        out = (proc.stdout or "").splitlines()
        err = (proc.stderr or "").splitlines()
        if out:
            print("--- stdout (last 40) ---", file=sys.stderr)
            for line in out[-40:]:
                print(line, file=sys.stderr)
        if err:
            print("--- stderr (last 40) ---", file=sys.stderr)
            for line in err[-40:]:
                print(line, file=sys.stderr)
        print("hint: missing shared library? run: ldd <orca> | grep 'not found'", file=sys.stderr)
        print("hint: offscreen failed? try xvfb-run", file=sys.stderr)
    raise SystemExit(1)


def fins_fail(msg: str) -> None:
    print(msg, file=sys.stderr)
    print("hint: support fins need node on PATH and app/support_fins.mjs beside app/supportfins.py",
          file=sys.stderr)
    raise SystemExit(1)


def check_fins(stl: Path, workdir: Path, app_dir: str) -> None:
    """Run the bundled CLI once and check the finned 3MF the preview dialog would read."""
    if not stl.is_file():
        fins_fail(f"smoke: fins input missing: {stl}")
    sys.path.insert(0, app_dir)
    try:
        import preview
        import supportfins
    except Exception as e:
        fins_fail(f"smoke: cannot import supportfins/preview: {e}")

    reason = supportfins.unavailable_reason()
    if reason:
        fins_fail(f"smoke: support fins unavailable: {reason}")

    outdir = workdir / "fins"
    outdir.mkdir(parents=True, exist_ok=True)
    dest = outdir / f"{stl.stem}-fins.3mf"
    try:
        result = supportfins.fin_model(stl, dest)
    except supportfins.SupportFinsError as e:
        fins_fail(f"smoke: support fins failed: {e}")
    if not dest.is_file() or dest.stat().st_size == 0:
        fins_fail(f"smoke: support fins wrote no 3MF at {dest}")

    try:
        scene = preview.read_scene(dest)
    except preview.PreviewError as e:
        fins_fail(f"smoke: finned 3MF is not readable: {e}")
    supports = [o for o in scene.get("objects", [])
                if o.get("role") == "supports" and o.get("triangles")]
    if not supports:
        fins_fail(f"smoke: no support geometry in {dest.name}: {result.get('stats')}")

    stats = result.get("stats") or {}
    print(f"smoke: ok {dest.name} objects={len(scene['objects'])} supports={len(supports)} "
          f"finTriangles={stats.get('finTriangles')} tines={stats.get('tines')} "
          f"report={result.get('report')!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orca", help="orca-slicer binary (slice step only)")
    ap.add_argument("--datadir", help="seed datadir to copy (slice step only)")
    ap.add_argument("--stl", required=True, help="model to slice, or the fins fixture")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--app-dir", default="/app")
    ap.add_argument("--bundle", default="/opt/smoke/bundle.orca_printer")
    ap.add_argument("--bed", default="220x220")
    ap.add_argument("--fins-only", action="store_true",
                    help="only the support-fins step (needs node, not OrcaSlicer)")
    args = ap.parse_args()

    if args.fins_only:
        check_fins(Path(args.stl), Path(args.workdir), args.app_dir)
        return
    if not args.orca or not args.datadir:
        ap.error("--orca and --datadir are required unless --fins-only is given")
    bed = tuple(float(part) for part in args.bed.lower().split("x"))

    workdir = Path(args.workdir)
    datadir = workdir / "datadir"
    outdir = workdir / "out"
    homedir = workdir / "home"
    for d in (datadir, outdir, homedir):
        d.mkdir(parents=True, exist_ok=True)
    # fresh datadir copy
    if datadir.exists():
        shutil.rmtree(datadir)
    shutil.copytree(args.datadir, datadir, dirs_exist_ok=True)

    env = dict(os.environ)
    env["HOME"] = str(homedir)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["DISPLAY"] = ""
    env["LC_ALL"] = "C"

    sys.path.insert(0, args.app_dir)
    try:
        import profiles
    except Exception as e:
        fail(f"smoke: cannot import profiles: {e}")

    bundle = Path(args.bundle)
    try:
        result = profiles.import_into(datadir, bundle.read_bytes(), bundle.name, bed)
    except Exception as e:
        fail(f"smoke: import failed: {e}")

    imported = result["imported"]

    def first_path(ptype: str) -> Path | None:
        for row in imported:
            if row["type"] == ptype:
                return datadir / row["path"]
        return None

    machine_path = first_path("machine")
    process_path = first_path("process")
    filament_path = first_path("filament")
    if not result["ok"] or machine_path is None or process_path is None or filament_path is None:
        print("smoke: import failed", file=sys.stderr)
        for note in result.get("notes", []):
            print(f"note: {note}", file=sys.stderr)
        raise SystemExit(1)

    cmd = [
        args.orca,
        "--datadir", str(datadir),
        "--load-settings", f"{machine_path};{process_path}",
        "--load-filaments", str(filament_path),
        "--slice", "0",
        "--outputdir", str(outdir),
        args.stl,
    ]

    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as e:
        print(f"smoke: timeout after {e.timeout}s", file=sys.stderr)
        raise SystemExit(1)

    gcodes = sorted(outdir.glob("*.gcode"))
    if proc.returncode != 0 or not gcodes:
        fail("smoke: slice failed", proc)

    # result.json check
    result_json = outdir / "result.json"
    if result_json.exists():
        try:
            rj = json.loads(result_json.read_text())
            if rj.get("return_code", 0) != 0:
                fail(f"smoke: result.json return_code={rj.get('return_code')}", proc)
        except Exception as e:
            fail(f"smoke: bad result.json: {e}", proc)

    gcode = gcodes[0]
    try:
        from gcode import parse_gcode_stats
    except Exception as e:
        fail(f"smoke: cannot import gcode: {e}")

    stats = parse_gcode_stats(gcode)
    grams = stats.get("grams", 0) or 0
    layers = stats.get("layers", 0) or 0
    time_str = stats.get("time")
    if not (grams and grams > 0):
        fail(f"smoke: grams not > 0: {stats}", proc)
    if not (layers and layers > 0):
        fail(f"smoke: layers not > 0: {stats}", proc)
    if not time_str:
        fail(f"smoke: time empty: {stats}", proc)

    print(f"smoke: ok {gcode.name} {json.dumps(stats)}")


if __name__ == "__main__":
    main()