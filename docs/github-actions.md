# GitHub Actions migration

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) is a proposed replacement for the
existing GitLab pipeline, not an already completed cutover. It tests pushes and pull requests with
Python 3.12 and Node 22. On a push to `master` or a manual run on `master`, it builds an amd64 image
and pushes `pank85/print-portal` to Docker Hub with the eight-character commit SHA, version-SHA,
and `latest` tags. It obtains the version from `app/config.py`.

The current GitLab pipeline also pushes a private GitLab registry mirror. **The GitHub workflow does
not publish that mirror**; arrange a reachable, authenticated mirror publisher if it is still needed
before retiring GitLab. Image and container names intentionally remain unchanged for the existing
homeserver deployment.

## Prerequisites

In GitHub repository Settings → Secrets and variables → Actions, add repository secrets:

- `DOCKERHUB_USER` and `DOCKERHUB_TOKEN`: credentials able to push `pank85/print-portal`.
- `DEPLOY_SSH_PRIVATE_KEY_BASE64`: base64-encoded SSH private key for `pank@172.16.0.50`.
- `DEPLOY_KNOWN_HOSTS_BASE64`: base64-encoded, independently verified known-hosts entry for that host.

The deploy secrets are only needed when production deployment is enabled. Never put private keys in
the repository. Configure a **trusted, dedicated self-hosted GitHub Actions runner** with labels
`linux` and `slicedeck-deploy` and network access to `172.16.0.50`. The remote account must be able
to run the existing `sudo docker` and `sudo systemctl` commands non-interactively. GitHub-hosted
runners cannot reach the private LAN address. Restrict who can run workflows on this runner and protect
`master`; self-hosted runners should not execute untrusted pull-request code. The test and publish
jobs use GitHub-hosted runners, not the deployment runner.

Create a `production` GitHub environment and set any desired deployment protection rules. After the
runner, credentials and host key are in place, set repository variable `ENABLE_PRODUCTION_DEPLOY` to
`true`. Until then deploy is skipped; a successful publish alone does **not** mean production was
updated. GitHub concurrency serializes master runs, but does not coordinate with GitLab or external
publishers of `latest`.

## Safe cutover

1. Commit the workflow and README changes, then run the tests on GitHub. Confirm Docker Hub
   credentials and run the publication job without enabling production deployment.
2. Verify the image and decide how to replace the GitLab mirror if required. Install and secure the
   dedicated runner, add the deployment secrets and verify its network access and host key.
3. Stop the GitLab publisher/deployer before enabling the GitHub deploy: both pipelines publish the
   same `latest` tag and have no cross-system lock. Keep a rollback image tag available.
4. Set `ENABLE_PRODUCTION_DEPLOY=true` and run the workflow on `master`. Confirm the production job
   completes and independently check the printer service. If it fails, investigate and roll back;
   a failed deployment does not automatically restore the old container.

The deploy job pulls via the existing compose project and restarts its systemd unit, waits up to
180 seconds for container health, then checks the Docker Hub manifest digest and running image ID.
It deliberately does not run `docker compose up -d`: the systemd unit owns foreground compose.
See [deployment.md](deployment.md) for the current host layout and rollback details; its GitLab CI
section describes the legacy pipeline until cutover is complete.
