"""printers.py: Fernet key lifecycle, validation, masking, printers.json CRUD.

Everything runs against ``tmp_path``: no process-wide state, no app import, and the only
external dependency is ``cryptography`` (already required by the app).
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import printers

URL = "http://voron.lan:7125"
KEY = "0123456789abcdef"
MACHINE_REL = "system/Custom/machine/generic-klipper-0.4.json"
PROCESS_REL = "system/Custom/process/generic-0.20-standard.json"
PAIRING = {"machine": MACHINE_REL, "process": PROCESS_REL}
SEED = printers.SeededPrinter(id=printers.ENV_PRINTER_ID, name="Environment",
                              url="http://env.lan:7125", api_key="env-secret-9999")


@pytest.fixture
def store(tmp_path: Path) -> printers.PrinterStore:
    """An empty store: ``secret.key`` is created, ``printers.json`` is not."""
    return printers.PrinterStore.open(tmp_path / "printers.json", tmp_path / "secret.key")


def add(store: printers.PrinterStore, name: str = "Voron 2.4", url: str = URL,
        api_key: str = KEY) -> printers.Printer:
    return store.add(name, url, api_key)


def stored_json(store: printers.PrinterStore) -> dict:
    return json.loads(store.path.read_text(encoding="utf-8"))


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def selected_ids(views: list[dict]) -> list[str]:
    """The ids ``views()`` renders as active; the UI's active chip reads this flag."""
    return [view["id"] for view in views if view["selected"]]


# ---------------------------------------------------------------- key file
def test_key_file_is_created_with_mode_0600(tmp_path: Path) -> None:
    key_path = tmp_path / "secret.key"
    assert not key_path.exists()

    key = printers.load_or_create_key(key_path)

    assert key_path.read_bytes() == key
    assert file_mode(key_path) == 0o600
    Fernet(key)  # a usable Fernet key


def test_key_file_is_reused_not_regenerated(tmp_path: Path) -> None:
    key_path = tmp_path / "secret.key"

    first = printers.load_or_create_key(key_path)
    second = printers.load_or_create_key(key_path)

    assert first == second
    assert key_path.read_bytes() == first


def test_key_file_race_does_not_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two processes on first start: the loser reads the winner's key instead of replacing it."""
    key_path = tmp_path / "secret.key"
    winner = Fernet.generate_key()
    real_open = os.open

    def racing_open(name, flags, mode=0o777):
        if Path(name) == key_path:
            key_path.write_bytes(winner)
            os.chmod(key_path, 0o600)
            raise FileExistsError(name)
        return real_open(name, flags, mode)

    monkeypatch.setattr(os, "open", racing_open)

    key = printers.load_or_create_key(key_path)

    assert key == winner
    assert key_path.read_bytes() == winner
    assert file_mode(key_path) == 0o600


# ---------------------------------------------------------------- crypto / masking
def test_encrypt_decrypt_round_trip() -> None:
    key = Fernet.generate_key()

    token = printers.encrypt_secret(KEY, key)

    assert token != KEY
    assert KEY not in token
    assert printers.decrypt_secret(token, key) == KEY


def test_decrypt_with_the_wrong_key_raises_store_error(store: printers.PrinterStore) -> None:
    printer = add(store)
    other_key = Fernet.generate_key()

    with pytest.raises(printers.PrinterStoreError, match="cannot be decrypted"):
        printers.decrypt_secret(printer.api_key_enc, other_key)

    # a store opened with a foreign key cannot print, but still renders masked rows
    foreign = printers.PrinterStore(store.path, other_key)
    assert foreign.all()[0].id == printer.id
    with pytest.raises(printers.PrinterStoreError, match="cannot be decrypted"):
        foreign.api_key(foreign.all()[0])
    assert foreign.views()[0]["api_key_masked"] == printers.MASK


@pytest.mark.parametrize("key", [None, "", "short", KEY])
def test_mask_key_never_returns_the_secret(key: str | None) -> None:
    masked = printers.mask_key(key)

    assert masked
    assert masked not in (key or "")
    assert masked != key


