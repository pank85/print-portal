# Deployment notes

## Reverse proxy

The portal has no authentication of its own. Put it behind a reverse proxy that terminates TLS and
requires authentication (basic auth, OAuth, or a VPN). The example below is nginx:

```nginx
server {
    listen 443 ssl;
    server_name print.example.com;

    client_max_body_size 150m;      # match MAX_UPLOAD_MB
    proxy_read_timeout 900s;        # match SLICE_TIMEOUT

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`client_max_body_size` must be at least `MAX_UPLOAD_MB` or large uploads are rejected by the proxy
before they reach the app. `proxy_read_timeout` should be at least `SLICE_TIMEOUT` so a long slice
is not cut off.

## The container must reach Moonraker

The portal calls Moonraker from inside the container, so `PRINTER_URL` must be routable from the
container's network. Two common options:

- **Routable address** — set `PRINTER_URL=http://192.168.1.10:7125` (or `http://printer.local:7125`)
  and make sure the container's network can reach it. This is the default and works with a bridge
  network.
- **Host networking** — if the printer is only reachable on the Docker host's loopback, run the
  container with `network_mode: host` and set `PRINTER_URL=http://127.0.0.1:7125`.

Do not rely on the browser reaching Moonraker; all printer calls are server-side.

## Data directory

`./data` is a bind mount for `/data` and holds jobs, uploaded presets, the profile selection and the
slicer datadir. The container runs as uid 1000, so the host directory must be writable by that user:

```bash
mkdir -p data
chown -R 1000:1000 data
```

If you skip this, the container may fail to write jobs or seed presets.

## Backups

Everything worth keeping lives in `./data` (and `./sliced` when `SLICED_DIR` points outside it):

- `jobs/` — uploaded models, sliced gcode and job metadata
- `orca-datadir/` — the slicer datadir, including any presets you imported
- `profile.json` — the active machine/process/filament selection
- `secret.key` — Fernet key for the encrypted printer store (mode 0600)
- `printers.json` — UI-managed printers, API keys stored as Fernet ciphertext
- `sliced/` — sliced output when `SLICED_DIR` points inside the data dir

Back up `./data` as a unit (and `secret.key` together with `printers.json`). Restoring it restores
your jobs, imported presets and configured printers. Losing `secret.key` means re-entering API keys.

## Sliced output mount

Mount `./sliced:/sliced` (or set `SLICED_DIR`). Fix permissions: `chown 1000:1000 sliced`.

## Upgrades and `SEED_VERSION`

On startup the entrypoint copies the image's seed into `$ORCA_DATADIR` with `cp -R -n`
(never overwriting), guarded by a `SEED_VERSION` marker file. The seed no longer carries any preset —
just the empty vendor index `system/Custom.json` — because importing a bundle is the only way presets
get in. This means:

- **new** seed files appear after an upgrade,
- your **edits and imports survive**,
- but **renamed or deleted** seed presets are **not removed** — the old file stays,
- an empty seed can never resurrect a preset, and
- an **existing** deployment keeps the presets it already has: `cp -R -n` never overwrites, so this
  seed change only reaches a new volume (or an operator who deletes the old presets).

`SEED_VERSION` is a build-time value (`ENV` in the Dockerfile, overridable with `--build-arg`).
Bump it only when you want the copy step to run again (for example, after the seed content changed).
To remove a stale preset, delete it from `./data/orca-datadir` by hand. For the fresh-install state,
delete `$DATA_DIR/orca-datadir/system/Custom/` once — that removes the old seed *and* the presets you
imported, so re-import your bundle afterwards. No migration deletes your data on its own.

Rebuild and recreate the container to pick up a new image (plain compose setup; on the
homeserver the `docker-compose@print-portal` unit owns the project — see
[Deploying from CI](#deploying-from-ci)):

```bash
docker compose up -d --build
```

## Publishing the image from CI

`.github/workflows/ci.yml` runs the test suite on every push and pull request. On `master`
it also builds the `linux/amd64` image and pushes it to Docker Hub as `pank85/print-portal`
with three tags: the 8-character commit SHA, `<VERSION>-<sha>` and `latest`. `<VERSION>` is
read from `app/config.py`, which stays the single version source.

Two repository secrets are required (**Settings → Secrets and variables → Actions**):
`DOCKERHUB_USER`, and `DOCKERHUB_TOKEN` — an access token allowed to push that repository.
Without them the publish job fails closed rather than skipping, so a green run always means
the image was published.

Builds run on `ubuntu-latest`, which supplies the Docker daemon. The build pins
`--platform linux/amd64` because the OrcaSlicer AppImage in the image is x86_64 only, and
the resulting image is ~1.1 GB. Publishing is serialized per ref, so two pushes cannot
race on `latest`.

The pipeline does **not** deploy. A published image changes nothing on the homeserver
until an operator pulls it.

## Deploying by hand

`172.16.0.50` is the homeserver's LAN address. Its compose project lives at
`/home/pank/homeserver/docker-compose/print-portal`, pins
`pank85/print-portal:latest`, names the container `print-portal`, publishes port 8088, and
is owned by the `docker-compose@print-portal` systemd unit.

`docker-compose@.service` runs `docker compose up` in the **foreground** with
`Restart=always`. A direct `docker compose up -d --force-recreate` therefore kills the
foreground compose process, systemd treats the exit as a failure, runs `compose down`, and
removes the container that was just recreated — the unit's `Restart=always` then brings a
container back after ~10 s, so the cost is a double recreate and a short outage, not a dead
portal. Instead:

```bash
cd /home/pank/homeserver/docker-compose/print-portal
sudo docker compose pull
sudo systemctl restart docker-compose@print-portal
```

Use `systemctl restart`, **never** `systemctl reload` (the unit's own ExecReload is the
unsafe `pull && up -d --force-recreate` form).

Two assumptions about that host, both required:

1. The compose project is managed by a `docker-compose@print-portal` systemd unit, so the
   directory is stable and a deploy only pulls and restarts.
2. Its compose file references the registry image (`image:`), never a `build:` block — with
   both set, compose still pulls on `up`, but the rule keeps a deploy reproducible from the
   published image alone.

### Verify the deploy

After the restart, on the host:

1. `docker compose pull` succeeded — a failed pull leaves the running portal serving.
2. The `print-portal` container reports `healthy` within 180 s (90 polls × 2 s); the image
   healthcheck has a 30 s interval and a 25 s start period, so a shorter budget can fail a
   container that is merely warming up.
3. The pulled `:latest` is the build you meant to deploy:
   `sudo docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' 'pank85/print-portal:latest' | grep -F 'pank85/print-portal@' | head -n1`
   should match the digest from the publish job log, and `.Id` identifies the same image.
4. The running container uses that exact image:
   `sudo docker inspect -f '{{.Config.Image}} {{.Image}} {{.State.Status}}' print-portal`
   must show `pank85/print-portal:latest`, the expected image ID, and `running`.

### Rollback

A failed pull or restart leaves whatever container compose last started in place. Recover
with `sudo systemctl restart docker-compose@print-portal` in that directory once the cause
is fixed. To roll back, pin the previous `<VERSION>-<sha>` tag in the compose file, pull and
restart again.
