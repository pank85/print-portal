#!/usr/bin/env python3
"""Build-time smoke test: import the bundle, then slice a cube with the image's presets.

The seeded datadir ships no preset, so the machine/process/filament the CLI is given come
from importing ``--bundle`` through the same ``profiles.import_into`` the app uses.

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orca", required=True)
    ap.add_argument("--datadir", required=True)
    ap.add_argument("--stl", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--app-dir", default="/app")
    ap.add_argument("--bundle", default="/opt/smoke/bundle.orca_printer")
    ap.add_argument("--bed", default="220x220")
    args = ap.parse_args()
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