def test_mask_key_keeps_only_the_last_four_characters() -> None:
    assert printers.mask_key(KEY) == printers.MASK + "cdef"
    assert len(printers.mask_key(KEY)) == len(printers.MASK) + 4
    assert printers.mask_key("123456789") == printers.MASK + "6789"
    assert printers.mask_key("12345678") == printers.MASK
    assert printers.mask_key("") == printers.MASK
    assert printers.mask_key(None) == printers.MASK


# ---------------------------------------------------------------- reading and writing
def test_store_reads_a_missing_file_as_empty_without_writing(
        store: printers.PrinterStore) -> None:
    assert store.corrupt is None
    assert store.all() == []
    assert store.selected_id() is None
    assert store.views() == []
    assert store.active() is None
    assert not store.path.exists()


def test_add_printer_persists_a_versioned_file_with_encrypted_key(
        store: printers.PrinterStore) -> None:
    printer = add(store)

    raw = store.path.read_text(encoding="utf-8")
    data = stored_json(store)

    assert KEY not in raw
    assert data["version"] == printers.PRINTERS_VERSION == 2
    assert data["selected_id"] == printer.id
    assert len(data["printers"]) == 1
    entry = data["printers"][0]
    assert set(entry) == {"id", "name", "url", "api_key_enc", "created", "updated",
                          "profiles"}
    assert entry["profiles"] is None
    assert printers.PRINTER_ID_RE.fullmatch(entry["id"])
    assert entry["id"] == printer.id
    assert entry["name"] == "Voron 2.4"
    assert entry["url"] == URL
    assert entry["api_key_enc"] != KEY
    assert entry["created"] == entry["updated"]

    key = printers.load_or_create_key(store.path.with_name("secret.key"))
    assert printers.decrypt_secret(entry["api_key_enc"], key) == KEY
    assert store.api_key(printer) == KEY


def test_printers_file_has_no_temp_leftover_and_mode_0600(store: printers.PrinterStore) -> None:
    add(store)

    assert list(store.path.parent.glob("*.tmp")) == []
    assert file_mode(store.path) == 0o600


@pytest.mark.parametrize("name, url, api_key, message", [
    ("", URL, KEY, "name must be 1-60 characters"),
    ("   ", URL, KEY, "name must be 1-60 characters"),
    ("x" * 61, URL, KEY, "name must be 1-60 characters"),
    ("bad\x00name", URL, KEY, "name must be 1-60 characters"),
    ("Voron", "", KEY, "url is required"),
    ("Voron", "voron.lan:7125", KEY, "url must start with http:// or https://"),
    ("Voron", "ftp://voron.lan", KEY, "url must start with http:// or https://"),
    ("Voron", "http://user:pass@voron.lan", KEY, "url must not contain credentials"),
    ("Voron", "http://voron.lan:7125/?x=1", KEY, "url must not have a query or fragment"),
    ("Voron", "http://voron.lan:7125/#top", KEY, "url must not have a query or fragment"),
    ("Voron", "http://voron.lan:7125/a b", KEY, "url must not contain whitespace"),
    ("Voron", "http://voron.lan:7125/" + "a" * 300, KEY, "url must be at most 300 characters"),
    ("Voron", URL, "", "api_key is required"),
    ("Voron", URL, "  ", "api_key is required"),
    ("Voron", URL, "a\nb", "api_key must not contain control characters"),
    ("Voron", URL, "k" * 257, "api_key must be at most 256 characters"),
])
def test_add_printer_validates_name_url_and_api_key(
        store: printers.PrinterStore, name: object, url: object, api_key: object,
        message: str) -> None:
    with pytest.raises(printers.PrinterError, match=message):
        store.add(name, url, api_key)

    assert not store.path.exists()


def test_url_normalisation_strips_a_trailing_slash(store: printers.PrinterStore) -> None:
    printer = add(store, url="http://voron.lan:7125/")

    assert printer.url == "http://voron.lan:7125"
    assert printers.normalise_url(" https://host.lan/moonraker/ ") == "https://host.lan/moonraker"
    assert printers.normalise_name("  Voron   2.4  ") == "Voron 2.4"


