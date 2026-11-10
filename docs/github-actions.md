# GitHub Actions: tests and Docker Hub publishing

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs tests on pushes and pull requests
using Python 3.12 and Node 22. On a push to `master`, or a manual workflow run on `master`, it
builds the amd64 image and publishes `pank85/print-portal` to Docker Hub after the tests pass.
The tags are the eight-character commit SHA, version-SHA (version from `app/config.py`), and
`latest`. The existing image name is retained for compatibility.

**GitHub Actions does not deploy to the printer host.** Pull and deploy the desired image manually.
A successful publish does not mean a running installation has been updated.

## Required secrets

In GitHub repository Settings → Secrets and variables → Actions, add:

- `DOCKERHUB_USER`: Docker Hub username with push access to `pank85/print-portal`.
- `DOCKERHUB_TOKEN`: Docker Hub access token with permission to push that repository.

Never commit the token. No deployment runner or SSH secrets are needed. Once the secrets are
configured, use **Actions → SliceDeck CI → Run workflow**, selecting `master`, or push to `master`.
Verify the publish job and its digest in the Actions log.

## Migration from GitLab

The legacy `.gitlab-ci.yml` remains in the repository and can still publish and automatically deploy
if its GitLab pipeline is active. Disable that pipeline in GitLab before treating GitHub as the sole
publisher; GitHub's concurrency control cannot coordinate with GitLab. GitHub Actions does **not**
publish the private GitLab registry mirror. Arrange a replacement if that mirror is still required.

For the homeserver, the existing compose project is owned by systemd: use `docker compose pull`
followed by `systemctl restart docker-compose@print-portal`, not `docker compose up -d`, which
conflicts with the foreground compose process. Check the image and container health after a manual
deploy. See [deployment.md](deployment.md) for the existing host layout, verification and rollback;
its GitLab CI sections describe the legacy pipeline.
