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

1. **Upload** — the browser posts an `.stl`, `.3mf` or `.obj` file to `/api/upload`.
2. **Slice** — a single background worker runs the OrcaSlicer CLI with your active machine,
   process and filament presets and writes `plate_1.gcode` plus a `result.json`.
3. **Send** — `POST /api/jobs/{id}/print` uploads the gcode to Moonraker's `gcodes` root and
   calls `printer/print/start`.
4. **Monitor** — `/api/printer` reports printer state, the current file, progress, nozzle and bed
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
| `MAX_UPLOAD_MB` | `150` | upload size cap |
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
| POST | `/api/upload` | multipart `file` (`.stl`, `.3mf`, `.obj`, ≤150 MB) → job id |
| GET | `/api/jobs` | list jobs, newest first |
| GET | `/api/jobs/{id}` | status, stats, error |
| GET | `/api/jobs/{id}/gcode` | download the sliced gcode |
| POST | `/api/jobs/{id}/print` | upload to Moonraker and start the print |
| DELETE | `/api/jobs/{id}` | remove job files |
| GET | `/api/profile` | active machine/process/filament + their key settings |
| GET | `/api/profiles` | every preset available to the slicer |
| POST | `/api/profile/upload` | import a `.orca_printer` bundle or preset json |
| POST | `/api/profile/select` | switch the active machine/process/filament |
| GET | `/api/printers` | list configured printers |
| POST | `/api/printers` | add a printer |
| PATCH | `/api/printers/{id}` | update a printer |
| DELETE | `/api/printers/{id}` | remove a printer |
| POST | `/api/printers/{id}/select` | select active printer |
| POST | `/api/printers/{id}/test` | test printer connection |
| DELETE | `/api/jobs` | remove all jobs |

## Importing profiles

`POST /api/profile/upload` takes a `.orca_printer` bundle (a ZIP containing `printer/`, `process/`,
`filament/` and `bundle_structure.json`) or a single preset `.json`, normalises it and writes it
into the datadir. `POST /api/profile/select` chooses which machine/process/filament the slicer
loads; the choice is stored in `profile.json`.

A bundle carries none of the local wiring the CLI needs, so import has to fix it:

| Problem in the bundle | What import does |
|---|---|
| no `type` field | derived from the folder (`printer/`→machine, `process/`, `filament/`) |
| `inherits` a vendor preset that does not exist here | re-rooted to `fdm_klipper_common` / `fdm_process_klipper_common` / `fdm_filament_pla` |
| `compatible_printers` names a machine preset we do not have | rewritten to the machine slot's name |
| `default_print_profile` / `default_filament_profile` point at vendor presets | re-pointed at the presets just imported |
| `printable_area` bigger than the bed | clamped to `BED_SIZE` |

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
  first instantiated machine), so any datadir with a machine preset works.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `printer unreachable` (503) | The container cannot reach `PRINTER_URL`. Check the URL, that the container has a route to the printer host, and that Moonraker is listening. See [docs/deployment.md](docs/deployment.md). |
| `Moonraker rejected the API key` (502) | `MOONRAKER_API_KEY` is wrong or the key is not allowed for this client. Regenerate the key in Moonraker and update `.env` or Settings. |
| `-17 incompatible profile` | The imported machine preset was not rebased onto the resolved slot, or the process preset is not compatible with it. Re-import the bundle through `/api/profile/upload`. |
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
