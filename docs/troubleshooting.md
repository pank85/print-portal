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

### Slicer CLI quirks the code works around

Keep these in mind before changing the slice invocation — each one has bitten the portal:

* **`--export-3mf` does not slice.** It writes a project 3MF with `layer_height: 0.0`. Slicing needs
  `--slice 0 --outputdir DIR`, which writes `plate_1.gcode` and `result.json`.
* **The loader flags matter.** The CLI is called with `--load-settings "<machine>;<process>"`,
  `--load-filaments <filament>` and `--datadir` pointing at the writable datadir.
* **A bare `--orient` and `--orient auto` are rejected (rc 254)** — the flag needs the literal `1`.
* **`--scale` alone produces no plate** (`-50 "One of the plate is empty"`), so a shrink also forces
  `--orient 1`.
* **The gcode summary is not always near the top.** A profile that prints every setting can push
  `estimated printing time` / `total filament used [g]` ~230 kB into the file; the parser reads 2 MB
  and then falls back to the Prusa-style `;TIME:` / `;Filament used:` lines.
* **Preset filenames must not contain spaces** — the importer replaces them.

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

## Support fins (opt-in, fins branch only)

Four failure modes on the finned path. The first three happen inside `POST /api/upload` with
`support_fins=true`, which is also the request that runs the CLI.

| Message | Status | Cause and fix |
|---|---|---|
| `support fins failed: <cli text>` | 400 | The bundled CLI exited non-zero and `<cli text>` is its own last line. Exit `1` = *this file cannot be finned* (e.g. `support-fins: /etc/hostname: reads .stl, .3mf and .obj only`), exit `2` = *bad arguments*, both mapped to 400 (`app/supportfins.py`). The job directory is removed before the response, so nothing is left on disk — fix the model and upload again. |
| `support fins failed: support fins timed out after <n>s` | 400 | The run exceeded `SUPPORT_FINS_TIMEOUT` (default `300`). Fin runs are serialised on one lock, so the wait can also be the upload queued behind a slower one. Raise the variable for very large meshes, or slice without fins. |
| `support fins failed: node runtime is not available` | 400 | The image has no `node` on `PATH` (or it vanished mid-run). `GET /api/support-fins` reports it up front — `{available:false, reason:"node runtime is not available"}`, the other reason being `support-fins CLI is missing from the image` — and the UI does not offer the checkbox. Rebuild the image: `node --version` and the `--fins-only` smoke step run at build time (`Dockerfile`, `scripts/smoke_test.py`). |
| `preview unavailable: <reason>` | 409 | `GET /api/jobs/{jid}/preview` could not read the finned 3MF. The caps are 200 000 triangles and 12 MiB of input (`app/preview.py:42,45`); a job without `support_fins`/`fins` answers `preview unavailable: this job has no support fins`. Slicing is unaffected — the dialog still offers **Slice it**. |

**Zero fins is not a failure.** A shallow overhang legitimately yields `finTriangles: 0` with exit `0`
(`tests/fixtures/cube.stl` does), and the job is staged as usual with `report` explaining why
(`1 overhang is too shallow for a fin this way up (tilt the part steeper)`). Use
`tests/fixtures/overhang.stl` — 7 overhang regions, 3916 fin triangles, 24 tines — when you need fins to
actually appear.

## "My model is bigger than my bed"

With `shrink_to_fit` off — the default — the model is measured before the CLI runs and refused with
the size it has and the size the bed allows:

```text
cube.stl is 300x300x300 mm and does not fit the 220x220x250 mm bed of 'printer-1' — turn on shrink to fit (uniform scale) or upload a smaller model
```

(`_too_big_message`, `app/fit.py:83`; raised from `plan_fit`, `app/fit.py:117`.)

* **Fix:** set **Shrink to fit** to *Yes* in the Printer card — the portal then computes one uniform
  factor (`--scale k`) and orients the model too (`--orient 1`); the job row reports the percentage
  it used. Or scale the model yourself to the size the message names and upload that. A factor below
  `0.25x` is refused as well, and `_below_floor_message` (`app/fit.py:91`) names the minimum size to
  re-export at.

## "Shrink to fit refuses my 3MF"

```text
model.3mf: the portal reads STL and OBJ geometry only (3MF geometry lives in 3D/3dmodel.model) — turn shrink to fit off for this job, or export the model as STL and upload that
```

* **Where:** the job fails with that error, appended in `main.py:695` to the `MeshError` raised by
  `model_bounding_box` (`app/mesh.py:162`, message at `app/mesh.py:32`).
* **Why:** only STL and OBJ are measurable (`MEASURABLE_SUFFIXES`, `app/mesh.py:25`). A 3MF's geometry
  lives in `3D/3dmodel.model`, which the portal does not parse — `app/threemf.py` rewrites the preset
  identity only. Known limitation, not a bad upload; the same file slices fine with shrink off.
* **Fix:** turn **Shrink to fit** off for that job (auto-orient still applies), or export the model as
  STL and upload that with shrink on.

## "It slices with the wrong printer"

* **A:** a stored `machine` path in `profile.json` is honoured without an existence check
  (`active_paths`, A `main.py:315-320`); an override via `MACHINE_SLOT` wins over everything.
* **B:** a stored machine path that no longer exists is ignored and the resolved machine is used
  (`active_paths`, B `main.py:310-333`); a stored *process*/*filament* path is still honoured even when
  missing, which surfaces as `derived.stale` on `GET /api/profile` rather than a silent substitution.

Either way, check `MACHINE_SLOT` first: if it is set, it outranks the stored selection.