@pytest.mark.parametrize("url, message", [
    ("http://user:pass@voron.lan:7125", "url must not contain credentials"),
    ("http://voron.lan:7125?api_key=x", "url must not have a query or fragment"),
    ("http://voron.lan:7125#top", "url must not have a query or fragment"),
])
def test_url_rejects_credentials_query_and_fragment(url: str, message: str) -> None:
    with pytest.raises(printers.PrinterError, match=message):
        printers.normalise_url(url)


# ---------------------------------------------------------------- selection
def test_first_printer_becomes_selected(store: printers.PrinterStore) -> None:
    first = add(store)
    second = add(store, name="Core One", url="http://core.lan:7125")

    assert store.selected_id() == first.id
    assert [printer.id for printer in store.all()] == [first.id, second.id]
    assert stored_json(store)["selected_id"] == first.id


def test_delete_printer_reselects_the_first_remaining(store: printers.PrinterStore) -> None:
    first = add(store)
    add(store, name="Core One", url="http://core.lan:7125")
    last = add(store, name="RatRig", url="http://ratrig.lan:7125")
    store.select(last.id)

    new_selection = store.delete(last.id)

    assert new_selection == first.id
    assert store.selected_id() == first.id
    assert store.delete(first.id) == store.all()[0].id
    assert store.selected_id() == store.all()[0].id


def test_delete_unknown_printer_is_not_found(store: printers.PrinterStore) -> None:
    with pytest.raises(printers.PrinterNotFound):
        store.delete("ffffffffffff")
    with pytest.raises(printers.PrinterNotFound):
        store.get("ffffffffffff")

    assert not store.path.exists()


def test_env_id_is_read_only(store: printers.PrinterStore) -> None:
    printer = add(store)
    before = store.path.read_text(encoding="utf-8")

    with pytest.raises(printers.PrinterReadOnly):
        store.update(printers.ENV_PRINTER_ID, name="Edited")
    with pytest.raises(printers.PrinterReadOnly):
        store.delete(printers.ENV_PRINTER_ID)

    assert [item.id for item in store.all()] == [printer.id]
    assert store.path.read_text(encoding="utf-8") == before


def test_select_none_clears_the_selection(store: printers.PrinterStore) -> None:
    add(store)

    store.select(None)

    assert store.selected_id() is None
    assert stored_json(store)["selected_id"] is None

    with pytest.raises(printers.PrinterNotFound):
        store.select("ffffffffffff")

    store.select(printers.ENV_PRINTER_ID)
    assert store.selected_id() == printers.ENV_PRINTER_ID


# ---------------------------------------------------------------- update
def test_update_printer_keeps_the_key_when_omitted(store: printers.PrinterStore) -> None:
    printer = add(store)

    updated = store.update(printer.id, name="  Voron   2.4  XL ")

    assert updated.id == printer.id
    assert updated.name == "Voron 2.4 XL"
    assert updated.url == printer.url
    assert updated.api_key_enc == printer.api_key_enc
    assert updated.created == printer.created
    assert store.api_key(updated) == KEY
    assert stored_json(store)["printers"][0]["api_key_enc"] == printer.api_key_enc


def test_update_printer_reencrypts_a_new_key(store: printers.PrinterStore) -> None:
    printer = add(store)

    updated = store.update(printer.id, url="https://voron.lan/moonraker", api_key="new-secret-42")

    assert updated.url == "https://voron.lan/moonraker"
    assert updated.api_key_enc != printer.api_key_enc
    assert store.api_key(updated) == "new-secret-42"
    assert stored_json(store)["printers"][0]["api_key_enc"] == updated.api_key_enc
    assert KEY not in store.path.read_text(encoding="utf-8")


def test_update_printer_rejects_an_empty_body(store: printers.PrinterStore) -> None:
    printer = add(store)

    with pytest.raises(printers.PrinterError, match="nothing to update"):
        store.update(printer.id)

    assert store.get(printer.id) == printer


