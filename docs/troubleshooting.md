# Troubleshooting

Real failure modes, with the text the app or the slicer actually produces. Each entry is traced to the
symbol that decides the behaviour, and every quoted string was grepped from the side that raises it.

**Two states.** **A** = `develop` @ `dc92ca1` (this worktree). **B** = the uncommitted
`ergo/20261022T123034Z-import-name` branch, read from a read-only snapshot. Some failure modes exist in
only one of them; those are labelled.

## A fresh datadir is not usable — state A: 500

State A has no "nothing imported yet" state. `machine_slot` raises when neither `MACHINE_SLOT`, the
datadir default, nor any instantiated machine is present:

```text
no machine preset found in <datadir> (set MACHINE_SLOT)
```

(`raise ProfileError(...)`, A `profiles.py:189`.) Nothing catches `ProfileError` at the HTTP layer
(`main.py` registers no exception handler), so the client sees **500** on `GET /api/profile`,
`POST /api/upload` (raised inside `profile_snapshot()`, after the job dir exists — cleaned up by the
`BaseException` handler) and `POST /api/profile/upload`.

Fix: restore a machine preset in `$ORCA_DATADIR` (re-seed by bumping `SEED_VERSION`, or point
`MACHINE_SLOT` at one that exists), or move to state B and import a bundle.

## "no printer profile imported yet" — state B only

```json
{"detail": {"error": "no_printer_profile",
            "detail": "no printer profile imported yet",
            "hint": "upload an .orca_printer bundle exported from OrcaSlicer (File → Export → Export Preset Bundle, tick Printer)"}}
```

* **Where:** `409` from `POST /api/upload`, `POST /api/profile/select`, and from
  `POST /api/printers` / `PATCH /api/printers/{pid}` / `POST /api/printers/{pid}/select` when the body
  carries an explicit `profiles` pairing (`require_machine`, B `main.py:354`).
* **Why:** `machine_slot()` returned `None` (B `profiles.py:241`) — nothing imported, and the seed ships
  no presets.
* **Fix:** import a bundle. `GET /api/profile` answers `200` with `"imported": false` — that is the
  empty state, not an error, and it confirms the diagnosis.

Do not "fix" this by pointing `MACHINE_SLOT` at a file that does not exist: a bad override is a
`ProfileError` (`machine slot override does not exist: <rel>`), not an empty datadir.

## `-17` — "The selected printer is not compatible with the process preset in the 3mf"

Both states. The OrcaSlicer CLI's **process↔machine compatibility check** — not a restriction on what a
machine preset is called. The literal is recorded in A at `app/threemf.py:6` and in B at
`app/profiles.py:31-43`; the raiser is the CLI binary, so it is not a constant in this repo.

| Cause | How to confirm | Fix |
|---|---|---|
| The process preset's `compatible_printers` does not name the selected machine | open the process JSON in `$ORCA_DATADIR/system/Custom/process/` | edit `compatible_printers`; in B a re-import rewrites it |
| The machine preset carries `"from": "user"` | `grep '"from"' <machine>.json` → `"user"` | must be `"system"`; B forces it on import |
| A `.3mf` carries its own foreign identity | `job.json` → `threemf.rewrite` is `none`, `threemf.notes` names the preset | see below |

For the 3MF case: `prepare_3mf` (`main.py:914`) applies the file's printer/process only when the
matched machine permits the matched process, and writes `upload/slice-<stem>.3mf` with the identity
rewritten. If the file's printer/process matched no local preset, the upload is sliced as it arrived
and the CLI refuses it; `threemf.notes` names what was missing.

Both states also refuse to *store* such a pair up front: `400` from `/api/profile/select` and `409`
from the printer routes with `process '<rel>' is not permitted by machine '<rel>'`
(`_reject_unpermitted_pair`, `main.py:260`).

**If you are on A and reading A's docs:** A's own comment (`profiles.py:31-35`) and `README.md:274-276`
claim a bundle machine under its own name returns `-17` *even when the process lists that name*. That
claim is what the B measurement contradicts — with `compatible_printers` naming the machine, the pair
slices. Do not treat A's merge rule as the explanation of `-17`.

## `251` — "The input preset file is invalid and can not be parsed"

A machine preset whose `from` is `"System"`, `"SYSTEM"`, or missing. Only lowercase `"system"` slices
(recorded as measured with 2.4.2, B `profiles.py:31-43`). B forces the value on import; this appears
when a preset was edited by hand in the datadir, or imported by state A. Re-import (B) or fix the field.

## Import refused — a name collision (state B only)

```text
<datadir>/system/Custom/<type>/<name>.json already holds a different preset named '<name>'; delete that file and re-import if you want to replace it
```

`400` from `POST /api/profile/upload`. `_plan_import` raises before anything is written, so nothing was
added. Delete the named file and re-import; re-importing the *same* bundle is idempotent
(`_same_preset`, B `profiles.py:228`).

## Import produced nothing (both states)

```json
{"imported": [], "notes": ["no presets found in that file"], "ok": false}
```

`200`, not an error: no zip member parsed as a preset. `_read_bundle` skips non-`.json` members,
unparsable JSON, the bundle's own `bundle_structure.json`/`bundle_id` documents, and objects with
neither `name` nor `layer_height` (A `profiles.py:297`, B `profiles.py:388`). Confirm the export came
from **File → Export → Export Preset Bundle** with at least one preset ticked.

