# Configuration

`app/config.py` is the single source of truth: it is the only module that reads `os.environ`
(`config.py:1-10`), and every other module uses `config.load_config()`. Defaults below are the values
in `DEFAULTS` (`config.py:23`) and in the `source.get(...)` branches of `load_config`
(`config.py:123`).

No secret values appear in this document. `MOONRAKER_API_KEY` is the only secret read from the
environment; it is never printed or logged.

## Environment variables

| Variable | Default | Meaning | Validation |
|---|---|---|---|
| `PRINTER_URL` | `http://127.0.0.1:7125` | Moonraker base URL for the **environment printer** (the `env` row in Settings) | must start with `http://` or `https://`; trailing `/` stripped |
| `MOONRAKER_API_KEY` | *(unset)* | API key for the environment printer. Becomes the `X-Api-Key` header; never printed | must be set together with an explicit `PRINTER_URL` (see below) |
| `MOONRAKER_ROOT` | `gcodes` | Moonraker file root for uploads | none |
| `PORT` | `8088` | port uvicorn binds (`docker/entrypoint.sh:27`) | positive integer ≤ 65535 |
| `DATA_DIR` | `/data` | jobs, uploads, `profile.json`, `printers.json`, Qt `HOME` | none (not `~`/`$VAR`-expanded) |
| `ORCA_BIN` | `/opt/orca/bin/orca-slicer` | slicer binary inside the image | none |
| `ORCA_DATADIR` | `$DATA_DIR/orca-datadir` | writable slicer datadir (seed copy + imported presets) | `~` / `$VAR` / `${VAR}` expanded |
| `MAX_UPLOAD_MB` | `150` | upload cap in MiB; also bounds an unpacked archive member | positive integer |
| `KEEP_JOBS` | `25` | finished job directories retained before the oldest are pruned | positive integer |
| `SLICE_TIMEOUT` | `900` | seconds before a slice is killed | positive integer |
| `BED_SIZE` | `220x220` | bed size used to clamp an imported machine's `printable_area` | `WxH`, both finite and > 0 |
| `MACHINE_SLOT` | *(unset)* | optional override of the machine preset the slicer loads: a path inside the datadir | blank = "no override"; a bad override is an error in both states. With no override and no machine preset in the datadir: **A** raises `ProfileError` (`no machine preset found in <datadir> (set MACHINE_SLOT)`), **B** resolves to `None` → `409 no_printer_profile` on actions |
| `SLICED_DIR` | `$DATA_DIR/sliced` | mountable output directory for finished gcode | `~` / `$VAR` expanded |
| `PRINTERS_FILE` | `$DATA_DIR/printers.json` | printer store | `~` / `$VAR` expanded |
| `SECRET_KEY_FILE` | `$DATA_DIR/secret.key` | Fernet key used to encrypt API keys in the store | `~` / `$VAR` expanded |
| `ENV_PRINTER_NAME` | `Default (environment)` | display name of the environment printer | blank falls back to the default |
| `PRINTER_UI_URL` | *(unset)* | fallback embedded Fluidd/Mainsail address. Each printer carries its own **Web UI URL** (`ui_url` in `printers.json`, set in Settings); this variable seeds the `env` printer and covers an empty field | blank = not configured; must start with `http://` or `https://`, name a host, and carry no credentials, query or fragment; trailing `/` stripped. The same rule validates the per-printer field (`normalise_ui_url`, `printers.py`), where a bad value is a `400 ui_url …` instead of a startup failure |

`docker-compose.yml` also forwards `SLICED_DIR` (default `/sliced`, the second mount) and reads
`PORT` for the published port.

### The PRINTER_URL / MOONRAKER_API_KEY pairing

Two behaviours, both in `load_config`:

* the app boots **with neither** set — printers are then managed in Settings. `main.py:59` calls
  `load_config(printer_seed_required=False)` for exactly this reason;
* `PRINTER_URL` set with an empty `MOONRAKER_API_KEY` is a startup error:
  `PRINTER_URL is set but MOONRAKER_API_KEY is empty: set both (environment printer) or neither (manage printers in Settings)`.

The strict CLI `python -m config` fails closed without `MOONRAKER_API_KEY` (`ConfigError`, exit 2);
`python -m config --allow-unconfigured` is what the container entrypoint runs, and it prints
`print-portal: no PRINTER_URL/MOONRAKER_API_KEY set; manage printers in Settings` to stderr
(`config.py:205-224`, `docker/entrypoint.sh:26`).

