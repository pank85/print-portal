# SliceDeck

**From model to print, in your browser.**

SliceDeck is a self-hosted 3D-printing workspace for OrcaSlicer and Klipper / Kalico printers.
Upload a model, choose your presets and options, slice it, then explicitly start the print through
[Moonraker](https://moonraker.readthedocs.io/). Monitor printers and manage jobs from the same UI.
One Docker container runs FastAPI and the OrcaSlicer CLI; there is no database, built-in account
system, shared filesystem with the printer, or software to install on the printer host.

> **Security:** SliceDeck has no built-in authentication. Anyone who can reach it can operate your
> printer. Use a trusted network, VPN or authenticated reverse proxy; see [SECURITY.md](SECURITY.md).
>
> **Existing installations:** SliceDeck was previously called print-portal. Existing image,
> container and data identifiers retain that name for compatibility.

```
browser ──upload──► SliceDeck ──OrcaSlicer CLI──► plate_1.gcode ──HTTP──► Moonraker ──► printer
```

## Features at a glance

- **Browser-to-printer workflow:** stage STL, OBJ, 3MF or ZIP uploads, select presets and print
  options, slice with OrcaSlicer, and explicitly send completed G-code to a ready printer.
- **Prepare and preview:** configure supports and brim, auto-orient or shrink compatible models,
  optionally generate support fins, and preview finned models and sliced toolpaths.
- **Keep working on a job:** download, reprint or delete G-code; create an independent reslice draft,
  change its settings or proportionally resize its model; or generate G-code to print from a layer.
- **Manage printers and presets:** store multiple Moonraker connections, associate printer and process
  presets, import OrcaSlicer bundles, edit supported presets and embed a Fluidd or Mainsail UI.
- **Monitor locally:** view printer status, progress and temperatures alongside your jobs, without
  needing a cloud service.

## Why this exists

- **OrcaSlicer without a desktop.** The slicer you already trust, reachable from a phone, a tablet, or
  anyone's laptop — with your presets imported once, not re-exported per machine.
- **Slicing off the printer host.** Klipper's `klippy` has to feed the MCU on a tight schedule; a
  multi-gigabyte slice on the same box competes for CPU and RAM and shows up as stutter on long
  prints. The portal slices wherever the container runs and sends the printer only a finished file.
- **Printer management in one place.** Your printers, their credentials, their preset pairing, live
  status, and the printer's own web UI — Fluidd or Mainsail — embedded in the same page.
- **Minimal by design.** One container, one bind mount, one background worker. It speaks Moonraker's
  HTTP API only, so it does not care whether the host runs mainline **Klipper** or **Kalico** (the
  Klipper fork): same API, same behaviour.

It does not flash firmware, edit `printer.cfg` or run G-code macros — that stays in Klipper's own
config and web console, which the portal frames for you.

## What it does

1. **Upload** — `.stl`, `.3mf`, `.obj` or `.zip`. A `.zip` is unpacked fail-closed (traversal, symlink
   and entry-count rules) and every printable member becomes its own staged job; nothing is sliced
   until that job is confirmed. A `.3mf` is read for its printer/process identity. In a zip, members
   are matched in archive order and each job keeps the presets from its own turn; the dashboard
   selection ends on the last `.3mf`.
2. **Match** — the presets named inside a `.3mf` are matched against your imported presets and a
   job-local copy is re-identified so OrcaSlicer accepts it. STL and OBJ skip this step.
3. **Slice** — one background worker runs the OrcaSlicer CLI with the active machine, process and
   filament presets. Optional **auto-orient** and **shrink to fit** (never enlarges; refuses a model
   that would need more than a 4× shrink).
4. **Print** — the gcode is uploaded to Moonraker's `gcodes` root and `printer/print/start` is called,
   gated on the printer reporting `ready` and `standby`/`complete`. Anything else is refused with the
   reason and nothing starts.
5. **Monitor** — state, current file, progress, nozzle and bed temperatures, homed axes, and the job
   list with every job's gcode, reprint and delete.

## Preview, reprint and recover

Completed slices offer a G-code toolpath preview and download; jobs can be reprinted or deleted.
**Print from layer…** can generate and download or print G-code starting at a chosen layer or Z height,
shifting the remaining layers down to the bed so the printed portions can be joined. When available,
you can use the printer's last recorded layer. This is not an in-place resume: check the preview,
adhesion and generated G-code before printing; not every model or G-code file is suitable.

## Reslice an existing job

A completed or failed job with its retained model offers **Reslice…**. This creates an independent
`awaiting_confirm` child, leaving the source's status, G-code and print history unchanged. The child
starts from the source's current retained model, including any fins or previously applied manual
resize—not its original ZIP or the CLI's oriented/fitted output. A printed job remains `sliced` and
can also be used. The child keeps its own copies, so deleting the source does not delete the child.

In the reslice dialog, choose the child-only printer, process and filament presets and supports, brim,
auto-orient and shrink-to-fit options. These edits do not change dashboard selections. The initial
choices are the source's preset **references**, not historical preset contents: later preset edits
are reflected when the child is queued. Missing or incompatible choices must be repaired; **Use
current dashboard settings** explicitly copies the current choices into the dialog. Once confirmed,
the directly loaded machine, effective process and filament files are captured for the child, but
this does not freeze the entire Orca datadir or embedded 3MF object overrides.

Enter one dimension in millimetres and **Apply size** to scale proportionally; **Reset size** restores
the child's starting model, not the original pre-fins upload. Manual resize happens before optional
CLI auto-fit, and scales fins too. Reslice auto-fit is unavailable for 3MF and finned models; finned
models are not auto-oriented. Some 3MF projects, including external references or geometry above
preview limits, cannot be previewed or resized; settings-only slicing remains available if preflight
accepts the input. Embedded per-object/project overrides may still apply, and unreadable 3MF identity
settings can prevent confirmation.

**Keep for later** or Escape keeps the draft; resume it from the job list. Apply size to persist it:
unapplied dimensions are only kept in the current browser tab. **Discard** deletes the child only.
**Slice new job** saves the settings and queues the child; it does **not** print. Printing still
requires a separate explicit action after slicing. Drafts are protected from age pruning but can be
removed individually or by **Delete finished**; completed children and sources are subject to normal
`KEEP_JOBS` retention. See [reslice API](docs/api.md#reslice) for revisions and error handling.

## Printer management

Printers live in the UI (**Settings**), not in the deployment: name, Moonraker URL, API key, and an
optional **Web UI URL**. Credentials and that address are stored encrypted in `data/printers.json`
using `data/secret.key` — back both files up together; losing the key means re-entering API keys.

- **The Printer tab** embeds the **Web UI URL** of the *active* printer (Fluidd, Mainsail, anything
  frameable) and always offers an "Open in a new tab" link, because those UIs may refuse framing.
- **Switching printers** re-frames that tab with the new printer's address.
- **Per-printer presets** — each printer can pin its own machine/process pair; selecting the printer
  applies it, and changing the pair on the dashboard writes it back onto the selected printer.
- The Web UI URL is **not** the Moonraker API address. It is opened by *your* browser, so it must be
  reachable from where you view the portal, and an `http://` UI inside an `https://` portal is blocked
  as mixed content. Put the printer UI behind HTTPS if you want it embedded over TLS.

One printer may also come from `PRINTER_URL` + `MOONRAKER_API_KEY` — the read-only "environment
printer", whose credential fields stay owned by the deployment and whose Web UI fallback is
`PRINTER_UI_URL`. Everything else is editable in Settings.

Basic settings of machines, process presets and filaments can be edited (and machines and processes
deleted) on the **Presets** and **Filaments** pages — deleting a machine also deletes its processes.

## Quickstart

### Docker Compose (recommended)

Run the published image without cloning the repository or building locally. This example is based
on the Compose configuration we use on our homeserver. Create a directory and save the following as
`docker-compose.yml`:

```yaml
services:
  print-portal:
    image: pank85/print-portal:latest
    container_name: print-portal
    restart: unless-stopped
    env_file:
      - path: .env
        required: true
    ports:
      - "8088:8088"
    volumes:
      - ./data:/data
    mem_limit: 2g
    healthcheck:
      test: ["CMD-SHELL", "python3 -c \"import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8088'), timeout=4).status == 200 else 1)\""]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 25s
```

In the same directory, create the optional settings file and writable data directory, then start:

```bash
touch .env                  # optionally add PRINTER_URL and MOONRAKER_API_KEY
mkdir -p data
sudo chown 1000:1000 data  # container runs as uid 1000
docker compose up -d
```

Open <http://localhost:8088> and follow [First run](#first-run). To update a normal Compose
installation later, run `docker compose pull` followed by `docker compose up -d`. The `latest` tag
moves; pin a published commit-SHA tag for a reproducible deployment. If Compose is managed by
systemd as on our homeserver, follow the [systemd deployment instructions](docs/deployment.md#deploying-by-hand)
instead of recreating it directly.

Requires Docker Compose **≥ 2.24**. The published image retains the `pank85/print-portal` name and
is **amd64 only** and ~1.1 GB. Jobs, presets, printer settings and sliced G-code are stored in
`./data`; back it up before upgrades, and do not delete it unless you intend a factory reset. The
healthcheck can report unhealthy before a printer is configured; see [First run](#first-run).

### Build from source

```bash
git clone https://github.com/pank85/slicedeck.git
cd slicedeck
cp .env.example .env        # optional: PRINTER_URL + MOONRAKER_API_KEY
mkdir -p data sliced
sudo chown 1000:1000 data sliced
docker compose up -d --build
```

The repository also provides [`docker-compose.hub.yml`](docker-compose.hub.yml), an alternative
published-image setup with a separate `./sliced` mount. More on upgrades, rollback and persistent
storage: [docs/deployment.md](docs/deployment.md).

## First run

The image ships **no presets** on purpose: a preset that does not match your printer fails at the
printer, after the upload already looked successful. Two things to do once:

1. **Import a preset bundle.** In OrcaSlicer: **File → Export → Export Preset Bundle…**, tick
   **Printer** (plus the process and filament presets you want), save the `.orca_printer`, and upload
   it in the portal's profile card. Each bundle is imported under its own names; importing a second
   one adds a second machine while the first stays selectable. Export per platform:
   [docs/orcaslicer-presets.md](docs/orcaslicer-presets.md).
2. **Add a printer.** Settings → **Add printer**: Moonraker URL, API key, and optionally the **Web UI
   URL** of Fluidd/Mainsail. Getting a Moonraker key: [docs/printer-setup.md](docs/printer-setup.md).

Until a bundle is imported, reads answer normally but slicing returns `409 no_printer_profile`. Until
a printer is configured, `/api/health` returns 503 and printer routes answer 503.

## Configuration

Every variable has a working default, and a printer can arrive entirely through the UI.

| Var | Default | Meaning |
|---|---|---|
| `PRINTER_URL` | `http://127.0.0.1:7125` | Moonraker base URL of the environment printer |
| `MOONRAKER_API_KEY` | *(optional)* | its API key; set together with `PRINTER_URL` |
| `PRINTER_UI_URL` | *(unset)* | Web UI fallback for the environment printer |
| `MOONRAKER_ROOT` | `gcodes` | Moonraker file root for uploads |
| `DATA_DIR` | `/data` | jobs, presets, printer store, slicer `HOME` |
| `SLICED_DIR` | `$DATA_DIR/sliced` | second copy of every gcode, for the host |
| `MAX_UPLOAD_MB` | `150` | upload cap, and the combined uncompressed size of models extracted from one zip |
| `KEEP_JOBS` | `25` | job directories kept before the oldest are pruned |
| `SLICE_TIMEOUT` | `900` | seconds before a slice is killed |
| `SUPPORT_FINS_TIMEOUT` | `300` | seconds one support-fins run may take (runs are serialised) |
| `BED_SIZE` | `220x220` | clamps imported machine presets |
| `PORT` | `8088` | web UI port inside the container |

Every variable, the datadir layout and the seeding rules: [docs/configuration.md](docs/configuration.md).

## API

`GET /api/health` · `/api/printer` · `/api/ui` · `/api/jobs` · `/api/support-fins` · `/api/printers`
· `/api/profile` (with `POST /api/upload`, `POST /api/jobs/{id}/slice`, `GET /api/jobs/{id}/preview`,
`POST /api/jobs/{id}/reslice`, `PATCH /api/jobs/{id}/settings`, `POST /api/jobs/{id}/scale`,
`POST /api/jobs/{id}/scale/reset`, `POST /api/jobs/{id}/print`, printer CRUD and selection, profile import/select). Full surface with
status codes and error bodies: [docs/api.md](docs/api.md).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `printer unreachable` (503) | The container cannot route to the Moonraker URL. |
| `Moonraker rejected the API key` (502) | Wrong or disallowed key — regenerate it and update Settings. |
| `-17 incompatible profile` | A `.3mf` still names a foreign printer/process, or a process preset does not list the selected machine. Re-import the bundle; uploads are re-identified automatically. |
| model bigger than the bed | Turn on **shrink to fit**, or scale the model and upload it again. |
| `Permission denied` on `/data` | The bind mount is root-owned: `sudo chown -R 1000:1000 ./data`. |
| blank frame in the Printer tab | Fluidd/Mainsail refuse framing, or the URL is http inside an https portal — use the new-tab link or HTTPS. |

Every failure mode with the exact message it produces: [docs/troubleshooting.md](docs/troubleshooting.md).

## Limitations

- **No built-in authentication.** Anyone who can reach the port can upload and start a print. Put it
  behind an authenticated reverse proxy or a VPN — see [SECURITY.md](SECURITY.md).
- **amd64 only** — the OrcaSlicer AppImage is x86_64.
- **One slice at a time** — a single worker, because OrcaSlicer peaks at ~1 GB RAM.
- The print gate is checked when you click, not re-checked during the print.

## Docs

| | |
|---|---|
| [architecture.md](docs/architecture.md) | components, request flow, job lifecycle |
| [api.md](docs/api.md) · [configuration.md](docs/configuration.md) | every route, every variable |
| [deployment.md](docs/deployment.md) · [runbook.md](docs/runbook.md) | install, upgrade, rollback, verifying a deploy |
| [presets.md](docs/presets.md) · [orcaslicer-presets.md](docs/orcaslicer-presets.md) | how presets are imported and loaded |
| [troubleshooting.md](docs/troubleshooting.md) | failure modes and their real messages |

## Optional support fins

Opt-in (default OFF) in the staged job's **Print options** panel: tick **Use support fins** and, when you click **Slice it**, the portal runs the
bundled [support-fins](#acknowledgements) CLI locally on the upload, then slices the finned 3MF instead
of the original. STL, 3MF and OBJ are accepted (a ZIP is unpacked first; each printable member is its own staged job) and the output is always a 3MF;
the original upload stays on disk. `GET /api/support-fins` reports whether the runtime is present; the
checkbox is shown on the staged panel and disabled, with the reason in the hint, when the CLI is absent.

Every upload is staged, not queued:

- Uploads come back `awaiting_confirm` and bind the **Print options** panel on the upload card. A zip with more than one model returns a `jobs` array and stages each member separately; confirm each one. Nothing
  enters the queue until you click **Slice it**. The preview dialog does not open by itself.
- Staged jobs offer **Print options** on the row to bind that job to the options panel, and **Preview fins**. Preview is optional: it
  shows what the CLI did (`"14 walls, 24 tines"`, overhang regions, fin/tine/brace counts) and a
  **local** 3D preview of the finned 3MF: drag to orbit, wheel to zoom, arrows when the canvas is
  focused, chips to show/hide the part and the supports. No geometry, no upload and no font ever leaves
  the container.
- Exactly one **Slice it** button lives in the **Print options** panel (`POST /api/jobs/{id}/slice`).
  Inside the preview dialog, **Keep for later** leaves the draft staged, and **Discard** deletes it.
- A slice-time CLI failure is a `400 support fins failed: <cli text>`, the job directory remains, and the
  status stays `awaiting_confirm`.

Limits worth knowing before you tick the box:

- **A 3MF project's own slicer settings are dropped.** The upload is re-identified against the selected
  printer/process first, then finned, but the CLI's 3MF carries no OrcaSlicer identity metadata — so the
  rewritten copy is discarded rather than sliced with presets that no longer match. The job row records
  it.
- **One part per upload.** The portal exposes no per-object or per-plate selection, so multi-part
  projects are not supported for finning.
- **Orientation is preserved** — no rotation is applied, the part is finned the way it lies (the finned mesh is not auto-oriented; the upstream CLI recentres it in XY, e.g. x=[0,76] becomes x=[-38,38], without rotating). Shrink-to-fit is refused for finned jobs because OrcaSlicer requires --orient for --scale. A shallow
  overhang can legitimately yield **zero fins** (`cube.stl` does); that is still a success.
- **The node runtime costs ~123 MB of image** (`node:22.23.4-bookworm`, digest-pinned and smoke-tested at
  build time), which is why the image is ~1.1 GB.
- **Serialised and bounded**: one fin run at a time, each capped by `SUPPORT_FINS_TIMEOUT` (300 s), so a
  second finned upload waits for the first.
- **The preview is bounded** to 450 000 triangles and 32 MiB of input for the rotatable view. A larger finned file, up to the 150 MiB upload limit, shows still pictures (front and side) instead of refusing. Past that the dialog reports `preview unavailable: …`.
- **A staged job is protected from age pruning** (absent from ACTIVE_STATUSES but in PRUNE_KEEP_STATUSES) and can be deleted
  individually or by **Delete finished**; it simply never enters the queue until you confirm.

## Acknowledgements

G-code toolpath preview uses [gcode-preview](https://github.com/remcoder/gcode-preview),
licensed under the MIT License.

Support fins (optional designed-in breakaway supports) are based on the **Support Fins** project by
Matthew Trahan (gittrahan) — <https://github.com/gittrahan/support-fins> — licensed under the MIT
License. The bundled CLI (`app/support_fins.mjs`) is unmodified upstream build output; its provenance
(version, sha256, how it was built), the MIT copyright notice and the Apache-2.0 terms of the bundled
`manifold-3d` are in [`app/SUPPORT_FINS_NOTICE.txt`](app/SUPPORT_FINS_NOTICE.txt). See the upstream
repository for the original implementation and documentation.

## License

MIT — see [LICENSE](LICENSE).
