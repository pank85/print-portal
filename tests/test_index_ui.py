"""Static DOM/handler assertions for the refactored dashboard profile card and printer form.

CI has no JS runtime, so these tests read the served page and assert on the ids, the option
strings and the listener wiring the browser would act on — the same technique as
``tests/test_profile_options.py`` and ``tests/test_api.py``.
"""
from __future__ import annotations

import re

import pytest


@pytest.fixture
def page(client) -> str:
    r = client.get("/")
    assert r.status_code == 200
    return r.text


def slice_between(page: str, start: str, end: str) -> str:
    """The page text from ``start`` up to the next ``end`` after it."""
    begin = page.index(start)
    return page[begin:page.index(end, begin)]


def function_body(page: str, name: str) -> str:
    """A top-level ``function name(…) { … }`` body, by its closing brace at column 0."""
    head = page.index(f"function {name}(")
    return page[head:page.index("\n}\n", head)]


def script_block(page: str) -> str:
    """The page's inline ``<script>`` body — the only place that builds element lookups."""
    begin = page.index("<script>") + len("<script>")
    return page[begin:page.index("</script>", begin)]


# ---------------------------------------------------------------- profile card
def test_profile_card_shows_supports_and_brim(page):
    info = slice_between(page, '$("#profInfo").innerHTML = kv([', "]);")
    assert '"supports"' in info
    assert 'eff.supports ? "Yes" : "No"' in info
    assert '"brim"' in info
    assert 'eff.brim ? (eff.brim_width ? `Yes (${eff.brim_width} mm)` : "Yes") : "No"' in info
    # the rows come from GET /api/profile, not from the raw preset
    assert "act.effective" in page

    # a stale derived preset has to say so, with the server's reason when it has one
    assert 'id="profWarning"' in page
    load = function_body(page, "loadProfile")
    assert "derived.stale" in load and "derived.error" in load


def test_supports_and_brim_selects_live_in_the_profile_card(page):
    card = page.index('id="profCardTitle"')
    card_end = page.index('id="uploadTitle"')          # the next card on the dashboard
    settings = page.index('id="viewSettings"')

    supports = page.index('id="optSupports"')
    brim = page.index('id="optBrim"')
    assert card < supports < brim < card_end, "the two option selects must sit in the profile card"
    assert page.count('id="optSupports"') == 1
    assert page.count('id="optBrim"') == 1
    assert page.index('id="profileSelectors"') < supports

    # Settings keeps only the visibility toggle, under the renamed heading
    assert 'id="optHideProfiles"' in page[settings:]
    assert 'id="optSupports"' not in page[settings:]
    assert 'id="optBrim"' not in page[settings:]
    assert '<h2 id="optionsTitle">Interface</h2>' in page


def test_enable_button_and_its_wording_are_gone(page):
    assert 'id="btnSaveProfile"' not in page
    assert "btnSaveProfile" not in page
    assert "press enable" not in page.lower()
    assert ">Enable<" not in page
    # the import toast now asks for a selection instead of a second confirmation
    assert "select one to activate" in page


def test_profile_selects_apply_on_change(page):
    for sel in ("#selMachine", "#selProcess", "#selFilament"):
        assert f'$("{sel}").addEventListener("change",' in page, sel
    # every one of them ends up in saveProfile, and the machine refetch happens first
    machine = slice_between(page, '$("#selMachine").addEventListener("change",', "\n  });")
    assert "loadProcessOptions(" in machine
    assert machine.index("loadProcessOptions(") < machine.index("saveProfile()")
    process = slice_between(page, '$("#selProcess").addEventListener("change",', ");")
    filament = slice_between(page, '$("#selFilament").addEventListener("change",', ");")
    assert "saveProfile()" in process and "saveProfile()" in filament

    # out-of-order responses must not win: the last request owns the result
    save = function_body(page, "saveProfile")
    assert "const seq = ++profileSeq" in save
    assert "seq !== profileSeq" in save
    # a rejected save reloads the persisted profile so the selects snap back
    assert "loadProfile()" in save
    assert "Active profile updated" in save


def test_blur_does_not_resubmit_the_same_selection(page):
    assert 'addEventListener("blur"' not in page
    assert "dispatchEvent(" not in page          # programmatic re-fill never fires change
    # the refill only rewrites innerHTML, and never calls back into the save path
    for name in ("fillPresets", "loadProcessOptions", "loadProfile"):
        body = function_body(page, name)
        assert "saveProfile" not in body, name
    assert ".innerHTML = (rows || []).map(" in page