### `PRINTER_UI_URL` (embedded Fluidd/Mainsail)

The address of the embedded printer UI is a property of a **printer**, not of the deployment: Settings →
**Web UI URL** (`ui_url` on the printer record, `printers.py`). It is published as `ui_url` on
`GET /api/printer`, and `GET /api/ui` answers with the *active* printer's value. Switching printers
re-frames the **Printer** tab with the new address.

`PRINTER_UI_URL` is the fallback: it seeds the environment printer (`env` row) and is used when the
active printer's field is empty. `parse_ui_url` (`config.py`) hands the variable to
`printers.normalise_ui_url` — one implementation for both surfaces, so the env var and the form cannot
drift apart. The portal never fetches this URL itself, so validation is shape-only — the two failure
modes are both the browser's, not the server's:

* the URL must be reachable **from the browser**, not from the container, so `http://127.0.0.1:80` from
the host's point of view is wrong;
* an `http://` UI inside an `https://` portal is blocked as **mixed content**. The tab detects this from
`location.protocol` and shows the warning instead of a blank frame; serve the printer UI over HTTPS to
embed it over TLS.

A bad env value fails loudly at startup, e.g. `PRINTER_UI_URL must start with http:// or https://`,
`PRINTER_UI_URL must not embed credentials`, or `PRINTER_UI_URL must not carry a query string`. The
same value typed into the Settings form is rejected with `400` and a detail naming `ui_url`
(`ui_url must ...`); an empty field means "no embedded UI" and is valid.

## The printer store (`printers.json`)

Settings → **Add printer** writes `DATA_DIR/printers.json`, a version 2 document. A version 1 file
still loads: reads never rewrite it, and the next change writes it as version 2. A single uvicorn
worker is assumed (there is no portal authentication).

A printer may carry an optional `profiles: {"machine": <rel>, "process": <rel>}` pairing of
datadir-relative preset paths. An absent pairing, or a `null` slot inside one, means that printer
inherits the global selection in `profile.json`. Selecting a printer applies its pairing, including
the displayed `machine_label`; choosing machine and process presets on the dashboard writes them back
onto the selected printer (best effort — a read-only store loses the stored pairing, never the
selection). The API checks that each path is a preset inside `ORCA_DATADIR` and answers `400`
`machine: '<rel>' is not a preset inside the datadir` otherwise.

The environment printer has no editable record to hold a pairing, so its pairing lives in an optional
top-level `env_profiles` object of the same `{"machine", "process"}` shape — written only for that
printer, and a malformed value is ignored on read so the printer falls back to the global selection
instead of the whole file counting as corrupt.

## Build / image variables

Set in the `Dockerfile` or the entrypoint, not by the operator — listed because they are read at
runtime.

| Variable | Value | Read by |
|---|---|---|
| `SEED_VERSION` | **A:** `2.4.2-1`, **B:** `2.4.2-2` (`Dockerfile:31`) | `docker/entrypoint.sh:5,16` — seeding runs when `.seed-version` differs |
| `HOME` | `/data/home` (`Dockerfile:30`), re-exported by the entrypoint | the slicer subprocess |
| `TMPDIR` | `/data/tmp` (`entrypoint.sh:24`) | Starlette's spooled uploads |
| `PORT` | `8088` (`Dockerfile:26`) | uvicorn |

The app does not inherit the container's full environment into the slicer: `run_slice` builds a
six-key environment by hand (`main.py:630-637`), so `MOONRAKER_API_KEY` never reaches the subprocess.

## Application options (not environment)

Stored in `$DATA_DIR/profile.json` and changed from the UI or `POST /api/profile/options`:

| Key | Default | Meaning |
|---|---|---|
| `options.supports` | `false` | `enable_support` = `"1"`/`"0"` in the derived process preset |
| `options.brim` | `true` | brim on/off; width comes from the preset, else `5` mm |
| `options.hide_profiles` | `false` | hide the preset selectors in the UI |

Only real JSON booleans count; a legacy or corrupt value falls back per key (`read_options`,
`profiles.py:102`). `machine_label` is stored in the same document but is a label, not a preset slot.

## UNVERIFIED

* Which of these values are actually overridden in a given installation. The app only ever sees the
  final environment, so `python -m config` on the running container is the way to confirm what is in
  effect.
