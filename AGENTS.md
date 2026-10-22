# AGENTS.md

Entry point for agent sessions in **print-portal**. Read this first, then the file it points to.

## What this project is

Self-hosted FastAPI service that turns an STL/3MF/OBJ upload into a finished print on a Klipper
printer: the container slices with the OrcaSlicer CLI binary, then hands the `.gcode` to Moonraker
over HTTP. One container, no database, no shared filesystem with the printer host.

## Where things live

| Path | Purpose |
|------|---------|
| `README.md` | User-facing docs: flow, config env vars, API table, preset import/caveats |
| `docs/printer-setup.md` | Moonraker side: file manager, API key, `trusted_clients`, reachability |
| `docs/orcaslicer-presets.md` | OrcaSlicer preset locations (desktop + container) and `.orca_printer` export |
| `docs/deployment.md` | Deployment |
| `app/` | FastAPI app; `app/profiles.py` owns preset import/rebase/derivation |
| `presets/datadir/` | Seed datadir copied into the image at `/opt/orca-datadir` (read-only seed) |
| `docker/`, `Dockerfile`, `docker-compose.yml` | Image build with pinned AppImage + sha256, build-time slice smoke test |
| `.gitlab-ci.yml` | CI/CD |
| `.orchestration/` | Ergo run plans/context |

Runtime paths inside the container (see `README.md` → Configuration): `ORCA_BIN=/opt/orca/bin/orca-slicer`,
`ORCA_DATADIR=$DATA_DIR/orca-datadir` (writable copy), `HOME=$DATA_DIR/home`.

## UI work

Any task that changes how the interface looks, feels, moves or is interacted with (`app/index.html`, its
inline CSS/JS, the dashboard or settings views) must load the pi skill **ui-ux-pro-max** first —
`/skill:ui-ux-pro-max` in a session, `~/.pi/agent/skills/ui-ux-pro-max` on disk. Follow its guidance for
layout, spacing, typography, colour, interaction and accessibility instead of ad-hoc CSS choices. Skip it
only for pure backend work.

## Presets

The OrcaSlicer CLI refuses a machine preset whose identity is not the indexed slot (`-17`), so the portal merges an imported machine onto the slot `system/Custom/machine/generic-klipper-0.4.json` instead of creating a new machine.
Full paths and the export procedure: [docs/orcaslicer-presets.md](docs/orcaslicer-presets.md)
