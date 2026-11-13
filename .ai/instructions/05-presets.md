## Presets

A fresh datadir ships no presets at all — only the empty `system/Custom.json` registry — so the first thing a user does is import an `.orca_printer` bundle.
An import writes the machine under its own name (`system/Custom/machine/<name>.json`) and registers it in `machine_list`.
The dated preset the portal used to copy (`generic-klipper-0.4.json`) is now only a test fixture under `tests/fixtures/preset-datadir/`.
`-17` is the CLI's process↔machine compatibility check (`compatible_printers` naming), not a machine-identity rule; a separately named machine slices fine.
The one hard literal is `"from": "system"` on the machine preset (`"user"` → `-17`, `"System"`/absent → rc 251); process and filament presets mirror it with `"User"`.
Full paths and the export procedure: [docs/orcaslicer-presets.md](../../docs/orcaslicer-presets.md)
The pipeline itself, including what a fresh install looks like step by step: [docs/presets.md](../../docs/presets.md)
