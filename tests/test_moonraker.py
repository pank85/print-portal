"""Offline tests for app/moonraker.py::MoonrakerClient."""
from __future__ import annotations

import io
import socket
import urllib.error
import urllib.request
from pathlib import Path

import moonraker


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url: str, code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "msg", {}, io.BytesIO(body))


def _patch_urlopen(monkeypatch, handler):
    monkeypatch.setattr(urllib.request, "urlopen", handler)


def test_base_url_trailing_slash() -> None:
    c = moonraker.MoonrakerClient("http://x:7125/", "secret")
    assert c.base_url == "http://x:7125"


def test_server_info_url(monkeypatch) -> None:
    captured = {}

    def handler(req, timeout=None):
        captured["req"] = req
        return FakeResponse(b'{"result": {"klippy_state": "ready"}}')

    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    assert c.server_info() == {"klippy_state": "ready"}
    req = captured["req"]
    assert req.get_method() == "GET"
    assert req.get_full_url() == "http://x:7125/server/info"


def test_query_objects_exact_url(monkeypatch) -> None:
    captured = {}

    def handler(req, timeout=None):
        captured["req"] = req
        return FakeResponse(b'{"result": {"status": {"print_stats": {"state": "standby"}}}}')

    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    status = c.query_objects()
    req = captured["req"]
    assert req.get_method() == "GET"
    assert req.get_full_url() == (
        "http://x:7125/printer/objects/query?print_stats&webhooks&virtual_sdcard"
        "&extruder&heater_bed&toolhead"
    )
    assert status == {"print_stats": {"state": "standby"}}


def test_query_objects_custom_list(monkeypatch) -> None:
    captured = {}

    def handler(req, timeout=None):
        captured["req"] = req
        return FakeResponse(b'{"result": {"status": {}}}')

    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    c.query_objects(("print_stats", "webhooks"))
    assert captured["req"].get_full_url() == (
        "http://x:7125/printer/objects/query?print_stats&webhooks"
    )


def test_x_api_key_header_present(monkeypatch) -> None:
    captured = {}

    def handler(req, timeout=None):
        captured["req"] = req
        return FakeResponse(b'{"result": {}}')

    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret-key")
    c.server_info()
    req = captured["req"]
    # urllib lowercases header name internally
    key = None
    for k, v in req.headers.items():
        if k.lower() == "x-api-key":
            key = v
    assert key == "secret-key"