```text
not a readable bundle: <reason>
```

`400` — the upload was not a readable zip/json at all (a truncated upload is the usual case).

## A machine-only bundle imports but the slice fails — state B only

```text
profile: ProfileError: process preset is not readable
```

B's seed ships no process preset, so a bundle that brings only a machine leaves `profile.json` without a
process entry; `active_paths()` then falls back to `DEFAULT_PROFILE["process"]`
(`system/Custom/process/generic-0.20-standard.json`, B `profiles.py:44`), which does not exist, and
`write_derived_process` raises (`run_slice` records it, B `main.py:644-648`).

In state A this does not happen: the generic process ships in the seed.

Fix (B): import a bundle that includes a process preset — and a filament, for the same reason. The
build-time smoke test asserts this: it exits `smoke: import failed` when any of the three types is
missing (`scripts/smoke_test.py:92`).

## The slicer ran but produced no gcode (both states)

The job ends `failed` with `error_string` from `out/result.json`, or — when there is none — the last
600 characters of the slicer's stdout/stderr (`main.py:674-677`). There is no separate "slicer log"
endpoint: `GET /api/jobs/{jid}` carries the text in `error`.

A slice exceeding `SLICE_TIMEOUT` fails with `slice timed out after <n>s` (`main.py:664`).

## Container exits at startup — `/data` not writable (both states)

```text
print-portal: /data is not writable by uid 1000.
print-portal: if ./data is bind-mounted, fix ownership on the host: sudo chown -R 1000:1000 ./data
```

`docker/entrypoint.sh:9-13`, exit 1. The image runs as uid 1000; the bind mount is root-owned. Fix
ownership on the host.

## `/api/health` answers 503 (both states)

Two possible bodies:

* `no printer configured: add one in Settings or set PRINTER_URL and MOONRAKER_API_KEY` — no printer
  resolves (`active_target()` returned `None`). The API is fine; add a printer, or set both
  `PRINTER_URL` and `MOONRAKER_API_KEY`.
* `jobs <path> is not writable` / `datadir <path> is not creatable: …` — a mount or permission problem
  on one of `jobs`, `home`, `orca-datadir`, `sliced`.

`/api/health` never contacts the printer, so a reachable API with a dead printer still answers 200.

## Printer errors (both states)

| Body / status | Cause | Fix |
|---|---|---|
| `printer unreachable at <url>: <type>: <reason>` (503) | the container has no route to Moonraker | check `PRINTER_URL`, the container network, and that Moonraker listens (`printer-setup.md`) |
| `Moonraker rejected the API key (HTTP 401)…` (502) | wrong key for that printer | re-enter it in Settings, or fix `MOONRAKER_API_KEY` |
| `Moonraker denied access (HTTP 403)… Add this host to Moonraker's trusted_clients…` (502) | client not trusted and no valid key | see `printer-setup.md` |
| `klipper not ready: <state>` (409) | Klipper `webhooks` is not `ready` | wait for Klipper, or read its log |
| `printer busy: state=<state>` (409) | printer is printing/paused | wait, or cancel on the printer |
| `job is '<status>', not sliced` (409) | print requested before slicing finished | poll `GET /api/jobs/{jid}` until `sliced` |
| `not sliced yet` (409 on `/gcode` or `/print`) | gcode missing from both `SLICED/<jid>/` and `jobs/<jid>/out/` | re-upload; check `SLICED_DIR` is mounted and writable |
| `no printer configured: …` (503) | nothing selected | add/select a printer |

## Printer-store errors (both states)

| Body | Status | Cause |
|---|---|---|
| `profiles must be an object with machine/process paths` | 400 | pairing body is not an object |
| `profile path must be relative to the datadir` | 409 | pairing names a path outside the datadir |
| `'<kind>: '<rel>' is not a preset inside the datadir'` | 400 | pairing/selection names a missing file, or a file of the wrong `type` |
| `the environment printer is read-only: change PRINTER_URL and MOONRAKER_API_KEY in the deployment instead` | 409 | tried to edit/delete the `env` row's name/url/key |
| `stored API key cannot be decrypted with this key file; re-enter the key in Settings` | 409 | `secret.key` was replaced or lost |
| `printer '<id>' not found` | 404 | unknown id, or the `env` id while no env seed is configured |

## Upload rejections (both states)

| Body | Status |
|---|---|
| `unsupported file type '<suffix>' (allowed: .3mf, .obj, .stl, .zip)` | 400 |
| `output_name must be a plain file name without path separators` | 400 |
| `output_name may only contain letters, digits, spaces, dots, dashes and underscores: remove '<char>'` | 400 |
| `output_name must be at most 60 characters` | 400 |
| `file larger than <n> MB` | 413 |

Archive failures carry `ArchiveError.status`: a hostile or truncated zip is `400`, too many members is
`413` (`app/archives.py`).

## "It slices with the wrong printer"

* **A:** a stored `machine` path in `profile.json` is honoured without an existence check
  (`active_paths`, A `main.py:315-320`); an override via `MACHINE_SLOT` wins over everything.
* **B:** a stored machine path that no longer exists is ignored and the resolved machine is used
  (`active_paths`, B `main.py:310-333`); a stored *process*/*filament* path is still honoured even when
  missing, which surfaces as `derived.stale` on `GET /api/profile` rather than a silent substitution.

Either way, check `MACHINE_SLOT` first: if it is set, it outranks the stored selection.
