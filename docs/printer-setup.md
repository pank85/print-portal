# Printer setup (Moonraker)

print-portal talks to your printer only through Moonraker's HTTP API. This page covers the
Moonraker side: enabling file management, creating an API key, and making sure the container can
reach it.

## 1. Enable file management

Uploads go through Moonraker's file manager. Make sure it is enabled in `moonraker.conf`:

```ini
[file_manager]
enable_file_management: True
```

## 2. Create an API key

Moonraker's `[authorization]` section controls API access. Add it if it is missing:

```ini
[authorization]
trusted_clients:
    192.168.1.10
force_logins: False
```

Then generate a key. Depending on your Moonraker version, either:

- use the **API key** panel in the web UI (Mainsail/Fluidd), or
- run the Moonraker API-key helper that ships with your distribution and copy the printed key.

Put the key in `.env` as `MOONRAKER_API_KEY`. It is a secret — do not commit `.env`.

`trusted_clients` lists the source addresses Moonraker will accept without a key. It is a
convenience for browser access to the printer's own UI; it is **not** a substitute for the portal's
own access control, and it does not protect the portal's port. See [SECURITY.md](../SECURITY.md).

## UI-managed variant

Printers can also be managed entirely in the portal UI: open **Settings → Add printer** and paste
the Moonraker URL and API key there. In that mode `PRINTER_URL` and `MOONRAKER_API_KEY` are left
unset, and the portal boots unconfigured (printer routes return 503 until a printer is added).

## 3. CORS is not needed

The portal makes server-side calls to Moonraker from inside the container. The browser never talks
to Moonraker directly, so no CORS configuration is required.

## 4. Test reachability

From the machine running the container:

```bash
curl -H "X-Api-Key: <your-key>" http://printer.local:7125/server/info
```

A JSON response means Moonraker is reachable and the key is accepted. A `401` means the key is
wrong; a connection error means the URL or network path is wrong.

Replace `printer.local` with your printer's hostname or its LAN address (for example
`192.168.1.10:7125`).