# ---------------------------------------------------------------- machine/process pairing
def test_machine_change_autoselects_a_compatible_process(page):
    """The machine change must resolve the pairing before it is written.

    The response always carries the active process (the backend appends it), so the old
    process is in the list even when the new machine does not permit it: ``loadProfile``
    cannot tell them apart either. The resolution therefore happens on the machine change,
    and the branches below are the order the browser runs them in.

    This test also carries the ``?machine=`` filter contract that used to sit in a
    presence-only test of its own: presence alone proved nothing about the handler — that
    test stayed green when the order broke — so it lives here, where the order is pinned.
    """
    # one place builds the query string, so the dashboard process list and the printer
    # form's list ask for exactly the same filter
    assert "?machine=${encodeURIComponent(machineRel)}" in page
    assert "/api/profiles${qs}" in page
    fetch = function_body(page, "fetchPresets")
    assert "machineRel" in fetch and "?machine=" in fetch

    options = function_body(page, "loadProcessOptions")
    assert "fetchPresets(machineRel)" in options
    # the permitted rows are the ones this machine's filter keeps, not the whole response
    assert 'processListedFor(list, row.rel, machineRel)' in options
    # kept branch: the stored process stays; swapped branch: the first permitted row wins
    assert 'fillPresets("#selProcess", list.process, processRel)' in options
    assert 'fillPresets("#selProcess", list.process, permitted[0].rel)' in options
    # the refusal branch keys off a real count: `permitted.length >= 0` would be always
    # true and disable the branch entirely, so the comparison is what gets pinned
    assert "anyPermitted: permitted.length > 0" in options
    assert "anyPermitted: true" in options
    assert 'PROCESS_NOT_LISTED = "Active process is not listed for this machine"' in page

    handler = slice_between(page, '$("#selMachine").addEventListener("change",', "\n  });")
    fetched = handler.index("loadProcessOptions(")
    refused = handler.index("!options.anyPermitted")
    no_save = handler.index("return;")
    saved = handler.index("saveProfile()")
    explained = handler.index("Active process was not listed for this machine — selected ")
    assert fetched < refused < no_save < saved < explained
    assert '"No process is listed for this machine' in page   # the refused case says so

    # the printer form resolves its pairing the same way instead of submitting one the
    # machine does not permit (and clears back to "inherit" when nothing is listed)
    printer = slice_between(page, '$("#pfMachine").addEventListener("change",', "\n  });")
    assert "fetchPresets(machineRel)" in printer
    assert "processListedFor(list, row.rel, machineRel)" in printer
    assert printer.index("fetchPresets(machineRel)") < printer.index('fillPaired("#pfProcess"')
    assert 'fillPaired("#pfProcess", "process", list.process, keep ? current' in printer
    assert 'permitted.length ? permitted[0].rel : ""' in printer


def test_refused_machine_change_restores_the_persisted_machine(page):
    """A refused machine change must put the control back on the stored machine.

    ``#selProcess`` and ``#selFilament`` post the DOM as it stands, so a ``#selMachine``
    left showing the machine that permits nothing would be written by the next one of those
    changes, together with the process profile.json still holds — the incompatible pairing
    this whole path exists to keep out of profile.json. Reverting the control is therefore
    part of the refusal; a note or a toast on its own leaves the bug in place.
    """
    handler = slice_between(page, '$("#selMachine").addEventListener("change",', "\n  });")
    refused = handler.index("!options.anyPermitted")
    branch = handler[refused:handler.index("return;", refused)]

    # the revert reads a real value: `activePaths` is declared, and loadProfile assigns it
    # from the GET /api/profile response's `paths` key — an undefined or typo'd source
    # would make the revert a no-op that this test would otherwise still pass
    assert "let activePaths = {}" in page
    load = function_body(page, "loadProfile")
    assert 'fetch("/api/profile")' in load
    assert "activePaths = act.paths || {}" in load

    assert "ev.target.value = activePaths.machine" in branch, "the refused branch must revert the control"
    restored = branch.index("ev.target.value = activePaths.machine")
    refreshed = branch.index("await loadProfile()")
    note = branch.index("setCompatNote(true, PROCESS_NOTHING_LISTED)")
    # restore the control, re-list the stored machine's presets, then explain — the note has
    # to outlive the refresh loadProfile() does, exactly like the swapped-in case below it
    assert restored < refreshed < note
    assert "saveProfile()" not in branch            # the refusal never reaches the API
    assert "toast(" not in branch                  # a toast alone leaves the stale control