# ---------------------------------------------------------------- views / active
def test_views_mask_keys_and_hide_the_token(store: printers.PrinterStore) -> None:
    printer = add(store)

    views = store.views(SEED)

    assert len(views) == 2
    stored, env = views
    assert set(stored) == {"id", "name", "url", "api_key_masked", "has_api_key", "selected",
                           "source", "editable", "pairing_editable", "profiles", "created",
                           "updated"}
    assert stored["id"] == printer.id
    assert stored["api_key_masked"] == printers.MASK + "cdef"
    assert stored["has_api_key"] is True
    assert stored["selected"] is True
    assert stored["source"] == "store"
    assert stored["editable"] is True
    assert stored["pairing_editable"] is True
    assert stored["created"] == printer.created

    assert set(env) == set(stored)
    assert env["id"] == printers.ENV_PRINTER_ID
    assert env["source"] == "env"
    assert env["editable"] is False
    assert env["created"] is None
    assert env["updated"] is None
    assert env["selected"] is False
    assert env["api_key_masked"] == printers.MASK + "9999"

    dumped = json.dumps(views)
    assert "api_key_enc" not in dumped
    assert KEY not in dumped
    assert SEED.api_key not in dumped


def test_active_prefers_the_selected_then_the_seed(store: printers.PrinterStore) -> None:
    assert store.active() is None
    assert store.active(SEED) == printers.PrintTarget(
        id=printers.ENV_PRINTER_ID, name=SEED.name, url=SEED.url, api_key=SEED.api_key,
        source="env")

    first = add(store)
    second = add(store, name="Core One", url="http://core.lan:7125")

    target = store.active(SEED)
    assert (target.id, target.source, target.api_key) == (first.id, "store", KEY)

    store.select(second.id)
    assert store.active(SEED).id == second.id

    store.select(printers.ENV_PRINTER_ID)
    assert store.active(SEED).source == "env"
    assert store.active().id == first.id      # no seed: fall back to the first stored printer

    store.select(None)
    assert store.active(SEED).id == first.id


def test_views_mark_the_env_seed_when_the_selection_is_unset(store: printers.PrinterStore) -> None:
    assert store.selected_id() is None

    assert store.active(SEED).id == printers.ENV_PRINTER_ID
    assert selected_ids(store.views(SEED)) == [printers.ENV_PRINTER_ID]


def test_views_mark_the_first_printer_when_the_selection_is_unset(
        store: printers.PrinterStore) -> None:
    first = add(store)
    add(store, name="Core One", url="http://core.lan:7125")
    store.select(None)

    assert store.active(SEED).id == first.id
    assert selected_ids(store.views(SEED)) == [first.id]


def test_views_mark_the_first_printer_when_the_selection_is_stale(
        store: printers.PrinterStore) -> None:
    """A selected id whose printer is gone must still leave exactly one active row."""
    first = add(store)
    add(store, name="Core One", url="http://core.lan:7125")
    data = stored_json(store)
    data["selected_id"] = "ffffffffffff"
    store.path.write_text(json.dumps(data), encoding="utf-8")
    store.reload()

    assert store.active().id == first.id
    assert selected_ids(store.views()) == [first.id]
    assert store.active(SEED).id == first.id
    assert selected_ids(store.views(SEED)) == [first.id]


def test_select_env_without_a_seed_leaves_views_and_active_in_step(
        store: printers.PrinterStore) -> None:
    first = add(store)
    add(store, name="Core One", url="http://core.lan:7125")

    store.select(printers.ENV_PRINTER_ID)      # persisted, but this instance has no env seed

    assert store.active().id == first.id
    assert selected_ids(store.views()) == [first.id]


# ---------------------------------------------------------------- corrupt / newer files
def test_corrupt_file_is_reported_and_writes_are_refused(store: printers.PrinterStore) -> None:
    printer = add(store)
    good = store.path.read_text(encoding="utf-8")
    store.path.write_text("{ not json", encoding="utf-8")

    assert store.reload() is store
    assert store.corrupt is not None
    assert "not valid JSON" in store.corrupt
    assert store.all() == []
    assert store.views(SEED) == []
    assert store.selected_id() is None

    with pytest.raises(printers.PrinterStoreError, match="not valid JSON"):
        store.add("Voron", URL, KEY)
    with pytest.raises(printers.PrinterStoreError):
        store.select(None)
    with pytest.raises(printers.PrinterStoreError):
        store.delete(printer.id)

    assert store.path.read_text(encoding="utf-8") == "{ not json"
    assert store.active(SEED).source == "env"      # the app still boots and can use the seed

    store.path.write_text(good, encoding="utf-8")
    store.reload()
    assert store.corrupt is None
    assert [item.id for item in store.all()] == [printer.id]