def test_api_key_not_in_repr_str_exceptions(monkeypatch) -> None:
    c = moonraker.MoonrakerClient("http://x:7125", "super-secret")
    assert "super-secret" not in repr(c)
    assert "super-secret" not in str(c)
    # trigger an unreachable error
    def handler(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    _patch_urlopen(monkeypatch, handler)
    try:
        c.server_info()
    except moonraker.MoonrakerUnreachable as exc:
        assert "super-secret" not in str(exc)
    else:
        raise AssertionError("expected unreachable")


def test_upload_multipart(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    def handler(req, timeout=None):
        captured["req"] = req
        captured["body"] = req.data
        return FakeResponse(b'{"result": {"ok": true}}')

    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret", root="gcodes")
    f = tmp_path / "out.gcode"
    f.write_bytes(b"G1 X0\nG1 Y1\n")
    res = c.upload(f, remote_name="remote.gcode")
    assert res == {"ok": True}

    req = captured["req"]
    assert req.get_method() == "POST"
    assert req.get_full_url() == "http://x:7125/server/files/upload"
    ctype = None
    for k, v in req.headers.items():
        if k.lower() == "content-type":
            ctype = v
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=", 1)[1]
    body = captured["body"]
    assert body is not None
    assert (f"--{boundary}".encode() in body)
    assert b'name="root"' in body
    assert b"gcodes" in body
    assert b'name="file"' in body
    assert b'filename="remote.gcode"' in body
    assert b"application/octet-stream" in body
    assert b"G1 X0\nG1 Y1\n" in body


def test_start_print_url(monkeypatch) -> None:
    captured = {}

    def handler(req, timeout=None):
        captured["req"] = req
        return FakeResponse(b'{"result": {"result": "ok"}}')

    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    c.start_print("my file.gcode")
    req = captured["req"]
    assert req.get_method() == "POST"
    assert req.get_full_url() == (
        "http://x:7125/printer/print/start?filename=my%20file.gcode"
    )


def test_envelope_unwrapping(monkeypatch) -> None:
    def make(body):
        def handler(req, timeout=None):
            return FakeResponse(body.encode())
        return handler

    c = moonraker.MoonrakerClient("http://x:7125", "secret")

    # dict result
    _patch_urlopen(monkeypatch, make('{"result": {"a": 1}}'))
    assert c.server_info() == {"a": 1}

    # list result
    _patch_urlopen(monkeypatch, make('{"result": [1, 2, 3]}'))
    assert c.server_info() == [1, 2, 3]

    # string result
    _patch_urlopen(monkeypatch, make('{"result": "ok"}'))
    assert c.server_info() == "ok"

    # no result key
    _patch_urlopen(monkeypatch, make('{"foo": "bar"}'))
    assert c.server_info() == {"foo": "bar"}


def test_401_auth_error(monkeypatch) -> None:
    url = "http://x:7125/server/info"
    def handler(req, timeout=None):
        raise _http_error(url, 401, b'{"error": {"code": 401, "message": "Unauthorized"}}')
    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    try:
        c.server_info()
    except moonraker.MoonrakerAuthError as exc:
        assert "Moonraker rejected the API key" in str(exc)
        assert exc.http_status == 502
        assert exc.http_code == 401
    else:
        raise AssertionError("expected auth error")


def test_403_auth_error(monkeypatch) -> None:
    url = "http://x:7125/server/info"
    def handler(req, timeout=None):
        raise _http_error(url, 403, b'{"error": {"code": 403, "message": "Forbidden"}}')
    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    try:
        c.server_info()
    except moonraker.MoonrakerAuthError as exc:
        assert "denied access" in str(exc).lower()
        assert exc.http_status == 502
    else:
        raise AssertionError("expected auth error")


def test_404_start_print_file_missing(monkeypatch) -> None:
    url = "http://x:7125/printer/print/start"
    def handler(req, timeout=None):
        raise _http_error(url, 404, b'{"error": {"code": 404, "message": "Not found"}}')
    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    try:
        c.start_print("missing.gcode")
    except moonraker.MoonrakerFileMissing as exc:
        assert "did not land in the 'gcodes' root" in str(exc)
        assert exc.http_status == 502
    else:
        raise AssertionError("expected file missing")


def test_404_elsewhere_moonraker_error(monkeypatch) -> None:
    url = "http://x:7125/server/info"
    def handler(req, timeout=None):
        raise _http_error(url, 404, b'{"error": {"code": 404, "message": "Not found"}}')
    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    try:
        c.server_info()
    except moonraker.MoonrakerError as exc:
        assert not isinstance(exc, (moonraker.MoonrakerFileMissing, moonraker.MoonrakerAuthError, moonraker.MoonrakerBusy, moonraker.MoonrakerUnreachable))
        assert "404" in str(exc)
        assert exc.http_status == 502
    else:
        raise AssertionError("expected error")


def test_409_busy(monkeypatch) -> None:
    url = "http://x:7125/server/info"
    def handler(req, timeout=None):
        raise _http_error(url, 409, b'{"error": {"code": 409, "message": "Conflict"}}')
    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    try:
        c.server_info()
    except moonraker.MoonrakerBusy as exc:
        assert "409" in str(exc)
        assert exc.http_status == 409
    else:
        raise AssertionError("expected busy")


def test_unreachable_connection_refused(monkeypatch) -> None:
    def handler(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    try:
        c.server_info()
    except moonraker.MoonrakerUnreachable as exc:
        assert "printer unreachable" in str(exc).lower() or "unreachable" in str(exc).lower()
        assert exc.http_status == 503
    else:
        raise AssertionError("expected unreachable")


def test_unreachable_socket_timeout(monkeypatch) -> None:
    def handler(req, timeout=None):
        raise socket.timeout("timed out")
    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    try:
        c.server_info()
    except moonraker.MoonrakerUnreachable as exc:
        assert exc.http_status == 503
    else:
        raise AssertionError("expected unreachable")


def test_unreachable_dns_failure(monkeypatch) -> None:
    def handler(req, timeout=None):
        raise urllib.error.URLError(OSError("Name or service not known"))
    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    try:
        c.server_info()
    except moonraker.MoonrakerUnreachable as exc:
        assert exc.http_status == 503
    else:
        raise AssertionError("expected unreachable")


def test_invalid_json(monkeypatch) -> None:
    def handler(req, timeout=None):
        return FakeResponse(b"not json {")
    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    try:
        c.server_info()
    except moonraker.MoonrakerError as exc:
        assert "invalid JSON" in str(exc)
        assert exc.http_status == 502
    else:
        raise AssertionError("expected error")


def test_non2xx_with_error_object_message_preserved(monkeypatch) -> None:
    url = "http://x:7125/server/info"
    def handler(req, timeout=None):
        raise _http_error(url, 500, b'{"error": {"code": 500, "message": "boom happened"}}')
    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    try:
        c.server_info()
    except moonraker.MoonrakerError as exc:
        assert "boom happened" in str(exc)
    else:
        raise AssertionError("expected error")


def test_2xx_payload_with_error_raises(monkeypatch) -> None:
    def handler(req, timeout=None):
        return FakeResponse(b'{"error": {"code": 1, "message": "bad thing"}}')
    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    try:
        c.server_info()
    except moonraker.MoonrakerError as exc:
        assert "bad thing" in str(exc)
    else:
        raise AssertionError("expected error")


def test_printer_status_mapping(monkeypatch) -> None:
    status = {
        "print_stats": {"state": "printing", "print_duration": 120.0, "message": "hi"},
        "webhooks": {"state": "ready"},
        "virtual_sdcard": {"progress": 0.5, "file_path": "/gcodes/cube.gcode"},
        "extruder": {"temperature": 210.44, "target": 210.0},
        "heater_bed": {"temperature": 60.04, "target": 60.0},
        "toolhead": {"homed_axes": "xyz"},
    }

    def handler(req, timeout=None):
        url = req.get_full_url()
        if url.endswith("/server/info"):
            return FakeResponse(b'{"result": {"klippy_state": "ready"}}')
        return FakeResponse(b'{"result": {"status": ' + __import__("json").dumps(status).encode() + b"}}")

    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    info = c.printer_status()
    assert info["ok"] is True
    assert info["state"] == "printing"
    assert info["webhooks"] == "ready"
    assert info["file"] == "cube.gcode"
    assert info["progress"] == 50.0
    assert info["duration_min"] == 2.0
    assert info["extruder"] == {"temp": 210.4, "target": 210.0}
    assert info["bed"] == {"temp": 60.0, "target": 60.0}
    assert info["homed"] == "xyz"
    assert info["mcu_version"] is None
    assert info["message"] == "hi"


def test_printer_state_offline(monkeypatch) -> None:
    def handler(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    _patch_urlopen(monkeypatch, handler)
    c = moonraker.MoonrakerClient("http://x:7125", "secret")
    out = c.printer_state()
    assert out == {"ok": False, "state": "offline", "error": out["error"]}
    assert "unreachable" in out["error"].lower() or "refused" in out["error"].lower()