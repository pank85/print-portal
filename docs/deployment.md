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

`.gitlab-ci.yml` runs the test suite on every branch and, on `master`, builds the
`linux/amd64` image and pushes it to **two** registries with the same three tags:
`$CI_COMMIT_SHORT_SHA`, `$FULL_VERSION` (`$APP_VERSION-$CI_COMMIT_SHORT_SHA`) and
`latest`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CI_REGISTRY` | `dockerhub.pkatopodis.me` | Primary registry host. |
| `CI_REGISTRY_IMAGE` | `dockerhub.pkatopodis.me/pank85/print-portal` | Primary repository (all three tags). |
| `GITLAB_REGISTRY_IMAGE` | `gitlab-ce:5050/pank85/print-portal` | Mirror repository (same three tags). |
| `PRIMARY_IMAGE` | *(empty)* | Overrides `CI_REGISTRY_IMAGE`. |
| `MIRROR_IMAGE` | *(empty)* | Overrides `GITLAB_REGISTRY_IMAGE`; `none` disables the mirror push. |

The defaults are hardcoded in the file, so the pipeline needs no CI/CD variables
at all on this host. The primary push logs in to `$CI_REGISTRY` with the job's
`CI_REGISTRY_USER`/`CI_REGISTRY_PASSWORD`. With `MIRROR_IMAGE` unset the mirror
is `GITLAB_REGISTRY_IMAGE` (also on the runner box, same account); the job logs
in to the mirror's own registry host — the first path component of the mirror
reference, so a custom mirror works without extra variables — **only when the
mirror is enabled**, and `MIRROR_IMAGE=none` skips both the login and the push.
A project or group variable of the same name (**Settings > CI/CD > Variables**)
overrides any default, so the publishing jobs still work for a fork; the deploy job is master-only, automatic, and fails loudly without a key.

After the pushes the job records the digest it actually pushed for the target
repo (selecting the `RepoDigests` entry whose repo is `$TARGET`, because the
mirror push adds a second entry) and publishes it as a dotenv artifact
(`release.env`, `PORTAL_IMAGE_DIGEST=...`). The deploy job consumes that
artifact, so a deploy proves the host serves *this* build rather than whatever
`:latest` points at later.

The build pins `--platform linux/amd64` — the OrcaSlicer AppImage in the image is
x86_64 only. The dind service uses a per-job alias (`docker-$CI_JOB_ID`) with a
preflight that fails when the alias resolves to more than one daemon, because
this runner shares one docker network across concurrent jobs; the daemon
readiness probe is bounded (60 attempts, 1 s apart) and dumps `docker info`
instead of hanging when the daemon never answers.

Example fork override: `PRIMARY_IMAGE=registry.example.com/me/print-portal`,
`CI_REGISTRY=registry.example.com`,
`CI_REGISTRY_PASSWORD=<token>` (masked), `MIRROR_IMAGE=none`.

## Deploying from CI

The `deploy:production` job pulls the image this pipeline just pushed onto the
homeserver and restarts the systemd unit that owns the compose project. On
`master` it runs **automatically** as soon as `test` and `docker` are green —
there is no manual gate.

Because it is automatic, a missing deploy key is a **hard failure**, not a silent
skip: `before_script` stops with `DEPLOY FAIL: no SSH deploy key ...` when neither
`DEPLOY_SSH_PRIVATE_KEY_BASE64` nor `SSH_PRIVATE_KEY` is set, and the job also
refuses to run without the `PORTAL_IMAGE_DIGEST` artifact it has to assert on.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEPLOY_HOST` | `172.16.0.50` | Host to deploy to, reachable over SSH from the runner. |
| `DEPLOY_USER` | `pank` | SSH user. |
| `DEPLOY_COMPOSE_PATH` | `/home/pank/homeserver/docker-compose/print-portal` | Compose project directory on that host. |
| `DEPLOY_SSH_PRIVATE_KEY_BASE64` | *(none)* | Private key, base64-encoded (`base64 -w0 key`). |
| `SSH_PRIVATE_KEY` | *(none)* | Raw private key; used when `DEPLOY_SSH_PRIVATE_KEY_BASE64` is unset. |
| `DEPLOY_KNOWN_HOSTS_BASE64` | *(none)* | The host's `known_hosts` line, base64-encoded; when unset, `ssh-keyscan -H` fills it in. |

Either credential style works: `DEPLOY_SSH_PRIVATE_KEY_BASE64` (preferred) or
`SSH_PRIVATE_KEY`. `DEPLOY_KNOWN_HOSTS_BASE64` pins the host key; when it is
unset the job falls back to `ssh-keyscan -H $DEPLOY_HOST`. Every default above
can be overridden.

### The systemd unit owns the compose project

CI never mutates the compose project itself. It performs exactly what an operator
would by hand:

```bash
cd /home/pank/homeserver/docker-compose/print-portal
sudo docker compose pull
sudo systemctl restart docker-compose@print-portal
```

`docker-compose@.service` runs `docker compose up` in the **foreground** with
`Restart=always`. A direct `docker compose up -d --force-recreate` therefore kills
the foreground compose process, systemd treats the exit as a failure, runs
`compose down`, and removes the container that was just recreated — but the unit's
`Restart=always` + 10 s restart recreates the container, so the real cost is a
double recreate / ~10 s outage, not a permanently dead portal. Pull plus unit
restart is the only sequence that replaces the container while systemd keeps
tracking it. Use `systemctl restart`, **never** `systemctl reload` (the unit's
own ExecReload is the unsafe `pull && up -d --force-recreate` form).

Two assumptions about that host, both required:

1. The compose project is managed by a `docker-compose@print-portal` systemd
   unit, so the directory is stable and CI only ever pulls and restarts.
2. Its compose file references the registry image (`image:`), never a
   `build:` block — with both `image:` and `build:` set compose still pulls the
   image and builds on `up`; the actionable rule is to keep only `image:` and
   never copy the repo's `build: .` flavor to the host.

Overriding `PRIMARY_IMAGE` therefore also means editing the host compose file's
`image:` to the same reference. The deploy asserts on the digest of the image CI
pushed (`PRIMARY_IMAGE`, i.e. `CI_REGISTRY_IMAGE` unless overridden), so a
compose file that still names a different repository leaves the old image in
place and fails the pipeline. That
failure is the point: it fails closed, and a green deploy always means the host
serves the exact build this pipeline pushed.

### What the deploy asserts

After the restart, on the host, the job fails unless all of these hold:

1. `docker compose pull` succeeded — a failed pull leaves the running portal
   serving.
2. The `print-portal` container reports `healthy` within 90 polls × 2 s (180 s);
   a container that never gets healthy fails the deploy.
3. The `:latest` the host pulled is this build:
   `sudo docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' '<repo>:latest' | grep -F '<repo>@' | head -n1`
   plus `.Id` must contain `PORTAL_IMAGE_DIGEST`.
4. The running container is that exact image:
   `sudo docker inspect -f '{{.Config.Image}} {{.Image}} {{.State.Status}}'
   print-portal` must show `<repo>:latest`, the same image ID, and `running`.

A failed deploy leaves whatever container compose last started in place —
recover with `sudo systemctl restart docker-compose@print-portal` in that
directory once the cause is fixed, or roll back by pinning the previous
`$FULL_VERSION` tag in its compose file.