def test_newer_version_is_a_store_error(store: printers.PrinterStore) -> None:
    newer = {"version": printers.PRINTERS_VERSION + 1, "selected_id": None, "printers": []}
    store.path.write_text(json.dumps(newer), encoding="utf-8")

    store.reload()

    assert store.corrupt is not None
    assert "newer than this build" in store.corrupt
    assert store.all() == []

    with pytest.raises(printers.PrinterStoreError, match="newer than this build"):
        store.add("Voron", URL, KEY)

    assert stored_json(store)["version"] == printers.PRINTERS_VERSION + 1


# ---------------------------------------------------------------- pairing
def stored_entry(store: printers.PrinterStore, printer_id: str = "a1b2c3d4e5f6",
                 name: str = "Voron 2.4") -> dict:
    """One ``printers.json`` record exactly as a version 1 build wrote it (no pairing)."""
    key = printers.load_or_create_key(store.path.with_name("secret.key"))
    stamp = "2026-01-01T00:00:00+00:00"
    return {"id": printer_id, "name": name, "url": URL,
            "api_key_enc": printers.encrypt_secret(KEY, key),
            "created": stamp, "updated": stamp}


def test_printer_profiles_are_persisted_and_read_back(store: printers.PrinterStore) -> None:
    printer = store.add("Voron 2.4", URL, KEY, profiles=PAIRING)

    assert printer.profiles == PAIRING
    assert stored_json(store)["printers"][0]["profiles"] == PAIRING

    reloaded = printers.PrinterStore.open(store.path, store.path.with_name("secret.key"))

    assert reloaded.get(printer.id).profiles == PAIRING
    assert reloaded.views(SEED)[0]["profiles"] == PAIRING


def test_printer_profiles_default_to_none_for_version_1_documents(
        store: printers.PrinterStore) -> None:
    entry = stored_entry(store)
    store.path.write_text(json.dumps({"version": 1, "selected_id": entry["id"],
                                      "printers": [entry]}), encoding="utf-8")

    store.reload()

    assert store.corrupt is None
    assert store.get(entry["id"]).profiles is None
    active = store.active()
    assert active is not None and active.id == entry["id"] and active.api_key == KEY
    assert stored_json(store)["version"] == 1        # reading never migrates the file


def test_version_1_document_is_rewritten_as_version_2_on_next_write(
        store: printers.PrinterStore) -> None:
    first = stored_entry(store, "a1b2c3d4e5f6", "Voron 2.4")
    second = stored_entry(store, "0f1e2d3c4b5a", "Trident")
    store.path.write_text(json.dumps({"version": 1, "selected_id": second["id"],
                                      "printers": [first, second]}), encoding="utf-8")
    store.reload()

    store.update(first["id"], name="Voron 2.4 XL")

    data = stored_json(store)
    assert data["version"] == printers.PRINTERS_VERSION == 2
    assert [entry["id"] for entry in data["printers"]] == [first["id"], second["id"]]
    assert data["selected_id"] == second["id"]
    assert [entry["profiles"] for entry in data["printers"]] == [None, None]
    assert store.selected_id() == second["id"]

    store.add("Voron Zero", URL, KEY)

    data = stored_json(store)
    assert data["version"] == printers.PRINTERS_VERSION
    assert [entry["id"] for entry in data["printers"]][:2] == [first["id"], second["id"]]
    assert data["selected_id"] == second["id"]
    assert data["printers"][-1]["profiles"] is None


