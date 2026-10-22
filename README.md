# print-portal

print-portal is a small self-hosted web service that turns an STL upload into a finished print on a
Klipper printer running [Moonraker](https://moonraker.readthedocs.io/). You upload a model in the
browser; the portal slices it with the OrcaSlicer command-line binary and then hands the resulting
`.gcode` to Moonraker over HTTP, which uploads it to the printer's file root and starts the print.

It is one Docker container: a FastAPI app plus the OrcaSlicer AppImage. No database, no job-queue
backend, no shared filesystem with the printer host.

## What it does

```
browser ──upload──► portal ──OrcaSlicer CLI──► plate_1.gcode ──HTTP──► Moonraker ──start──► printer
```

1. **Upload** — the browser posts an `.stl`, `.3mf`, `.obj` or `.zip` file to `/api/upload`. A
   `.zip` is unpacked and one model member is sliced; a `.3mf` is read for its printer and
   process identity.
2. **Switch** — the machine and process presets (and the selected printer, when the portal
   recognises it) are switched to the ones the `.3mf` names, and a job-local copy of the file
   is re-identified to the local presets. STL and OBJ uploads skip this step.
3. **Slice** — a single background worker runs the OrcaSlicer CLI with your active machine,
   process and filament presets and writes `plate_1.gcode` plus a `result.json`.
4. **Send** — `POST /api/jobs/{id}/print` uploads the gcode to Moonraker's `gcodes` root and
   calls `printer/print/start`.
5. **Monitor** — `/api/printer` reports printer state, the current file, progress, nozzle and bed
   temperatures and homed axes.

Slicing and printer control are two separate steps. The portal never holds a connection to the
printer host; it only speaks Moonraker's HTTP API.

## Why slicing runs off the printer host

A Klipper printer host runs `klippy`, which has to feed the MCU on a tight schedule. Slicing is a
single-core, multi-gigabyte burst of work; running it on the same machine competes with `klippy`
for CPU and RAM and can introduce motion artifacts or stutters on long prints.

The portal slices on whatever machine runs the container — a NAS, a home server, a small VM — and
sends the printer nothing but a finished `.gcode` over HTTP. The printer host needs only Moonraker
and network reachability; it does not need OrcaSlicer, Python or any shared mount.

## Prerequisites

- **Moonraker** reachable from the container, with an API key (see
  [docs/printer-setup.md](docs/printer-setup.md)).
- **An OrcaSlicer preset bundle** — an `.orca_printer` export carrying your machine preset and the
  process and filament presets you want. The image ships **no presets**, so import one bundle once
  through `POST /api/profile/upload`. Where presets live on each platform, and how to export a
  `.orca_printer` from the desktop app: [docs/orcaslicer-presets.md](docs/orcaslicer-presets.md).
- **Docker** with the Compose plugin, Docker Compose **≥ 2.24** — `docker-compose.yml` uses the
  optional `env_file: {path: .env, required: false}` form, which older Compose releases reject.

## Quickstart

```bash
git clone <this repository> print-portal
cd print-portal
cp .env.example .env
# edit .env: set PRINTER_URL and MOONRAKER_API_KEY
mkdir -p data
docker compose up -d
```

Then open <http://localhost:8088>, upload an STL, and press **Print**.

`.env.example` and `docker-compose.yml` in this repository are the real configuration files; the
quickstart above is all you need to start.

## First run

A fresh install ships **no printer, process or filament preset**. The portal cannot slice anything
until you import one bundle — that is deliberate: a preset that does not match your printer fails at
the printer, after the upload looked successful.

In OrcaSlicer, with your printer selected: **File → Export → Export Preset Bundle…**, tick
**Printer** (and the process and filament presets you want), then select the printer on the next
screen. Save the `.orca_printer` file.

In the portal, open the profile card and upload that file (or `POST /api/profile/upload`). The
import writes your machine under **its own name**, registers it, and selects it together with the
first process and filament in the bundle — the three pickers then list exactly what you imported.
Importing a second bundle adds a second machine; both stay selectable.

The bundle carries **presets only**. The printer *connection* — Moonraker URL and API key — is
separate: `PRINTER_URL` / `MOONRAKER_API_KEY`, or the Printers card. Neither depends on the other.

Until a bundle is imported, `GET /api/profiles` answers with three empty lists and `GET /api/profile`
answers `200` with `"imported": false` — reads never fail. Anything that would slice answers `409`
with `detail.error: "no_printer_profile"`.

## Configuration

Most configuration is environment variables (read from `.env` via Compose; see `docker-compose.yml` and
`.env.example`). Printers can also be managed in the UI (Settings), in which case `PRINTER_URL` and
`MOONRAKER_API_KEY` are left unset.

| Var | Default | Meaning |
|---|---|---|
| `PRINTER_URL` | `http://127.0.0.1:7125` | Moonraker base URL, no trailing slash |
| `MOONRAKER_API_KEY` | *(optional)* | Moonraker API key for the environment printer; required together with `PRINTER_URL`. The CLI `python -m config` fails closed without it, but the app boots so printers can be managed in Settings |
| `MOONRAKER_ROOT` | `gcodes` | Moonraker file root for uploads |
| `PORT` | `8088` | port the web UI listens on |
| `DATA_DIR` | `/data` | jobs, uploaded presets, profile selection, Qt `HOME` |
| `ORCA_BIN` | `/opt/orca/bin/orca-slicer` | slicer binary inside the image |
| `ORCA_DATADIR` | `$DATA_DIR/orca-datadir` | writable slicer datadir, seeded from the image |
| `MAX_UPLOAD_MB` | `150` | upload size cap, also bounding an unpacked archive member |
| `KEEP_JOBS` | `25` | job directories retained before the oldest are pruned |
| `SLICE_TIMEOUT` | `900` | seconds before a slice is killed |
| `BED_SIZE` | `220x220` | bed size used to clamp imported machine presets |
| `MACHINE_SLOT` | *(auto)* | optional override of the machine preset the slicer loads |
| `SEED_VERSION` | `2.4.2-2` | build-time marker for the seed copy; bump it to re-run seeding on upgrade |

`HOME` for the slicer subprocess is `$DATA_DIR/home` and must be writable.

An existing deployment keeps the presets it already has: the seed is copied into the datadir with
`cp -R -n`, which never overwrites, so an empty seed can never resurrect a preset either. Delete
`$DATA_DIR/orca-datadir/system/Custom/` once if you want the fresh-install state — the portal never
deletes your data in a migration.

## Build the image yourself

The image is **amd64 only** (the OrcaSlicer AppImage is x86_64). The Dockerfile downloads a pinned
build and verifies it:

- URL: `https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.4.2/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage`
- sha256: `d12fb8c8eac1aecd2dfb6377acd48f994f8fa439ed5292fa532dd82880f029fd`

```bash
docker build --platform linux/amd64 -t print-portal:dev .
```

The download is a dedicated early layer gated by `sha256sum -c`, so a rebuild reuses the cached
layer instead of re-downloading the ~138 MB AppImage. `ORCA_URL` and `ORCA_SHA256` are build args,
so a mirror can be substituted without editing the Dockerfile — the hash still has to match, so a
compromised mirror fails the build.

**Build-time smoke test.** During `docker build`, the image slices `tests/fixtures/cube.stl` with
its own OrcaSlicer binary (as the non-root uid 1000, with `QT_QPA_PLATFORM=offscreen`) and asserts
that a `.gcode` is produced with positive filament mass and layer count. The build fails loudly if
a shared library is missing or a preset is broken — never the first user print.

## Get a Moonraker API key

See [docs/printer-setup.md](docs/printer-setup.md). In short: Moonraker's `[authorization]`
section generates a key (or use the API key panel in your web UI). Put it in `MOONRAKER_API_KEY`
for an environment printer, or paste it in Settings → Add printer for a UI-managed printer. The key
is a secret — treat it like a password.

## API

| Method | Path | What |
|---|---|---|
| GET | `/` | upload UI |
| GET | `/api/health` | `{"status":"ok","app":"print-portal","version":...}` — readiness; returns 503 until a printer is configured |
| GET | `/api/printer` | live snapshot: state, file, progress, nozzle/bed temps, homed axes |
| POST | `/api/upload` | multipart `file` (`.stl`, `.3mf`, `.obj`, `.zip`, ≤`MAX_UPLOAD_MB`) → job; the response adds `archive` and `threemf` when the upload was unpacked or switched a profile |
| GET | `/api/jobs` | list jobs, newest first |
| GET | `/api/jobs/{id}` | status, stats, error, plus the fit/orient fields described under [Auto-orient and shrink to fit](#auto-orient-and-shrink-to-fit) |
| GET | `/api/jobs/{id}/gcode` | download the sliced gcode |
| POST | `/api/jobs/{id}/print` | upload to Moonraker and start the print |
| DELETE | `/api/jobs/{id}` | remove job files |
| GET | `/api/profile` | active machine/process/filament + their key settings, plus `effective` (supports/brim and brim width the slicer will use) and `derived` (`path`, `stale`, `error`) |
| GET | `/api/profiles` | every preset available to the slicer; `?machine=<rel>` narrows the process list to that machine |
| POST | `/api/profile/upload` | import a `.orca_printer` bundle or preset json |
| POST | `/api/profile/select` | switch the active machine/process/filament; applies at once and is written back onto the selected printer |
| POST | `/api/profile/options` | set supports/brim (regenerates the derived process preset) and `hide_profiles` |
| GET | `/api/printers` | list configured printers, including their `profiles` pairing |
| POST | `/api/printers` | add a printer, optionally with a machine/process pairing |
| PATCH | `/api/printers/{id}` | update a printer, including its pairing; the `env` row accepts only `profiles`, answering `409` when `name`/`url`/`api_key` are supplied, `404` when no environment printer is configured, and `400` (`nothing to update`) when the body carries neither `profiles` nor one of those fields |
| DELETE | `/api/printers/{id}` | remove a printer |
| POST | `/api/printers/{id}/select` | select active printer and apply its pairing to the active profile |
| POST | `/api/printers/{id}/test` | test printer connection |
| DELETE | `/api/jobs` | remove all jobs |

A read never `409`s on a fresh install: `GET /api/profile` answers `200` with `"imported": false` and
null selections when nothing is imported. `409` with `detail.error: "no_printer_profile"` comes only
from actions that need a preset: slice/upload, profile select, and printer pairing.

## 3MF and .zip uploads

`.3mf` and `.zip` are not opaque blobs: the portal reads what they contain and prepares the
slice so OrcaSlicer accepts it.

### 3MF identity

- The printer/process identity is read from `Metadata/project_settings.config` (OrcaSlicer and
  Bambu JSON), falling back to `Metadata/Slic3r_PE.config` (PrusaSlicer-style `key = value`)
  and then to `Metadata/slice_info.config` for `printer_model_id`. `Metadata/model_settings.config`
  describes objects and per-object overrides and is not used for identity. Each member read is
  capped at 8 MB (`MEMBER_MAX_BYTES` in `app/threemf.py`): a settings member bigger than that, or
  one whose header understates its own size, counts as absent, so the 3MF is sliced with the
  current selection instead of the upload failing.
- The names are matched against the presets in `ORCA_DATADIR`. A printer the portal cannot
  match is left alone — the current selection stays — and the mismatch is recorded as a note
  on the job. `profile.json` only ever receives a pairing the selected machine permits: the
  3MF's own process when that machine lists it, otherwise the process already selected when it
  still fits, otherwise nothing — a half match leaves the stored pairing alone and the reason
  goes into the job's notes.
- Switching at the app level is not enough: OrcaSlicer applies the 3MF's own project settings,
  so a foreign `printer_settings_id` still yields `-17` — the 3MF's process does not list the
  machine the presets provide. The portal therefore writes
  `upload/slice-<stem>.3mf` in the job directory with `printer_settings_id`, `printer_model`,
  `printer_variant`, `print_settings_id` and `compatible_printers` set to the local presets,
  with every other member's contents preserved — the archive is re-zipped, so members are
  re-compressed and the archive comment and per-member extra fields are dropped. The original
  upload is untouched;
  `job["threemf"]["rewrite"]` records `identity`, `stripped` or `none`.
- Fallback: setting `STRIP = True` in `app/threemf.py` drops the settings member instead of
  rewriting it, and OrcaSlicer then treats the file as a plain model so `--load-settings` wins.

### ZIP archives

- `.zip` is unpacked under fail-closed rules: 64 entries at most (413 beyond that), and *every*
  member name is checked, so an absolute path, a `..` component or a symlink entry rejects the
  whole archive (400) rather than being skipped.
- Nested archives are not recursed into; directories, `__MACOSX/` and macOS `._*` resource
  forks are skipped.
- One model is picked deterministically: the shallowest path first, then the case-insensitive
  name. The response says which one: `Slicing part.stl (1 of 3 model files)`.
- The extracted member is streamed to `upload/` under its own basename with a byte cap equal
  to `MAX_UPLOAD_MB` (413 when exceeded). The archive is kept for provenance, while the job's
  `filename` and the default output name come from the extracted model.
- A `.3mf` inside the archive runs the same switch-and-rewrite pipeline described above.

## Importing profiles

`POST /api/profile/upload` takes a `.orca_printer` bundle (a ZIP containing `printer/`, `process/`,
`filament/` and `bundle_structure.json`) or a single preset `.json`, normalises it and writes it
into the datadir **under the bundle's own names**: the machine becomes its own preset file, gets an
entry in `system/Custom.json`, and the bundle's processes are pointed at it. No pre-existing file is
rewritten except that index. `POST /api/profile/select` chooses which machine/process/filament the
slicer loads; the choice is stored in `profile.json`. Selecting a preset in the dashboard applies it
immediately — there is no Enable step — and the chosen machine/process pair is written back onto
the selected printer (see [Printer pairing](#printer-pairing)).

### Basic options

`options` lives in `profile.json` with `supports`, `brim`, `hide_profiles`; defaults `false`,
`true`, `false`; legacy files without `options` get defaults. Supports is yes/no only; Brim yes/no
drives `enable_support` and `brim_type`/`brim_width`. The Supports and Brim controls sit on the
dashboard **profile card**, which also shows the resulting `Supports` / `Brim` state (with the brim
width when brim is on) from `effective` on `GET /api/profile`. The Settings view keeps only the
**Interface** card for options, i.e. the `hide_profiles` toggle that hides the dashboard
machine/process/filament pickers while slicing keeps using the saved selection. It hides only
those pickers: the Supports and Brim selects stay visible and editable, because they set how the
model is sliced instead of choosing a preset.

Saving supports or brim rewrites the derived preset immediately, and every slice rewrites it
again. That derived file is `system/Custom/process/print-portal-options.json` inside
`ORCA_DATADIR`; bundled presets are never modified and the derived file is hidden from the
picker. If it cannot be written, the save still succeeds and `GET /api/profile` reports
`derived.stale`, which the profile card shows as a warning. `derived.error` carries the reason
when the derived file is missing or unreadable; when the file is there but its support/brim
fields differ from the saved options, `stale` is still true and `error` is `null`.

- `system/Custom/process/print-portal-options.json` inside `ORCA_DATADIR` is reserved and regenerated on every option save and every slice; don't edit it by hand.
- Concurrent option saves are last-write-wins.

A bundle carries none of the local wiring the CLI needs, so import has to fix it:

| Problem in the bundle | What import does |
|---|---|
| no `type` field | derived from the folder (`printer/`→machine, `process/`, `filament/`) |
| `from` missing, `"user"` or `"System"` on a machine preset | set to `"system"` — the load-bearing literal: the CLI answers `-17` for `"user"` and `251` for `"System"` or an absent `from` |
| `inherits` a preset that is not here | left as-is: the CLI tolerates a dangling `inherits` |
| `compatible_printers` names a machine preset we do not have | rewritten to the imported machine's own name |
| `default_print_profile` / `default_filament_profile` point at vendor presets | re-pointed at the presets just imported |
| `printable_area` bigger than the bed | clamped to `BED_SIZE` |

### Auto-orient and shrink to fit

Two more switches live in `options`, and unlike supports and brim they never reach the derived
process preset — they become command-line flags on the slice. `auto_orient` defaults to `true`,
`shrink_to_fit` to `false`; `POST /api/profile/options` stores both, there is no separate
endpoint.

- **`auto_orient`** adds `--orient 1` (the literal `1`: the CLI rejects a bare `--orient` and
  `--orient auto` with rc 254), so OrcaSlicer's own orientation search picks the flattest stable
  pose. It is a no-op for a model that already lies flat, whose gcode is identical to a slice
  without it apart from the slicer's `; generated by OrcaSlicer … at <timestamp>` banner.
- **`shrink_to_fit`** measures the uploaded mesh against the selected machine preset's
  `printable_area` and `printable_height` and computes one uniform factor
  `k = min(1, (X span − 16)/size_x, (Y span − 16)/size_y, Z max/size_z)`, capped at 1 (never
  enlarged) and rounded down to three decimals (never rounded up). It passes `--scale k` only
  when `k < 1`; every real shrink also forces `--orient 1`, because the CLI produces no plate
  with `--scale` alone (`-50 "One of the plate is empty"`). Units are millimetres and the scale
  is uniform — no per-axis distortion.
- The 16 mm is the clearance kept on each of X and Y for brim and skirt, empirically the largest
  that still slices on a 220 mm bed: `k = 0.68` slices cleanly (gcode spans 1.0–219.1 mm) while
  `k = 0.69` reaches 220.6 mm and fails. Nothing is reserved above the part, so Z uses
  `printable_height` as it stands.
- A factor below `0.25` is refused rather than silently producing a uselessly tiny part. The
  message names the model's dimensions, the factor it would have needed, the bed limits, the
  floor and the minimum size to re-export at.
- With shrink off, a model larger than the bed is refused before the CLI runs, with a message
  naming the model dimensions and the bed; the CLI's own `-50 "One of the plate is empty or has
  no objects"` names no dimensions.
- Geometry is read for STL (ASCII and binary) and OBJ. 3MF is a known limitation: its geometry
  lives in `3D/3dmodel.model`, which the portal does not parse (`app/threemf.py` only rewrites
  preset identity). With shrink off a 3MF slices exactly as before and auto-orient still applies.
  A machine preset without `printable_area`/`printable_height` only blocks shrink, never a
  normal slice.

Job records — `GET /api/jobs/{id}` and the SSE stream — carry what was decided: `auto_orient`,
`auto_orient_used` (true when orient actually ran, e.g. because a shrink forced it),
`shrink_to_fit`, `fit_scale` (`1.0` when nothing was scaled, `null` when the model could not be
measured), and `model_size` / `bed_limits` as `{x,y,z}` spans in millimetres.

### Process presets follow the selected machine

The dashboard asks `GET /api/profiles?machine=<rel>`, so the process picker only lists presets
whose `compatible_printers` names the selected machine; an empty or absent `compatible_printers`
means universal and is always listed. An unknown `machine` value is ignored rather than
rejected, and so is one that names a preset of another type (a process or filament file) — only
an existing machine preset narrows the list. The active process is always appended back so the
picker never goes blank. Changing the machine re-filters that list and resolves the pairing
before it is saved: when the new machine does not list the active process, the first process it
does list is selected in its place and the card shows a note naming the swap. When the new
machine lists no process at all, the current selection stays on screen but is not written to
`profile.json`, and the note says nothing was saved.

## Safety gates

`POST /api/jobs/{id}/print` refuses unless **all** of these hold:

- the printer answers over Moonraker HTTP,
- `webhooks.state == ready`,
- `print_stats.state` is `standby` or `complete`.

Otherwise it returns `409`/`503` with the reason and starts nothing.

## OrcaSlicer CLI quirks

These are the behaviours the code works around; keep them in mind when debugging:

- **`--export-3mf` does not slice.** It writes a project 3MF with `layer_height: 0.0`. Slicing needs
  `--slice 0 --outputdir DIR`, which writes `plate_1.gcode` and `result.json`.
- **The loader flags matter.** The slicer is invoked with `--load-settings "<machine>;<process>"`
  and `--load-filaments <filament>`, plus `--datadir` pointing at the writable datadir.
- **The gcode summary is not always near the top.** A profile that prints every setting can push
  `estimated printing time` / `total filament used [g]` to ~230 kB into the file. The parser reads
  2 MB and falls back to the Prusa-style `;TIME:` / `;Filament used:` lines.
- **Preset filenames must not contain spaces** (CLI quirk). The importer replaces them.
- **The `-17` compatibility rule.** `-17` (`"The selected printer is not compatible with the
  process preset in the 3mf"`) is the *process ↔ machine* check: the process preset does not list the
  selected machine in `compatible_printers`. It is not a restriction on the machine's identity.
  Measured with OrcaSlicer 2.4.2: a separately named machine with its own index entry sliced fine,
  and patching the process preset's `compatible_printers` to that name changed the result to `rc=0`
  `"Success."`. Import therefore rewrites `compatible_printers` to the imported machine's own name,
  which is why an imported bundle slices. The same rule bites an uploaded `.3mf`: its embedded
  project identity overrides `--load-settings`, so the upload path rewrites that identity in a
  job-local copy instead (see [3MF identity](#3mf-identity)).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `printer unreachable` (503) | The container cannot reach `PRINTER_URL`. Check the URL, that the container has a route to the printer host, and that Moonraker is listening. See [docs/deployment.md](docs/deployment.md). |
| `Moonraker rejected the API key` (502) | `MOONRAKER_API_KEY` is wrong or the key is not allowed for this client. Regenerate the key in Moonraker and update `.env` or Settings. |
| `-17 incompatible profile` | A `.3mf`'s embedded project settings still name a printer or process the local presets do not accept, or a process preset does not list the selected machine in `compatible_printers`. Uploads are re-identified automatically into `upload/slice-<stem>.3mf`; the job's `threemf.rewrite` field says what happened (`identity`, `stripped` or `none`). If it still fails, set `STRIP = True` in `app/threemf.py` to drop the settings member instead of rewriting it. For bundles, re-import through `/api/profile/upload` — an imported process preset always lists the machine it came with. |
| my model is bigger than my bed | Turn on **shrink to fit** for the job, or scale the model yourself and upload it again. The portal's refusal message names the model's dimensions and the bed, so it tells you how much to take off. |
| shrink to fit refuses my model | The uniform factor needed is under the `0.25` floor, so the part would no longer print as designed. The message names the factor it would have needed and the smallest size that works — scale the model to at least that and upload it again. |
| shrink to fit fails on a 3MF | 3MF geometry lives in `3D/3dmodel.model`, which the portal does not parse — `app/threemf.py` only rewrites preset identity there. Turn shrink to fit off for this job, or export the model as STL and upload that. A bounding-box reader for 3MF is separate follow-up work, not part of this change. |
| `missing shared library` | The image was built for the wrong architecture, or the OrcaSlicer binary cannot find its bundled libraries. The image is amd64 only; rebuild with `--platform linux/amd64`. |
| `mkdir: cannot create directory '/data/...': Permission denied` and the container exits | The bind-mounted `./data` directory is owned by root — Compose creates it that way when it does not exist yet on the host, while the image runs as uid 1000. Fix ownership on the host: `sudo chown -R 1000:1000 ./data`. |

## Limitations

- **amd64 only** — the OrcaSlicer AppImage is x86_64.
- **No built-in authentication.** Anyone who can reach the port can upload and start a print. Put
  the container behind a reverse proxy with authentication or a VPN. See
  [SECURITY.md](SECURITY.md).
- **One slice at a time** (single worker) on purpose: OrcaSlicer peaks at ~1 GB RAM.
- **The print gate is not re-checked during a print.** State is read at click time only.

## Printers

Printers are managed in the UI (Settings view): name, URL, API key. They are stored encrypted in
`data/printers.json` using `data/secret.key` (mode 0600). Back up both files together; losing the
key file means re-entering API keys. A single uvicorn worker is assumed (no portal authentication).

You can also configure one printer via environment variables (`PRINTER_URL` +
`MOONRAKER_API_KEY`); this "environment printer" is seeded into the store at startup. If neither
is set, the portal still boots and printer routes return 503 until a printer is added in Settings.

`/api/health` returns 503 (and the Docker healthcheck goes unhealthy) until at least one printer is
configured.

### Printer pairing

`printers.json` is version 2. A printer may carry an optional
`profiles: {"machine": <rel>, "process": <rel>}` pairing of datadir-relative preset paths, set
with `POST`/`PATCH /api/printers` or the machine/process selects in the Settings printer form. An
absent pairing, or a `null` slot inside one, means that printer inherits the global selection in
`profile.json`. The environment printer has no printer record to hold one, so its pairing lives in
an optional top-level `env_profiles` object of the same `{"machine", "process"}` shape (same
rules: datadir-relative `.json` paths or `null` per slot). It is written only for the environment
printer, and a malformed value there is ignored on read — that printer falls back to the global
selection instead of the whole file counting as corrupt.

Selecting a printer (`POST /api/printers/{id}/select`) applies its pairing to `profile.json`,
including the displayed `machine_label`. Choosing machine and process presets on the dashboard records
them on the selected printer, so the two stay in step (best effort: a read-only `printers.json`
loses the stored pairing, never the selection). The environment printer
can be paired too: the `Profiles` button on its row opens the printer form with the credential
fields disabled, and only its name, URL and API key stay owned by the environment. The pairing is
validated in two places: the store checks its shape, and the API checks that each path is a preset
inside `ORCA_DATADIR`, answering `400` with `machine: '<rel>' is not a preset inside the datadir`
otherwise.

A version 1 file still loads: reads never rewrite it, and the next change writes the document as
version 2.

## Environment variables (UI-managed printers)

| Var | Default | Notes |
|---|---|---|
| `SLICED_DIR` | `$DATA_DIR/sliced` | mountable output directory |
| `PRINTERS_FILE` | `$DATA_DIR/printers.json` | printer store |
| `SECRET_KEY_FILE` | `$DATA_DIR/secret.key` | Fernet key |
| `ENV_PRINTER_NAME` | `Default (environment)` | name used for the environment printer |

`MOONRAKER_API_KEY` is optional when printers are managed in the UI; `PRINTER_URL` and
`MOONRAKER_API_KEY` must be set together. The strict CLI `python -m config` still fails closed
without a key. An unconfigured portal stays reachable and printer routes return 503.

Sliced output from older jobs in `jobs/*/out` remains readable via fallback.

## License



MIT — see [LICENSE](LICENSE).
