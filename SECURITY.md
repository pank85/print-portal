# Security policy

## Reporting a vulnerability

Please report security issues **privately**. Do not open a public issue.

- Open a private security advisory on the repository, or
- contact the maintainers through the private channel listed in the repository profile.

Include the version, the affected component, and a description of the impact. We will acknowledge
reports and work with you on a fix before any public disclosure.

## What you should know before exposing it

- **The Moonraker API key is a secret.** It is stored in `.env` and sent as the `X-Api-Key` header.
  Do not commit `.env`, and do not share the key. Anyone with the key can upload files to the
  printer and start prints.
- **The portal has no authentication of its own.** Anyone who can reach the port can upload a model
  and start a print. Always put the container behind a reverse proxy with authentication, or a VPN.
  See [docs/deployment.md](docs/deployment.md).
- **Moonraker's `trusted_clients` is not a substitute.** It controls which clients may reach
  Moonraker without a key; it does not protect the portal's port, and it does not authenticate
  portal users.
- **The portal trusts its network.** There is no per-user authorization, no rate limiting and no
  audit log. Treat the port as a privileged control surface.