@pytest.mark.parametrize("profiles, message", [
    ({"machine": "../x.json"}, "profile path must be relative to the datadir"),
    ({"machine": "/abs/x.json"}, "profile path must be relative to the datadir"),
    ({"machine": "notjson"}, "profile path must be relative to the datadir"),
    ({"machine": 3}, "profiles must be an object with machine/process paths"),
    ({"slicer": "x.json"}, "unknown profile slot: 'slicer'"),
])
def test_printer_profiles_reject_unsafe_or_unknown_paths(
        store: printers.PrinterStore, profiles: object, message: str) -> None:
    with pytest.raises(printers.PrinterError, match=message):
        store.add("Voron 2.4", URL, KEY, profiles=profiles)
    assert store.all() == []                         # a rejected pairing writes nothing

    printer = add(store)

    with pytest.raises(printers.PrinterError, match=message):
        store.update(printer.id, profiles=profiles)
    assert store.get(printer.id) == printer

    document = {"version": printers.PRINTERS_VERSION, "selected_id": printer.id,
                "printers": [dict(stored_entry(store, printer.id), profiles=profiles)]}
    store.path.write_text(json.dumps(document), encoding="utf-8")
    store.reload()

    assert store.corrupt is not None
    assert message in store.corrupt


def test_update_can_set_and_clear_a_pairing(store: printers.PrinterStore) -> None:
    printer = add(store)
    assert printer.profiles is None

    paired = store.update(printer.id, profiles=PAIRING)
    assert paired.profiles == PAIRING
    assert stored_json(store)["printers"][0]["profiles"] == PAIRING

    kept = store.update(printer.id, name="Voron 2.4 XL")      # not passed: unchanged
    assert kept.profiles == PAIRING

    partial = store.update(printer.id, profiles={"machine": MACHINE_REL})
    assert partial.profiles == {"machine": MACHINE_REL}       # the dict replaces the pairing

    cleared = store.update(printer.id, profiles=None)
    assert cleared.profiles is None
    assert stored_json(store)["printers"][0]["profiles"] is None

    store.update(printer.id, profiles=PAIRING)
    all_none = store.update(printer.id, profiles={"machine": None, "process": None})
    assert all_none.profiles is None

    store.update(printer.id, profiles=PAIRING)
    emptied = store.update(printer.id, profiles={})
    assert emptied.profiles is None


def test_view_exposes_the_pairing(store: printers.PrinterStore) -> None:
    printer = add(store)

    stored_view, env_view = store.views(SEED)

    assert stored_view["profiles"] == {"machine": None, "process": None}
    assert env_view["profiles"] == {"machine": None, "process": None}
    assert stored_view["pairing_editable"] is True
    assert env_view["pairing_editable"] is True      # the env pairing is a preference
    assert env_view["editable"] is False             # its credentials are not

    store.update(printer.id, profiles=PAIRING)
    stored_view, env_view = store.views(SEED)

    assert stored_view["profiles"] == PAIRING
    assert env_view["profiles"] == {"machine": None, "process": None}
    assert set(stored_view) == set(env_view)         # one shape for both sources

    with pytest.raises(printers.PrinterReadOnly):
        store.update(printers.ENV_PRINTER_ID, profiles=PAIRING)


def test_env_pairing_is_stored_outside_the_printer_records(
        store: printers.PrinterStore) -> None:
    """The env seed has no record of its own: its pairing lives in ``env_profiles``."""
    assert store.set_env_pairing(PAIRING) == {"machine": MACHINE_REL,
                                              "process": PROCESS_REL}

    data = stored_json(store)
    assert data["env_profiles"] == PAIRING
    assert data["printers"] == []
    assert store.views(SEED)[-1]["profiles"] == PAIRING

    reloaded = printers.PrinterStore.open(store.path)
    assert reloaded.views(SEED)[-1]["profiles"] == PAIRING

    assert store.set_env_pairing({"machine": None, "process": None}) == {
        "machine": None, "process": None}
    assert stored_json(store)["env_profiles"] is None

    with pytest.raises(printers.PrinterError, match=printers.PROFILE_PATH_ERROR):
        store.set_env_pairing({"machine": "../outside.json"})
    assert stored_json(store)["env_profiles"] is None   # a rejected pairing writes nothing


def test_malformed_env_profiles_reads_as_no_pairing(store: printers.PrinterStore) -> None:
    """One unusable preference must not blank the store or block every write."""
    document = {"version": printers.PRINTERS_VERSION, "selected_id": None,
                "printers": [], "env_profiles": {"machine": 5}}
    store.path.write_text(json.dumps(document), encoding="utf-8")

    store.reload()

    assert store.corrupt is None
    assert store.views(SEED)[-1]["profiles"] == {"machine": None, "process": None}
    assert store.all() == []
