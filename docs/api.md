# API

Every route registered by `app/main.py` (`@app.…` decorators). The inventory below was captured when
there were exactly 21 and the route **set** was identical in both states:

* **A** = `develop` @ `dc92ca1` (this worktree, `main.py` 1208 lines);
* **B** = the uncommitted `ergo/20261022T123034Z-import-name` branch, read from a read-only snapshot
  (`main.py` 1308 lines, `main.py:750-1308`).

Line numbers quoted below are from B unless marked otherwise. Only the preset routes and
`POST /api/upload` behave differently — see [Presets](#presets). Everything else is unchanged between
the two states. The support-fins work adds three routes on top of that set (`POST
/api/jobs/{jid}/slice`, `GET /api/jobs/{jid}/preview`, `GET /api/support-fins` — § [Support
fins](#support-fins)), so a current `grep -c '^@app\.' app/main.py` says 29 rather than 21 — every one of
them is listed below (the `{kind}/{rel:path}` preset routes appear once each, without the `:path`).

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
| GET | `/api/printer` | live snapshot of the selected printer: state, file, progress, temps, homed axes; adds `host`, `printer{id,name,source}` and `ui_url` (the active printer's embedded-UI address, falling back to `PRINTER_UI_URL`; empty when neither is set) | 503 no printer · 500 when Moonraker fails (a `MoonrakerError` is not caught on this route) |
| GET | `/api/ui` | just the embedded-UI address: `{"printer_ui_url": …}` for the **active** printer, `PRINTER_UI_URL` as the fallback | 200 always — answers `""` only when neither the active printer nor `PRINTER_UI_URL` supplies an address, and the env value otherwise (including with no printer at all or an unreadable store), so the Printer tab can tell "not configured" apart from "cannot reach the printer" |

## Printer store

| Method | Path | Purpose | Notable statuses |
|---|---|---|---|
| GET | `/api/printers` | masked printer views (including `ui_url`), `selected_id`, `env_seed`, `warning` (corrupt file) | 200 |
| POST | `/api/printers` | add a printer, optional `ui_url` and `profiles` pairing | 201 · 400 invalid body (`ui_url must …` names the field) · 409 pairing refused / store error |
| PATCH | `/api/printers/{pid}` | change name/url/api_key/ui_url and/or pairing; an omitted `ui_url` keeps the stored one, `""` clears it. On the `env` row only `profiles` is accepted | 200 · 404 unknown · 409 read-only env row (`name`/`url`/`api_key`/`ui_url` supplied) or pairing refused · 400 `nothing to update` |
| DELETE | `/api/printers/{pid}` | remove a printer | 200 · 404 · 409 the `env` row is read-only |
| POST | `/api/printers/{pid}/select` | make it active and apply its pairing to `profile.json` | 200 · 404 · 409 pairing refused |
| POST | `/api/printers/{pid}/test` | reachability probe, never raises for an offline printer | 200 always (`ok:false`, `error`, `ms`) · 404 unknown id |

Status mapping is centralised in `printer_call` (`main.py:121`): `PrinterNotFound` → 404,
`PrinterReadOnly` → 409, `PrinterError` → 400, `PrinterStoreError` → 409. The env seed is read-only
because its URL, key and embedded-UI address are deployment-owned (`READ_ONLY_ERROR`, `printers.py:46`).

`ui_url` is an optional key on a stored printer record. A `printers.json` written before the field
existed reads back as `""` and gains the key on the next save, so the format needs no migration; an
unusable stored address also reads as `""` rather than corrupting the store.

A pairing is a complete statement about a printer, not a patch: a slot the body omits is cleared
(`pairing_from_body`, `main.py:288`).

## Jobs and slicing

| Method | Path | Purpose | Notable statuses |
|---|---|---|---|
| POST | `/api/upload` | multipart `file` (`.stl` `.3mf` `.obj` `.zip`) + optional `output_name`, optional `support_fins=true` → queue a slice, or stage a finned job | 200 `{id,status:"queued",queue_position}` · 200 `{id,status:"awaiting_confirm",fins:{report,stats},original}` when `support_fins` is set (nothing queued) · 400 `support fins failed: <cli text>` (job dir removed) · 400 unsupported suffix / invalid `output_name` · **409 `no_printer_profile`** · 413 larger than `MAX_UPLOAD_MB` (from `Content-Length` or the streaming count) · archives: `ArchiveError.status`, 400 or 413 |
| GET | `/api/jobs` | job list, newest first (every `job.json` that parses) | 200 |
| GET | `/api/jobs/{jid}` | one job: status, stats, error, `threemf`, `archive`, and for a finned job `original`, `fins`, `support_fins` | 200 · 404 (`jid` must be 12 lowercase hex) |
| GET | `/api/jobs/{jid}/gcode` | download the sliced gcode | 200 file · 409 `not sliced yet` · 404 |
| POST | `/api/jobs/{jid}/print` | upload to Moonraker and start the print | 200 `{started:true,remote_file}` · 409 `job is '<status>', not sliced` / `not sliced yet` / `klipper not ready: <state>` / `printer busy: state=<state>` · Moonraker error status (502/503/409) |
| DELETE | `/api/jobs/{jid}` | remove the job dir and its sliced output | 200 `{deleted}` |
| DELETE | `/api/jobs` | remove every finished job, skipping active ones | 200 `{deleted,skipped,deleted_ids,skipped_ids}` |

`/api/upload` calls `require_machine()` **before** creating the job directory, so a 409 leaves nothing
on disk (`main.py:997`). `output_name` rules: no path separators, not a Windows reserved device name,
at most 60 characters, `[A-Za-z0-9 ._-]` only (`_validated_output_name`, `main.py:891`).

A job object (`GET /api/jobs`, `GET /api/jobs/{jid}` and the SSE stream) also carries the slicing
decisions: `auto_orient`, `auto_orient_used` (true when orient actually ran, e.g. because a shrink
forced it), `shrink_to_fit`, `fit_scale` (`1.0` when nothing was scaled, `null` when the model could
not be measured), plus `model_size` and `bed_limits` as `{x,y,z}` spans in millimetres.

## Support fins

Opt-in designed-in supports, off by default. `POST /api/upload` with `support_fins=true` runs the
bundled CLI inside the request, then **stages** the job instead of queueing it: `status` is
`awaiting_confirm` and the body is `{id, status, fins: {report, stats}, original}` (no `queue_position`).
The status list is therefore `queued` · `slicing` · `awaiting_confirm` · `sliced` · `failed`;
`awaiting_confirm` is deliberately **not** an active status (it can be deleted like a
finished job) but `prune()` keeps it, so an open preview dialog is never collected.

| Method | Path | Purpose | Notable statuses |
|---|---|---|---|
| POST | `/api/jobs/{jid}/slice` | enter the queue for a staged job — the preview dialog's *Slice it* | 200 `{id,status:"queued",queue_position}` · 404 · 409 `job is '<status>', not awaiting confirmation` (a double click is a 409, not a second queue entry: the check and the enqueue share one lock) |
| GET | `/api/jobs/{jid}/preview` | geometry for the pre-slice preview, or `{mode:"shots", views:["front","side"], bytes}` when the file is over `MAX_BYTES` (32 MiB) and at most `shot.SHOT_MAX_BYTES` (150 MiB) | 200 · 404 · 409 `preview unavailable: this job has no support fins` · 409 `preview unavailable: <reason>` |
| GET | `/api/jobs/{jid}/preview-shot?view=front\|side` | one cached PNG of the finned mesh | 200 `image/png` · 400 · 404 · 409 |
| GET | `/api/support-fins` | capability probe for the upload checkbox | 200 always: `{available, reason, timeout, max_triangles}`; `reason` is `""` when available, else `support-fins CLI is missing from the image` / `node runtime is not available` |

A finned job's `filename` is the CLI's own output (`fins-<stem>.3mf`), `model_bytes` its size,
`original` the upload it came from, and `fins` the parsed `--json` line (`report` + `stats`). Because
the CLI's 3MF carries no OrcaSlicer identity metadata, the 3MF is prepared *before* finning and the
rewritten copy is then discarded: such a job has **no** `slicer_input`, and its `threemf` record has
`rewrite: "discarded"` plus a note saying so.

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
| POST | `/api/profile/options` | set `supports` / `brim` / `hide_profiles` / `auto_orient` / `shrink_to_fit` | 200 · 400 `options must be an object` / `unknown option: <key>` / `<key> must be true or false` / `nothing to update` | identical |

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

### Editing presets in the UI (current develop)

| Method | Path | Purpose | Statuses |
|---|---|---|---|
| GET | `/api/presets?kind=machine\|process\|filament[&machine=<rel>]` | preset rows with `editable`, `read_only_reason`, `deletable` | 200 · 400 unknown kind |
| GET | `/api/presets/{kind}/{rel}` | one preset's editable fields, effective values, `own`/`inherited` source | 200 · 404 not a preset of that kind · 400 unreadable |
| PATCH | `/api/presets/{kind}/{rel}` | write overrides onto the preset's own file | 200 · 400 unknown key / bad value / `nothing to update` · 404 · 409 read-only |
| DELETE | `/api/presets/{kind}/{rel}` (`machine` or `process`) | delete; a machine delete also deletes its processes | 200 `{deleted,cleared}` · 404 · 405 filament · 409 read-only |

Only presets under `system/Custom/` are editable/deletable; the derived `print-portal-options.json`
and vendor presets answer `409`. A machine delete also removes every `system/Custom/process` preset
whose effective `compatible_printers` names that machine, and repairs `profile.json`,
`default_print_profile` and printer pairings (`printers.json`).

## Routes that do not exist

There is no authentication, no `/api/jobs/{jid}/cancel`, no printer delete-all, no logs endpoint, and
no websocket. The UI polls `/api/jobs` and `/api/printer`. Anything not listed above answers FastAPI's
404.
