# OrcaSlicer presets

This page covers where OrcaSlicer keeps its presets on each platform, what this container does with
them, and how to export a `.orca_printer` from the desktop app so you can upload it to print-portal.

It exists to stop the two usual mistakes: looking for "the printer preset" or "the process preset"
and not finding it, and editing a bundled preset that gets overwritten on the next update.

## Preset types

Layout is the same everywhere — one JSON file per preset, one directory per type:

```text
machine/    printer presets      (Preset.hpp: PRESET_PRINTER_NAME = "machine")
process/    process presets      (PRESET_PRINT_NAME   = "process")
filament/   filament presets     (PRESET_FILAMENT_NAME = "filament")
```

## Desktop OrcaSlicer on Windows

### User (own) presets

These live in the `user/` tree, one subdirectory per user profile folder, default name `default`
(`DEFAULT_USER_FOLDER_NAME`):

```text
%APPDATA%\OrcaSlicer\user\default\machine\
%APPDATA%\OrcaSlicer\user\default\process\
%APPDATA%\OrcaSlicer\user\default\filament\
```

### The data directory

`data_dir` on Windows is `wxStandardPaths::GetUserDataDir()`, i.e.
`C:\Users\<user>\AppData\Roaming\OrcaSlicer` (confirmed by the source's own log-path comment:
`AppData\Roaming\OrcaSlicer\log\`). Other contents: `OrcaSlicer.conf` (app config: selected
presets, `preset_folder`, enabled vendors), `log/`, `system/`, `ota/`.

### Bundled ("system") presets

Bundled presets ship with the app and are read-only:

```text
C:\Program Files\OrcaSlicer\resources\profiles\<Vendor>\machine|process|filament\
C:\Program Files\OrcaSlicer\resources\profiles\<Vendor>.json          (vendor index: name + version)
%LOCALAPPDATA%\Programs\OrcaSlicer\resources\profiles\...             (per-user install, no admin)
```

### Portable mode

A directory named `data_dir` next to `OrcaSlicer.exe` wins — the app then uses `<install>\data_dir`
instead of `%APPDATA%`.

## Linux and macOS

Linux/macOS equivalents of `data_dir`: `$XDG_CONFIG_HOME/OrcaSlicer` (default
`~/.config/OrcaSlicer`) on Linux since 2.3; `~/Library/Application Support/OrcaSlicer` on macOS (wx
standard location).

## This container

The headless datadir is Bambu/Orca "system JSON" style, so system presets live under `system/`, not
`user/`:

```text
$ORCA_DATADIR/system/<Vendor>/machine|process|filament   writable copy (default $DATA_DIR/orca-datadir)
$ORCA_DATADIR/system/Custom/...                          where the portal writes imported presets
/opt/orca-datadir/system/Custom/...                      read-only seed from presets/datadir/ in this repo
```

`app/profiles.py` resolves the machine slot as `system/Custom/machine/generic-klipper-0.4.json`,
falls back to the first instantiated `system/**/machine/*.json`, and writes the derived options
preset to `system/Custom/process/print-portal-options.json` (hidden from the picker).

Imported presets land in `$ORCA_DATADIR/system/Custom/{machine,process,filament}`. On container
start `docker/entrypoint.sh` re-seeds `/opt/orca-datadir` into `$ORCA_DATADIR` whenever the
`.seed-version` marker does not match `SEED_VERSION` (`2.4.2-1` in the `Dockerfile`).

## Gotchas

- Never edit bundled/system presets: they are replaced on app update, and the container's are
  re-seeded (bump `SEED_VERSION` to force re-seed on upgrade).
- Preset filename = preset identity. `inherits` must name an existing parent exactly, and a user
  preset may not shadow a system file of the same name.
- The portal's importer rejects/renames filenames containing spaces (OrcaSlicer CLI quirk) — see
  the caveats in the [README](../README.md).
- OrcaSlicer rescans preset directories at startup only: close it (or restart the container) after
  adding JSON by hand.
- The CLI refuses a machine identity that is not the indexed slot with `-17` (`"The selected printer
  is not compatible with the process preset in the 3mf"`), even when the process lists that name in
  `compatible_printers`.

## Exporting your presets (.orca_printer)

On a Windows desktop install:

1. In OrcaSlicer open **File → Export → Export preset bundle**.
2. Tick the **printer** preset(s) you want to include, then press **OK**.
3. On the next screen **select the printer** (the bundle's target printer), then save the
   `.orca_printer` file.
4. Upload that file in print-portal: **Profile card → Upload .orca_printer**. The endpoint behind
   it, `POST /api/profile/upload`, is documented under "Importing profiles" in the
   [README](../README.md).

The portal merges the bundle's machine settings onto the stock machine slot and re-points
`compatible_printers`, so the printer name you see in the portal stays the slot's name rather than
the name from the bundle. That is deliberate: the OrcaSlicer CLI refuses a new machine identity with
`-17`, so applying the bundle's settings onto the existing slot is the only way it loads.
