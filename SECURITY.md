# Security policy

## Reporting a vulnerability

Please report security issues **privately**. Do not open a public issue.

- Open a private security advisory on the repository, or
- contact the maintainers through the private channel listed in the repository profile.

Include the version, the affected component, and a description of the impact. We will acknowledge
reports and work with you on a fix before any public disclosure.

## What you should know before exposing it

- **The Moonraker API key is a secret.** There are two storage modes:
  - **Environment mode:** the key is stored in `.env` as `MOONRAKER_API_KEY` and sent as the
    `X-Api-Key` header.
  - **UI-managed mode:** printers and their keys are stored in `data/printers.json` as Fernet
    ciphertext, encrypted with `data/secret.key` (mode 0600).
  Do not commit `.env` or `secret.key`, and do not share keys. Anyone with a key can upload files
  to the printer and start prints.
- **The portal has no authentication of its own.** Anyone who can reach the port can upload a model
  and start a print. Always put the container behind a reverse proxy with authentication, or a VPN.
  See [docs/deployment.md](docs/deployment.md).
- **Moonraker's `trusted_clients` is not a substitute.** It controls which clients may reach
  Moonraker without a key; it does not protect the portal's port, and it does not authenticate
  portal users.
- **The portal trusts its network.** There is no per-user authorization, no rate limiting and no
  audit log. Treat the port as a privileged control surface.
- **Settings is unauthenticated.** In UI-managed mode any browser that can reach the portal can
  add, edit or remove printers and enter API keys (SSRF/credential-entry risk). Keep the portal
  behind an authenticated reverse proxy or VPN; do not expose it to untrusted networks.

## Secrets at rest

- `data/secret.key` (mode 0600) holds the Fernet key.
- `data/printers.json` stores API keys as ciphertext.
- Keys are masked in the UI/API; losing the key file means re-entering keys.

## Trust model

Unconfigured portal is reachable and printer routes return 503. Same trusted-network, no-portal-auth
model as before.
