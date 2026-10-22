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
| `docs/deployment.md` | Deployment |
| `app/` | FastAPI app; `app/profiles.py` owns preset import/rebase/derivation |
| `presets/datadir/` | Seed datadir copied into the image at `/opt/orca-datadir` (read-only seed) |
| `docker/`, `Dockerfile`, `docker-compose.yml` | Image build with pinned AppImage + sha256, build-time slice smoke test |
| `.gitlab-ci.yml` | CI/CD |
| `.orchestration/` | Ergo run plans/context |

Runtime paths inside the container (see `README.md` → Configuration): `ORCA_BIN=/opt/orca/bin/orca-slicer`,
`ORCA_DATADIR=$DATA_DIR/orca-datadir` (writable copy), `HOME=$DATA_DIR/home`.

## OrcaSlicer preset locations

Symptom this section prevents: looking for "the printer preset" or "the process preset" and not
finding it, or editing a bundled preset that gets overwritten on the next update.

Layout is the same everywhere — one JSON file per preset, one directory per type:

```
machine/    printer presets      (Preset.hpp: PRESET_PRINTER_NAME = "machine")
process/    process presets      (PRESET_PRINT_NAME   = "process")
filament/   filament presets     (PRESET_FILAMENT_NAME = "filament")
```

### Desktop OrcaSlicer on Windows

User (own) presets — the `user/` tree, one subdirectory per user profile folder, default name
`default` (`DEFAULT_USER_FOLDER_NAME`):

```
%APPDATA%\OrcaSlicer\user\default\machine\
%APPDATA%\OrcaSlicer\user\default\process\
%APPDATA%\OrcaSlicer\user\default\filament\
```

`data_dir` on Windows is `wxStandardPaths::GetUserDataDir()`, i.e. `C:\Users\<user>\AppData\Roaming\OrcaSlicer`
(confirmed by the source's own log-path comment: `AppData\Roaming\OrcaSlicer\log\`). Other contents:
`OrcaSlicer.conf` (app config: selected presets, `preset_folder`, enabled vendors), `log/`, `system/`, `ota/`.

Bundled ("system") presets ship with the app and are read-only:

```
C:\Program Files\OrcaSlicer\resources\profiles\<Vendor>\machine|process|filament\
C:\Program Files\OrcaSlicer\resources\profiles\<Vendor>.json          (vendor index: name + version)
%LOCALAPPDATA%\Programs\OrcaSlicer\resources\profiles\...             (per-user install, no admin)
```

Portable mode: a directory named `data_dir` next to `OrcaSlicer.exe` wins — the app then uses
`<install>\data_dir` instead of `%APPDATA%`.

Linux/macOS equivalents of `data_dir`: `$XDG_CONFIG_HOME/OrcaSlicer` (default `~/.config/OrcaSlicer`)
on Linux since 2.3; `~/Library/Application Support/OrcaSlicer` on macOS (wx standard location).

### This container

The headless datadir is Bambu/Orca "system JSON" style, so system presets live under
`system/`, not `user/`:

```
$ORCA_DATADIR/system/<Vendor>/machine|process|filament   writable copy (default $DATA_DIR/orca-datadir)
$ORCA_DATADIR/system/Custom/...                          where the portal writes imported presets
/opt/orca-datadir/system/Custom/...                      read-only seed from presets/datadir/ in this repo
```

`app/profiles.py` resolves the machine slot as `system/Custom/machine/generic-klipper-0.4.json`,
falls back to the first instantiated `system/**/machine/*.json`, and writes the derived options
preset to `system/Custom/process/print-portal-options.json` (hidden from the picker).

### Gotchas

- Never edit bundled/system presets: they are replaced on app update, and the container's are
  re-seeded (bump `SEED_VERSION` to force re-seed on upgrade).
- Preset filename = preset identity. `inherits` must name an existing parent exactly, and a user
  preset may not shadow a system file of the same name.
- The portal's importer rejects/renames filenames containing spaces (OrcaSlicer CLI quirk) — see
  `README.md` caveats.
- OrcaSlicer rescans preset directories at startup only: close it (or restart the container) after
  adding JSON by hand.
