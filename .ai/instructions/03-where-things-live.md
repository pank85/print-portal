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
| `.github/workflows/ci.yml` | CI: tests, then Docker Hub publish on `master`. No deploy. |
| `.orchestration/` | Ergo run plans/context |

Runtime paths inside the container (see `README.md` → Configuration): `ORCA_BIN=/opt/orca/bin/orca-slicer`,
`ORCA_DATADIR=$DATA_DIR/orca-datadir` (writable copy), `HOME=$DATA_DIR/home`.

