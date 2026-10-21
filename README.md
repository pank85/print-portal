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
- **An OrcaSlicer preset** — a machine preset, a process preset and a filament preset. The image
  ships a neutral generic set; you can also import your own `.orca_printer` bundle or single
  preset `.json` through `POST /api/profile/upload`.
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
| `MACHINE_SLOT` | *(auto)* | optional override of the machine preset that imports rebase onto |
| `SEED_VERSION` | `2.4.2-1` | build-time marker; bump it to re-run preset seeding on upgrade |

`HOME` for the slicer subprocess is `$DATA_DIR/home` and must be writable.

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
| GET | `/api/jobs/{id}` | status, stats, error |
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
| PATCH | `/api/printers/{id}` | update a printer, including its pairing |
| DELETE | `/api/printers/{id}` | remove a printer |
| POST | `/api/printers/{id}/select` | select active printer and apply its pairing to the active profile |
| POST | `/api/printers/{id}/test` | test printer connection |
| DELETE | `/api/jobs` | remove all jobs |


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
  so a foreign `printer_settings_id` still yields `-17`. The portal therefore writes
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
into the datadir. `POST /api/profile/select` chooses which machine/process/filament the slicer
loads; the choice is stored in `profile.json`. Selecting a preset in the dashboard applies it
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
| `inherits` a vendor preset that does not exist here | re-rooted to `fdm_klipper_common` / `fdm_process_klipper_common` / `fdm_filament_pla` |
| `compatible_printers` names a machine preset we do not have | rewritten to the machine slot's name |
| `default_print_profile` / `default_filament_profile` point at vendor presets | re-pointed at the presets just imported |
| `printable_area` bigger than the bed | clamped to `BED_SIZE` |

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
- **The `-17` rebase rule.** OrcaSlicer's CLI refuses a machine preset that is not the identity it
  expects: a bundle machine written under its own name returns
  `-17 "The selected printer is not compatible with the process preset in the 3mf"` even when the
  process lists that exact name in `compatible_printers`. `printer_model`, `inherits` and
  `compatible_printers` do not help. Import therefore merges the bundle machine onto the resolved
  machine slot (identity keys preserved, everything else overwritten) and keeps the original label
  in `profile.json` for display. The slot is resolved at runtime (`MACHINE_SLOT` → datadir default →
  first instantiated machine), so any datadir with a machine preset works. The same rule bites an
  uploaded `.3mf`: its embedded project identity overrides `--load-settings`, so the upload path
  rewrites that identity in a job-local copy instead (see [3MF identity](#3mf-identity)).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `printer unreachable` (503) | The container cannot reach `PRINTER_URL`. Check the URL, that the container has a route to the printer host, and that Moonraker is listening. See [docs/deployment.md](docs/deployment.md). |
| `Moonraker rejected the API key` (502) | `MOONRAKER_API_KEY` is wrong or the key is not allowed for this client. Regenerate the key in Moonraker and update `.env` or Settings. |
| `-17 incompatible profile` | A `.3mf`'s embedded project settings still name a printer or process the local presets do not accept, or an imported machine preset was not rebased onto the resolved slot. Uploads are re-identified automatically into `upload/slice-<stem>.3mf`; the job's `threemf.rewrite` field says what happened (`identity`, `stripped` or `none`). If it still fails, set `STRIP = True` in `app/threemf.py` to drop the settings member instead of rewriting it. For bundles, re-import through `/api/profile/upload`. |
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
`profile.json`.

Selecting a printer (`POST /api/printers/{id}/select`) applies its pairing to `profile.json`,
including the displayed `machine_label`. Choosing presets on the dashboard writes them back onto
the selected printer, so the pairing and the active profile cannot drift; the environment printer
is read-only and is never paired. The pairing is validated in two places: the store checks its
shape, and the API checks that each path is a preset inside `ORCA_DATADIR`, answering `400` with
`machine: '<rel>' is not a preset inside the datadir` otherwise.

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
