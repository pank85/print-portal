# print-portal docs

Documentation for the print-portal **application**: how a request is handled, how presets are imported
and sliced, what the API and the configuration surface are.

## Read this first: two states

This worktree is `develop` @ `dc92ca1`. A preset/import rewrite is **uncommitted** on
`ergo/20261022T123034Z-import-name` in `/home/pank/projects/print-portal`, built by another agent. The
two behave differently, so every page labels them:

| | State | Where it was read | Status |
|---|---|---|---|
| **A** | `develop` @ `dc92ca1` | this worktree | shipped |
| **B** | `ergo/20261022T123034Z-import-name` | read-only snapshot of the live tree | **in progress, NOT in develop** |

Verified before documenting: `dc92ca1` has no `VENDOR_LISTS`, `NO_MACHINE_MESSAGE`, `read_vendor_index`,
`require_machine` or `selection_from_import`, and its `presets/datadir/` ships 11 preset files. B ships
only `system/Custom.json` and writes every machine under its own name.

## Files

| File | What it covers |
|---|---|
| [presets.md](presets.md) | A's merge-onto-stock-slot import, B's own-name import + vendor-index registration, and the `-17` rule (which A misdiagnosed). |
| [architecture.md](architecture.md) | Components and request flow: browser → FastAPI → OrcaSlicer CLI → Moonraker; how a job runs; `data/` vs image. |
| [api.md](api.md) | Every route in `app/main.py`, with A/B status codes where they differ. |
| [configuration.md](configuration.md) | Every environment variable and option the app reads, with defaults. No secret values. |
| [runbook.md](runbook.md) | Operations: verify a deploy is the build you think, roll back (and what that does not restore), restart/seed behaviour, fresh-install state. |
| [troubleshooting.md](troubleshooting.md) | Real failure modes with the text each state actually produces. |

Existing pages in this folder, not part of the set above:

| File | What it covers |
|---|---|
| [orcaslicer-presets.md](orcaslicer-presets.md) | OrcaSlicer preset locations per desktop platform, `.orca_printer` export, and the container paths (A/B noted). |
| [printer-setup.md](printer-setup.md) | Moonraker side: file manager, API key, `trusted_clients`. |
| [deployment.md](deployment.md) | Host-side deployment notes (pre-existing). |

User-facing overview and quickstart: top-level [README](../README.md).

## How to read a claim

Each non-obvious statement carries a file and symbol, and a state letter when the states differ. Claims
that could not be checked against code are marked **UNVERIFIED** with what would confirm them. Build, CI
and host deployment are out of scope.

Quoted error text is taken from the code that raises it (the `raise`/`HttpException`/`ProfileError`
site). The one exception is the OrcaSlicer CLI's own `-17` / `251` messages, which come from the binary
and are only recorded in this repo — that is stated where they appear.