def test_a_failed_refresh_still_restores_the_process_control(page):
    """Swallowing a failed recovery refresh leaves the refused machine's rows selected.

    ``loadProfile()`` rejecting on its own leaves ``#selProcess`` holding the rows the
    refused machine permits while ``#selMachine`` shows the persisted one; the next
    ``#selProcess``/``#selFilament`` change posts the DOM, and ``POST /api/profile/select``
    takes that pairing as it stands. So the catch must rebuild the process control rather
    than swallow, from the list ``loadProcessOptions`` already fetched (it returns the raw
    response) or by re-running it for the persisted machine.
    """
    handler = slice_between(page, '$("#selMachine").addEventListener("change",', "\n  });")
    refused = handler.index("!options.anyPermitted")
    branch = handler[refused:handler.index("return;", refused)]
    start = branch.index("await loadProfile().catch(")
    recovery = branch[start:branch.index("});", start)]
    assert 'fillPresets("#selProcess"' in recovery or "loadProcessOptions(" in recovery, (
        "the recovery refresh must restore the process control, not swallow")

    # both return paths of loadProcessOptions have to carry the list that recovery reads
    options = function_body(page, "loadProcessOptions")
    assert options.count(", list}") == 2, "every result carries the raw list"


def test_profile_card_note_explains_an_auto_selected_process(page):
    note = slice_between(page, 'id="profCompat"', "</p>")
    assert 'role="status"' in note and "hidden" in note

    # one writer for the note, and the text travels with the visibility
    setter = function_body(page, "setCompatNote")
    assert "note.textContent = text || PROCESS_NOT_LISTED" in setter
    assert "note.hidden = !shown" in setter

    # the three things the operator can be told, none of which is silent
    assert 'PROCESS_NOT_LISTED = "Active process is not listed for this machine"' in page
    assert 'PROCESS_NOTHING_LISTED = "No process is listed for this machine' in page
    assert "Active process was not listed for this machine — selected ${options.name}" in page


def test_every_element_id_the_script_looks_up_exists_in_the_markup(page):
    """A listener bound to a missing id never fires, and nothing else in CI notices.

    The lookup is not always ``$("#id")``: helpers such as ``fillPresets("#selProcess", …)``
    and ``fillPaired("#pfProcess", …)`` take the id as a plain ``"#…"`` string, so every id
    literal in the script is resolved against the markup — a typo in one of them used to be
    CI-green.
    """
    markup_ids = set(re.findall(r'id="([\w-]+)"', page))
    looked_up = set(re.findall(r'"#([\w-]+)"', script_block(page)))
    assert len(looked_up) > 40, "the page should be looking its elements up"
    assert looked_up - markup_ids == set()


# ---------------------------------------------------------------- printer form
def test_printer_form_offers_inherit_and_pairing_options(page):
    form = slice_between(page, 'id="printerForm"', "</form>")
    assert 'id="pfMachine"' in form
    assert 'id="pfProcess"' in form
    assert 'id="pfMachine"' in page[page.index('id="viewSettings"'):]

    assert 'INHERIT_PRESET = "Inherit current profile"' in page
    assert '`<option value="">${esc(INHERIT_PRESET)}</option>`' in page
    assert 'profiles: {machine: $("#pfMachine").value || null, process: $("#pfProcess").value || null}' in page

    # editing loads the stored pairing (and filters its process list by its machine)
    assert "populatePairing(" in function_body(page, "editPrinter")
    assert 'fillPaired("#pfProcess", "process", list.process' in function_body(page, "populatePairing")
    # the chip line names the paired presets
    assert "printer-pair" in page
    assert "presetName(" in function_body(page, "renderPrinters")


# ---------------------------------------------------------------- upload
def test_upload_accepts_zip_and_reports_the_archive_member(page):
    assert 'accept=".stl,.3mf,.obj,.zip"' in page
    assert "j.archive" in page
    assert "Slicing ${j.archive.member} (1 of ${total} model files)" in page
    assert "j.archive.candidates" in page


def test_upload_reports_a_profile_switch(page):
    switch = slice_between(page, "if (j.threemf) {", "refresh();")
    assert "j.threemf.printer" in switch and "j.threemf.process" in switch
    assert "Profile switched to ${printerName} / " in switch
    # the switch rewrote profile.json (and maybe the active printer), so both cards refresh
    assert "await loadPrinters();" in switch
    assert "await loadProfile();" in switch
    assert switch.index("await loadProfile();") < switch.index("Profile switched to")
