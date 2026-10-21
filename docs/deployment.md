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

Everything worth keeping lives in `./data`:

- `jobs/` — uploaded models, sliced gcode and job metadata
- `orca-datadir/` — the slicer datadir, including any presets you imported
- `profile.json` — the active machine/process/filament selection

Back up `./data` as a unit. Restoring it restores both your jobs and your imported presets.

## Upgrades and `SEED_VERSION`

On startup the entrypoint copies the image's seed presets into `$ORCA_DATADIR` with `cp -R -n`
(never overwriting), guarded by a `SEED_VERSION` marker file. This means:

- **new** seed files appear after an upgrade,
- your **edits and imports survive**,
- but **renamed or deleted** seed presets are **not removed** — the old file stays.

`SEED_VERSION` is a build-time value (`ENV` in the Dockerfile, overridable with `--build-arg`).
Bump it only when you want the copy step to run again (for example, after the seed content changed).
To remove a stale preset, delete it from `./data/orca-datadir` by hand.

Rebuild and recreate the container to pick up a new image:

```bash
docker compose up -d --build
```

## Publishing the image from CI

`.gitlab-ci.yml` runs the test suite on every branch and, on `master`, builds the
`linux/amd64` image and pushes it. Targets are configured with CI/CD variables
(**Settings > CI/CD > Variables**), not in the file, so the same pipeline works
for any fork or registry:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PRIMARY_IMAGE` | `$CI_REGISTRY_IMAGE` | Repository to push to (all three tags). |
| `MIRROR_IMAGE` | *(empty)* | Optional second repository; mirror push is skipped while unset. |
| `PRIMARY_REGISTRY_USER` / `PRIMARY_REGISTRY_PASSWORD` | job's `CI_REGISTRY_*` | Credentials for a primary target outside the GitLab registry. |
| `MIRROR_REGISTRY_USER` / `MIRROR_REGISTRY_PASSWORD` | job's `CI_REGISTRY_*` | Credentials for the mirror target. |

Each push uses three tags: `$CI_COMMIT_SHORT_SHA`, `$FULL_VERSION`
(`$APP_VERSION-$CI_COMMIT_SHORT_SHA`) and `latest`. If a registry host has no
credentials available the login step is skipped with a log line instead of
failing the job; the push only succeeds when the runner is already
authenticated to that host.

Example: publish to a public registry and keep a private mirror, with
`PRIMARY_IMAGE=registry.example.com/me/print-portal`,
`PRIMARY_REGISTRY_USER=me`, `PRIMARY_REGISTRY_PASSWORD=<token>` (masked),
`MIRROR_IMAGE=gitlab.example.com/group/print-portal` and
`MIRROR_REGISTRY_PASSWORD=<deploy-token>` (masked). Leave the mirror empty to
push to one registry only.

## Deploying from CI

The `deploy:production` job pushes the image this pipeline just built onto a
host and restarts that host's compose service. It is **off by default**: its
rule requires `DEPLOY_HOST` and `DEPLOY_SSH_PRIVATE_KEY_BASE64`, so a fork or a
repository without those variables never runs it — and never reaches anyone's
host.

| Variable | Meaning |
| --- | --- |
| `DEPLOY_HOST` | Host to deploy to, reachable over SSH from the runner. |
| `DEPLOY_USER` | SSH user. |
| `DEPLOY_SSH_PRIVATE_KEY_BASE64` | Private key, base64-encoded (`base64 -w0 key`). |
| `DEPLOY_KNOWN_HOSTS_BASE64` | The host's `known_hosts` line, base64-encoded. |
| `DEPLOY_COMPOSE_PATH` | Directory holding the compose project on that host. |

Two assumptions about that host, both required:

1. The compose file references the registry image (`image:`), never a `build:`
   block — `docker compose pull` is the only step that fetches a new image, and
   a local `build:` silently makes it a no-op.
2. The container sets `restart: unless-stopped` (or `always`), and the Docker
   daemon is enabled at boot. That is what brings it back after a reboot; no
   systemd unit is needed, and the deploy job never relies on one.

The job then checks what is actually serving, not just that the commands
exited 0: it reads the pulled image's `RepoDigests` and the running container's
`Config.Image`/`Image`, and requires both to match the digest this pipeline
pushed. A `:latest` that moved, or a container the restart did not recreate,
fails the deploy.
