# API

Every route registered by `app/main.py` (`@app.…` decorators). There are exactly 21, and the route
**set** is identical in both states:

* **A** = `develop` @ `dc92ca1` (this worktree, `main.py` 1208 lines);
* **B** = the uncommitted `ergo/20261022T123034Z-import-name` branch, read from a read-only snapshot
  (`main.py` 1308 lines, `main.py:750-1308`).

Line numbers quoted below are from B unless marked otherwise. Only the preset routes and
`POST /api/upload` behave differently — see [Presets](#presets). Everything else is unchanged between
the two states.

Error bodies are FastAPI's `{"detail": …}`. In state B, the no-printer condition raises a detail object
(absent in state A, which has no empty-datadir state):

```json
{"error": "no_printer_profile",
 "detail": "no printer profile imported yet",
 "hint": "upload an .orca_printer bundle exported from OrcaSlicer (File → Export → Export Preset Bundle, tick Printer)"}
```

(`NO_PRINTER_DETAIL`, B `main.py:336`.)

## UI and readiness

| Method | Path | Purpose | Notable statuses |
|---|---|---|---|
| GET | `/` | serves `app/index.html` | 200 |
| GET | `/api/health` | readiness: our dirs exist/writable **and** a printer resolves. Never touches the printer | 200 `{"status":"ok","app":"print-portal","version":…}` · 503 when a dir is not creatable/writable, or `no printer configured: add one in Settings or set PRINTER_URL and MOONRAKER_API_KEY` |

`/api/health` does **not** probe Moonraker (`api_health`, `main.py:755`). A portal with no printer
configured is therefore *unhealthy* — see [troubleshooting.md](troubleshooting.md).

## Printer (live state)

| Method | Path | Purpose | Notable statuses |
|---|---|---|---|
| GET | `/api/printer` | live snapshot of the selected printer: state, file, progress, temps, homed axes; adds `host` and `printer{id,name,source}` | 503 no printer · 500 when Moonraker fails (a `MoonrakerError` is not caught on this route) |

## Printer store

| Method | Path | Purpose | Notable statuses |
|---|---|---|---|
| GET | `/api/printers` | masked printer views, `selected_id`, `env_seed`, `warning` (corrupt file) | 200 |
| POST | `/api/printers` | add a printer, optional `profiles` pairing | 201 · 400 invalid body · 409 pairing refused / store error |
| PATCH | `/api/printers/{pid}` | change name/url/api_key and/or pairing. On the `env` row only `profiles` is accepted | 200 · 404 unknown · 409 read-only env row (`name`/`url`/`api_key` supplied) or pairing refused · 400 `nothing to update` |
| DELETE | `/api/printers/{pid}` | remove a printer | 200 · 404 · 409 the `env` row is read-only |
| POST | `/api/printers/{pid}/select` | make it active and apply its pairing to `profile.json` | 200 · 404 · 409 pairing refused |
| POST | `/api/printers/{pid}/test` | reachability probe, never raises for an offline printer | 200 always (`ok:false`, `error`, `ms`) · 404 unknown id |

Status mapping is centralised in `printer_call` (`main.py:121`): `PrinterNotFound` → 404,
`PrinterReadOnly` → 409, `PrinterError` → 400, `PrinterStoreError` → 409. The env seed is read-only
because its URL and key are deployment-owned (`READ_ONLY_ERROR`, `printers.py:46`).

A pairing is a complete statement about a printer, not a patch: a slot the body omits is cleared
(`pairing_from_body`, `main.py:288`).

## Jobs and slicing

| Method | Path | Purpose | Notable statuses |
|---|---|---|---|
| POST | `/api/upload` | multipart `file` (`.stl` `.3mf` `.obj` `.zip`) + optional `output_name` → queue a slice | 200 `{id,status:"queued",queue_position}` · 400 unsupported suffix / invalid `output_name` · **409 `no_printer_profile`** · 413 larger than `MAX_UPLOAD_MB` (from `Content-Length` or the streaming count) · archives: `ArchiveError.status`, 400 or 413 |
| GET | `/api/jobs` | job list, newest first (every `job.json` that parses) | 200 |
| GET | `/api/jobs/{jid}` | one job: status, stats, error, `threemf`, `archive` | 200 · 404 (`jid` must be 12 lowercase hex) |
| GET | `/api/jobs/{jid}/gcode` | download the sliced gcode | 200 file · 409 `not sliced yet` · 404 |
| POST | `/api/jobs/{jid}/print` | upload to Moonraker and start the print | 200 `{started:true,remote_file}` · 409 `job is '<status>', not sliced` / `not sliced yet` / `klipper not ready: <state>` / `printer busy: state=<state>` · Moonraker error status (502/503/409) |
| DELETE | `/api/jobs/{jid}` | remove the job dir and its sliced output | 200 `{deleted}` |
| DELETE | `/api/jobs` | remove every finished job, skipping active ones | 200 `{deleted,skipped,deleted_ids,skipped_ids}` |

`/api/upload` calls `require_machine()` **before** creating the job directory, so a 409 leaves nothing
on disk (`main.py:997`). `output_name` rules: no path separators, not a Windows reserved device name,
at most 60 characters, `[A-Za-z0-9 ._-]` only (`_validated_output_name`, `main.py:891`).

## Presets

Two states, different behaviour. **A** = `develop` @ `dc92ca1` (this worktree). **B** = the uncommitted
`ergo/20261022T123034Z-import-name` branch, read from a read-only snapshot. Nothing in B is in develop.
Full pipeline: [presets.md](presets.md).

| Method | Path | Purpose | Statuses — A (`dc92ca1`) | Statuses — B (uncommitted) |
|---|---|---|---|---|
| GET | `/api/profile` | active presets, summaries, options, `effective`, `derived` | 200; **500** if the datadir has no machine preset (`ProfileError` escapes `active_paths()`) | **always 200**; fresh install → `"imported": false`, nulls |
| GET | `/api/profiles` | selectable presets; `?machine=<rel>` narrows `process` | 200 (an unknown/wrong-typed `machine` is ignored) | 200, same rule; empty lists on a fresh install |
| POST | `/api/profile/upload` | import `.orca_printer` / `.zip` / `.json` | 200 `{imported:[{type,name,label,file,path,rebased_on}],notes,ok}` · 400 unreadable bundle · 413 > 20 MB · **500** when no machine preset resolves | 200 `{…,"registered":true}` · 400 unreadable bundle **or name collision** · 413 > 20 MB |
| POST | `/api/profile/select` | choose the active presets | 200 · 400 not-a-preset / `nothing to select` / not permitted · **500** when no machine resolves | 200 · 400 same · **409 `no_printer_profile`** |
| POST | `/api/profile/options` | set `supports` / `brim` / `hide_profiles` | 200 · 400 `options must be an object` / `unknown option: <key>` / `<key> must be true or false` / `nothing to update` | identical |

`POST /api/upload` differs the same way: A answers **500** when no machine preset resolves (the raise
happens inside `profile_snapshot()`, after the job dir exists — the `BaseException` handler removes it);
B answers **409 `no_printer_profile`** before creating the job dir (`main.py:997`).

### The read/action rule (state B only)

* **Reads never fail for an empty datadir.** `selection_or_none()` (`main.py:368`) returns `None`
  and `api_profile` renders an empty state.
* **Actions always 409.** `require_machine()` (`main.py:354`) is the single entry point for "this action
  needs presets": `/api/upload`, `/api/profile/select`, and `resolve_pairing()`'s explicit-pairing
  branch (`POST /api/printers`, `PATCH /api/printers/{pid}`, `POST /api/printers/{pid}/select`).

In state A neither exists: there is no empty-datadir concept, so there is no 409 body and no
`"imported"` flag.

### Why a selection is refused

`_reject_unpermitted_pair` (`main.py:260`) checks the machine/process pair the document is *about to
hold* against `filter_process_by_machine`. It runs before anything is written, so a refusal leaves
`profile.json`, the stored pairing and the derived preset exactly as they were. A filament-only pick
is never refused. This is the `-17` guard — see [presets.md](presets.md#3-the--17-rule).

## Routes that do not exist

There is no authentication, no `/api/jobs/{jid}/cancel`, no printer delete-all, no logs endpoint, and
no websocket. The UI polls `/api/jobs` and `/api/printer`. Anything not listed above answers FastAPI's
404.
