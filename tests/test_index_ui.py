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


# ---------------------------------------------------------------- printer card
def test_profile_edit_controls_live_in_the_printer_card(page):
    """The printer and the things you change about it are one card.

    Order inside the Printer card is the tab order (DOM order drives it): machine → process →
    filament → supports → brim → auto-orient → shrink to fit → upload → notes. The old Profile
    card keeps the read-only summary only; it is now the last card of the `.cards` cluster, below
    the upload card.
    """
    printer = page.index('id="printerCardTitle"')
    profile = page.index('id="profCardTitle"')
    settings = page.index('id="viewSettings"')

    order = [
        'id="printerInfo"',
        'id="profileSelectors"',
        'id="selMachine"',
        'id="selProcess"',
        'id="selFilament"',
        'id="profOptions"',
        'id="optSupports"',
        'id="optBrim"',
        'id="optAutoOrient"',
        'id="optShrinkFit"',
        'id="fitHint"',
        'for="profFile"',
        'id="profFile"',
        'id="profNotes"',
    ]
    marks = [page.index(needle) for needle in order]
    assert printer < min(marks), "the edit controls belong to the Printer card"
    assert marks == sorted(marks), f"wrong control order inside the Printer card: {order}"
    assert max(marks) < profile, "the Profile card is the read-only summary only"

    # containment, not just order — two regressions the order list above cannot see:
    # (a) #profOptions nested inside #profileSelectors would make hide_profiles start hiding
    #     Supports/Brim, so the selectors wrapper must open and close before the options open
    seg = page[page.index('id="profileSelectors"'):page.index('id="profOptions"')]
    assert seg.count("<div") == seg.count("</div>")   # selectors wrapper closed before options open
    # (b) .subgroup nested deeper than a direct child of the Printer <section> would silently
    #     kill the `.card > .subgroup` rule at app/index.html:136
    between = page[page.index('id="printerFile"'):page.index('<div id="profileSelectors"')]
    assert between.count("<div") == 1 and "</div>" not in between   # .subgroup is the card's direct child

    # the group is one heading level down from the card titles, and it names its group
    assert '<h3 id="profileGroupTitle">Profile</h3>' in page
    assert 'role="group" aria-labelledby="profileGroupTitle"' in page

    # the Profile card is now the LAST card of the .cards cluster — bound it by its own tag, not by
    # whatever card happens to follow (the upload card moved above it, see
    # test_dashboard_source_order_and_desktop_grid)
    summary = slice_between(page, 'id="profCardTitle"', "</section>")
    for kept in ('id="profSource"', 'id="profInfo"', 'id="profWarning"', 'id="profCompat"'):
        assert kept in summary, kept
    for moved in ('id="profileSelectors"', 'id="profOptions"', 'id="profFile"', 'id="profNotes"'):
        assert moved not in summary, moved

    # a moved node must not be duplicated: every id still resolves exactly once
    for elem in ("printerCardTitle", "printerInfo", "progTrack", "progBar", "printerFile",
                 "profileGroupTitle", "profCardTitle", "profSource", "profInfo", "profWarning",
                 "profCompat", "profileSelectors", "selMachine", "selProcess", "selFilament",
                 "profOptions", "optSupports", "optBrim", "optAutoOrient", "optShrinkFit",
                 "fitHint", "profFile", "profNotes"):
        assert page.count(f'id="{elem}"') == 1, elem

    # Settings keeps only the visibility toggle, under the renamed heading
    assert 'id="optHideProfiles"' in page[settings:]
    assert 'id="optSupports"' not in page[settings:]
    assert 'id="optBrim"' not in page[settings:]
    assert 'id="optAutoOrient"' not in page[settings:]
    assert 'id="optShrinkFit"' not in page[settings:]
    assert '<h2 id="optionsTitle">Interface</h2>' in page


def test_hide_profiles_still_hides_only_the_selectors(page):
    """`hide_profiles` gates the three selects — never the options or the upload control.

    The group moved inside the Printer card, so the guard is pinned to the one id it always
    named: hiding `#profOptions` or the upload button would take Supports/Brim away on a
    setting that never meant to.
    """
    body = function_body(page, "applyProfileVisibility")
    assert '$("#profileSelectors")' in body
    assert body.count(".hidden") == 1
    for untouched in ("#profOptions", "#profFile", "#profNotes", "#optSupports", "#optBrim"):
        assert untouched not in body, untouched
    # the id-level display:grid still needs its [hidden] guard, and only that one
    assert "#profileSelectors[hidden]{ display:none; }" in page


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


def test_fit_options_reach_the_options_endpoint_and_a_real_scale_shows_in_the_row(page):
    """Auto-orient and shrink to fit are stored like supports/brim, and the row only shows a real scale.

    ``auto_orient`` is on by default, so the reader has to be ``!== false``: ``!!`` would read a
    profile written before the option existed as *off*, the opposite of the server's default.
    The job row is the one place a factor is surfaced, and it must stay quiet for ``1.0``,
    ``null`` and a job saved before the field existed — hence the guard, not just the markup.
    """
    assert "let profileOptions = {supports:false, brim:true, hide_profiles:false, auto_orient:true, shrink_to_fit:false};" in page

    apply = function_body(page, "applyProfileOptions")
    assert "opts.auto_orient !== false" in apply
    assert "!!opts.shrink_to_fit" in apply
    assert '$("#optAutoOrient").value = orient ? "yes" : "no";' in apply
    assert '$("#optShrinkFit").value = shrink ? "yes" : "no";' in apply
    # both keys survive into the in-memory options the error path restores from
    assert "auto_orient: orient, shrink_to_fit: shrink" in apply

    for sel, key in (("#optAutoOrient", "auto_orient"), ("#optShrinkFit", "shrink_to_fit")):
        assert (f'$("{sel}").addEventListener("change", '
                f'ev => saveOption("{key}", ev.target.value === "yes"));') in page, key

    # the hint is one muted paragraph inside the options grid, after both selects
    options = slice_between(page, 'id="profOptions"', 'class="acts"')
    assert '<p class="hint" id="fitHint">' in options
    assert options.index('id="optAutoOrient"') < options.index('id="fitHint"')
    assert options.index('id="optShrinkFit"') < options.index('id="fitHint"')

    row = function_body(page, "refresh")
    assert 'typeof j.fit_scale === "number" && j.fit_scale < 1' in row
    assert "Math.round(j.fit_scale * 100)" in row
    stats = slice_between(row, 'class="job-stats"', "</div>")
    assert "scaled <b>${scaled}%</b> to fit" in stats
    assert re.search(r'\$\{\s*scaled === null \? "" :', stats)   # no span at all unless it scaled


def test_only_supports_and_brim_claim_the_preset_was_updated(page):
    """The status line and the card refresh follow the same rule: only preset-backed switches.

    ``auto_orient`` and ``shrink_to_fit`` are slicer arguments, not preset fields, so saving
    them must not claim a preset update nor trigger a profile reload.
    """
    assert 'const PRESET_OPTIONS = new Set(["supports", "brim"]);' in page

    save = function_body(page, "saveOption")
    assert ('key === "hide_profiles" ? "" : (PRESET_OPTIONS.has(key) ? "Process preset updated" : "Saved")'
            in save)
    assert 'if (key !== "hide_profiles" && PRESET_OPTIONS.has(key)) await loadProfile();' in save
    assert save.count("await loadProfile()") == 1


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


def test_the_env_printer_gets_a_profiles_row_action(page):
    """The env-seeded row is the one that is pairing-editable but not editable.

    It owns the machine/process pairing, so it needs an action of its own: on a
    single-printer deployment it is the only row, and without this button the two selects
    in the form below it are unreachable. The gate is pinned as the expression, not as a
    substring, because ``pairing_editable`` also appears in the row's other lookups — and
    an editable row keeps its pairing inside Edit, so it must not get a second button.
    """
    render = function_body(page, "renderPrinters")
    assert "const pairingBtn = p.pairing_editable && !p.editable ? `" in render

    button = slice_between(render, "const pairingBtn =", '` : "";')
    assert 'data-act="pairing"' in button
    assert 'data-ref="${esc(p.id)}"' in button
    assert '<button type="button" class="btn" data-act="pairing"' in button
    assert '<svg class="icon"' in button           # same icon row as its neighbours
    assert "\n        Profiles\n" in button

    # the button reaches the markup, and the editable-gated pair is left as it was
    assert "${edit}${pairingBtn}${remove}" in render
    assert "const edit = p.editable ? `" in render
    assert "const remove = p.editable ? `" in render
    edit = slice_between(render, "const edit = p.editable ?", '` : "";')
    assert 'data-act="pairing"' not in edit, "an editable row pairs through Edit"


def test_edit_pairing_locks_the_credentials_and_relabels_the_form(page):
    """Pairing-only mode must not offer a rename: PATCH /api/printers/env 400s on
    name/url/api_key, so those inputs are locked and the two selects are the whole form."""
    body = function_body(page, "editPairing")
    assert "if (!p || !p.pairing_editable) return;" in body
    assert "pairingOnly = id;" in body
    for sel in ("#pfName", "#pfUrl", "#pfKey", "#btnRevealKey"):
        assert f'$("{sel}").disabled = true' in body, sel

    # the presets come from the shared loader — a second fetch here would be a duplicate
    assert 'await populatePairing(pairing.machine || "", pairing)' in body
    assert "fetch(" not in body and "fetchPresets" not in body

    assert '"#formTitle").textContent = `Profiles — ${p.name}`' in body
    assert '"#btnPrinterSave").textContent = "Save profiles"' in body
    assert '"#btnPrinterCancel").hidden = false' in body


def test_leaving_pairing_only_mode_unlocks_the_credentials(page):
    """Profiles then Edit, without a submit or a Cancel in between.

    Both entry points write the same form, so the one that is not pairing-only has to
    clear the flag and unlock the inputs ``editPairing`` locked — otherwise the next
    submit patches the wrong printer from disabled fields.
    """
    edit = function_body(page, "editPrinter")
    assert "pairingOnly = null;" in edit
    for sel in ("#pfName", "#pfUrl", "#pfKey", "#btnRevealKey"):
        assert f'$("{sel}").disabled = false' in edit, sel
    assert '$("#formTitle").textContent = "Edit printer"' in edit


def test_submitting_the_pairing_patches_only_the_profiles(page):
    """PATCH /api/printers/<env id> with ``{profiles: …}`` and nothing else."""
    assert "let pairingOnly = null;" in page
    body = function_body(page, "submitPrinter")
    branch = slice_between(body, "if (pairingOnly) {", "\n    return;")

    assert "/api/printers/${pairingOnly}" in branch
    assert 'method: "PATCH"' in branch
    assert ('body: JSON.stringify({profiles: {machine: $("#pfMachine").value || null, '
            'process: $("#pfProcess").value || null}})') in branch
    # nothing the env endpoint rejects travels in this body
    assert "name:" not in branch
    assert "url:" not in branch
    assert "api_key" not in branch

    assert '"Profiles updated"' in branch
    for call in ("resetPrinterForm();", "await loadPrinters();", "refreshPrinter();"):
        assert call in branch, call

    # the save rewrote profile.json, so the dashboard has to reload it after the printer
    # list: otherwise #selMachine keeps the previous machine and the next process save
    # posts it back to /api/profile/select, silently reverting the pairing just saved
    load = "await loadProfile().catch(() => {});"
    assert load in branch, "the pairing branch must refresh the profile card"
    assert (branch.index("await loadPrinters();") < branch.index("refreshPrinter();")
            < branch.index(load)), "loadProfile() must run after loadPrinters/refreshPrinter"

    # the create/edit path stays where it was, behind this branch
    assert "const editing = Boolean(editingId);" in body
    assert 'method: editing ? "PATCH" : "POST"' in body
    assert 'toast(editing ? "Printer updated" : "Printer added")' in body


def test_submitting_the_printer_form_refreshes_the_profile_card(page):
    """The create/edit branch repoints a printer's pairing too, so it reloads the card.

    Editing the active printer's machine/process writes profile.json just like the
    pairing branch does; without the reload #selMachine keeps the old machine and the
    next process save posts it back to /api/profile/select.
    """
    body = function_body(page, "submitPrinter")
    store = body[body.index("const editing = Boolean(editingId);"):]

    load = "await loadProfile().catch(() => {});"
    assert load in store, "the printer-form save branch must refresh the profile card"
    assert (store.index("await loadPrinters();") < store.index("refreshPrinter();")
            < store.index(load)), "loadProfile() must run after loadPrinters/refreshPrinter"
    # the reload stays inside the try, on the success side of the error toast
    assert store.index("toast(editing ? \"Printer updated\"") < store.index(load) < store.index("catch (e) {")


def test_resetting_the_printer_form_leaves_pairing_only_mode(page):
    """Cancel and every successful submit land here, so the plain form has to come back."""
    body = function_body(page, "resetPrinterForm")
    assert "pairingOnly = null;" in body
    for sel in ("#pfName", "#pfUrl", "#pfKey", "#btnRevealKey"):
        assert f'$("{sel}").disabled = false' in body, sel
    assert '$("#formTitle").textContent = "Add printer"' in body
    assert '$("#btnPrinterSave").textContent = "Add printer"' in body
    # the process list still snaps back to the unfiltered one
    assert 'populatePairing("", null)' in body


def test_the_printer_list_delegates_the_pairing_action(page):
    handler = slice_between(page, '$("#printerList").addEventListener("click",', "\n  });")
    assert 'act === "pairing"' in handler
    assert "editPairing(ref)" in handler
    for other in ('act === "use"', 'act === "test"', 'act === "edit"', 'act === "delete"'):
        assert other in handler, other
    # defined above the wiring that binds it
    assert page.index("async function editPairing(") < page.index("function boot()")


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


def test_dashboard_source_order_and_desktop_grid(page):
    """Source order is the mobile order: printer → uploads → profile info → jobs.

    Desktop is the same DOM split by named grid areas, so the tab order, the skip-link target
    (#main) and the screen-reader order stay correct without any `order:` reshuffle.
    """
    view = slice_between(page, 'id="viewDashboard"', 'id="viewSettings"')
    printer = view.index('id="printerCardTitle"')
    upload = view.index('id="uploadTitle"')
    profile = view.index('id="profCardTitle"')
    jobs = view.index('id="jobsTitle"')

    # (a) uploads after the Printer card, (b) uploads before the Profile-info card
    assert printer < upload < profile
    # (c) the jobs section is last — after every card, and it is not inside the card cluster
    assert profile < jobs
    jobs_block = view[jobs:]
    assert 'id="btnDeleteAll"' in jobs_block and 'id="jobs"' in jobs_block
    assert 'id="jobCount"' in jobs_block

    # the three cards share one wrapper, which is what makes the two-column split possible
    cards = view[view.index('<div class="cards"'):jobs]
    for needle in ('id="printerCardTitle"', 'id="uploadTitle"', 'id="profCardTitle"'):
        assert needle in cards, needle
    assert cards.count('class="card"') == 3

    # real nesting: upload must not live inside the printer or profile card
    printer_slice = slice_between(page, 'aria-labelledby="printerCardTitle"', "</section>")
    assert 'id="uploadTitle"' not in printer_slice
    profile_slice = slice_between(page, 'aria-labelledby="profCardTitle"', "</section>")
    assert 'id="uploadTitle"' not in profile_slice

    # (d) the desktop grid declaration exists, is explicit, and keeps the mobile order as its default
    style = page[page.index("<style>"):page.index("</style>")]
    # whitespace-normalised: the assertion must not depend on how the page breaks its declarations
    assert re.search(r'grid-template-areas:\s*"printer"\s*"upload"\s*"profile"', style)
    desktop = style[style.index("@media (min-width:900px)"):style.index("\n}", style.index("@media (min-width:900px)"))]
    assert '"printer upload"' in desktop and '"profile profile"' in desktop
    assert re.search(r'grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)', desktop)
    for area, title in (("printer", "printerCardTitle"), ("upload", "uploadTitle"), ("profile", "profCardTitle")):
        assert re.search(rf'\[aria-labelledby="{title}"\]\s*\{{\s*grid-area:\s*{area};', style), title
    assert not re.search(r"[;{\s]order\s*:", style, re.I), "the layout must not depend on `order:`"


# ---------------------------------------------------------------- slicing wait (task 4)
def test_active_job_row_carries_an_indeterminate_slice_indicator(page):
    """The CLI reports no slice fraction, so the row must not imply one.

    The bar is a named ``role="progressbar"`` with no value attributes: the tabular
    ``aria-valuenow`` set belongs to the Moonraker print bar (``#progTrack``) only, and
    copying it here would turn an honest "length unknown" into a second fake percentage.
    """
    row = function_body(page, "refresh")
    assert 'class="slice-wait"' in row
    assert 'aria-busy="true"' in row
    assert 'role="progressbar"' in row
    assert 'class="slice-track"' in row

    bar = slice_between(row, 'class="slice-track"', "</span>")
    assert 'aria-label="${label}"' in bar
    assert "aria-valuenow" not in bar
    assert "aria-valuemin" not in bar
    assert "aria-valuemax" not in bar


def test_queued_and_slicing_use_different_wait_labels(page):
    """Waiting for the slicer and slicing are different waits, so they read differently.

    Both labels are literals in ``SLICE_LABELS``; the row takes its label from that map by
    status, so the text cannot drift away from the state it describes.
    """
    labels = slice_between(page, "const SLICE_LABELS", "};")
    assert re.search(r'queued:\s*"waiting for the slicer…"', labels)
    assert re.search(r'slicing:\s*"slicing…"', labels)
    row = function_body(page, "refresh")
    assert "SLICE_LABELS[j.status]" in row
    # the row interpolates that map's value, so a hard-coded label cannot pass this test
    label_span = slice_between(row, 'class="slice-wait-label"', "</span>")
    assert re.search(r'class="slice-wait-label"[^>]*>\s*\$\{\s*label\s*\}\s*$', label_span)


def test_slice_indicator_is_gated_off_for_done_and_failed_jobs(page):
    """Only a live slice gets the bar: a failure shows its error text and no progress at all.

    ``sliced`` / ``printed`` are finished and ``failed`` carries an error, so the two guards
    together (status in the helper, ``!j.error`` at the call site) make ``.slice-wait``
    unreachable for all three.
    """
    refresh_body = function_body(page, "refresh")
    assert "const active = isSlicing(j.status) && !j.error;" in refresh_body
    assert "isSlicing(j.status)" in refresh_body
    assert "!j.error" in refresh_body
    # the guard only means anything if the markup is driven by it: `active` picks the bar and
    # the other branch is the empty string, so no finished or failed row can render one
    assert re.search(r"const wait\s*=\s*active\s*\?", refresh_body)
    assert "${wait}" in refresh_body
    assert re.search(r'const wait\s*=\s*active\s*\?\s*`[^`]*`\s*:\s*""\s*;', refresh_body)
    # the error itself is still rendered — nothing is hidden, only the bar is withheld.
    # The row markup is pinned inside the row template, not page-wide: a page-wide match would
    # still pass off the catch-path markup if the per-row error line were deleted.
    assert re.search(r'<p\s+class="err">\$\{esc\(j\.error\)\}</p>', refresh_body)
    assert re.search(r'\$\{j\.error\s*\?\s*`<p\s+class="err"\s*>\$\{\s*esc\(j\.error\)\s*\}', refresh_body)
    # ...and the load-failure message is its own, separately required piece of markup
    catch_body = re.split(r"\}\s*catch\s*\(\s*e\s*\)\s*\{", refresh_body)[-1]
    assert re.search(r'<p\s+class="err">Could not load jobs:\s*\$\{\s*esc\(e\.message\)\s*\}</p>', catch_body)

    helper = function_body(page, "isSlicing")
    # function_body runs to the next column-0 brace, so bound the one-line helper first
    decl = helper[:helper.index("\n")]
    assert '"queued"' in decl and '"slicing"' in decl
    for status in ("sliced", "printed", "failed", "error", "done", "canceled", "cancelled"):
        assert f'"{status}"' not in decl, status


def test_upload_card_shows_the_wait_state_in_a_hidden_region(page):
    """The upload card mirrors the same wait, starts hidden, and its bar has no value either."""
    card = slice_between(page, '<section class="card" aria-labelledby="uploadTitle">', "</section>")
    assert 'id="uploadWait"' in card
    assert 'class="slice-wait"' in card
    assert 'id="uploadWaitModel"' in card
    assert "hidden" in slice_between(card, 'id="uploadWait"', ">")
    # beside the drop zone, not inside it: #drop is one big click target
    assert 'id="uploadWait"' not in slice_between(card, 'id="drop"', "</div>")

    bar = slice_between(card, 'class="slice-track"', "</span>")
    assert 'id="uploadWaitTrack"' in bar
    assert 'role="progressbar"' in bar
    assert "aria-valuenow" not in bar
    assert "aria-valuemin" not in bar
    assert "aria-valuemax" not in bar

    style = page[page.index("<style>"):page.index("</style>")]
    # .slice-wait sets display:flex, an author rule that beats the UA [hidden] rule
    assert re.search(r"\.slice-wait\[hidden\]\s*\{\s*display:\s*none", style)


def test_upload_wait_region_is_updated_in_place_without_innerhtml(page):
    """The card is not re-rendered by the poll, so it is toggled and retitled in place.

    Re-parsing it on every poll would restart the animation and could flash the region.
    """
    body = function_body(page, "setUploadWait")
    assert "hidden" in body
    assert "textContent" in body
    assert "innerHTML" not in body
    # failed wins in the card too, so a contradictory payload cannot paint a bar over an error
    assert "const active = !!job && isSlicing(job.status) && !job.error;" in body
    # the bar's accessible name tracks the visible label, and it is synced on its own guard: the
    # role=progressbar track is not a descendant of the label span, so a reader of the bar cannot
    # see the label text. Folding this back inside the labelEl guard would leave a stale name.
    assert re.search(
        r'if\s*\(\s*track\.getAttribute\("aria-label"\)\s*!==\s*label\s*\)\s*'
        r'track\.setAttribute\("aria-label",\s*label\)',
        body,
    )
    refresh_body = function_body(page, "refresh")
    assert "setUploadWait(jobs[0])" in refresh_body
    # a failed poll clears the card's wait too, so it cannot contradict the error row list —
    # and only the catch branch may clear it: clearing on the happy path would blank a live slice
    try_part, catch_part = re.split(r"\}\s*catch\s*\(\s*e\s*\)\s*\{", refresh_body)
    assert re.search(r"setUploadWait\s*\(\s*null\s*\)", catch_part)
    assert not re.search(r"setUploadWait\s*\(\s*null\s*\)", try_part)


def test_reduced_motion_keeps_the_slice_indicator_visible(page):
    """Motion off freezes the sweep, so the frozen state has to be a visible static bar.

    A 35 % segment stuck at phase 0 plus real label text is the fallback; this test pins the
    static fill so a later translucent-sweep redesign cannot leave an empty-looking track.
    The animated base is pinned alongside it: the fallback is only enough while the moving
    state is a visible segment that the keyframes sweep.
    """
    style = page[page.index("<style>"):page.index("</style>")]
    # (0) the animated base: a named sweep over a segment that is already visible at phase 0,
    # so "motion off" is a frozen copy of something rather than the only filled state
    assert re.search(r"@keyframes\s+slice-sweep\s*\{", style)
    base_rules = re.findall(r"\.slice-track\s*>\s*i\s*\{([^}]*)\}", style)
    assert base_rules, "no base .slice-track > i rule"
    base = base_rules[0]  # the reduced-motion override is a later rule in the same block
    assert re.search(r"animation:\s*slice-sweep\b", base)
    width = re.search(r"width:\s*([\d.]+)%", base)
    assert width and float(width.group(1)) > 0, base
    base_width = float(width.group(1))

    block = slice_between(style, "@media (prefers-reduced-motion: reduce)", "\n}\n")
    # (a) the global freeze is still the block's first rule
    assert re.search(
        r"\*,\s*\*::before,\s*\*::after\s*\{\s*transition:\s*none\s*!important\s*;\s*"
        r"animation:\s*none\s*!important\s*;?\s*\}",
        block,
    ), "the block still freezes every transition and animation"
    # (b) the frozen segment becomes a static, full-width striped fill
    assert ".slice-track" in block
    assert "repeating-linear-gradient" in block
    assert re.search(r"width:\s*100%", block)
    frozen = re.search(r"width:\s*([\d.]+)%", block)
    assert frozen and float(frozen.group(1)) > base_width, "the frozen fill is the full track"
    assert "transform:none" in block
    assert "content:" not in block, "the fallback is real text, not a ::before"
    # (c) the labels are DOM text, so they read the same with motion off
    label = slice_between(page, 'id="uploadWaitLabel">', "</span>")
    assert re.fullmatch(r"\s*waiting for the slicer…\s*", label.split(">", 1)[1])


def test_print_progress_bar_stays_determinate_and_separate(page):
    """#progTrack reports the Moonraker print fraction and keeps its own determinate track."""
    track = slice_between(page, 'id="progTrack"', "</div>")
    assert 'aria-valuenow="0"' in track
    assert 'aria-valuemin="0"' in track and 'aria-valuemax="100"' in track
    assert "slice-track" not in track

    style = page[page.index("<style>"):page.index("</style>")]
    assert re.search(r"\.bar\s*\{[^}]*height:\s*8px", style)
    # the print bar still starts at width:0 — the indeterminate bar is a separate rule
    assert re.search(r"\.bar\s*>\s*i\s*\{[^}]*width:\s*0", style)


# ---------------------------------------------------------------- header nav / mobile disclosure
def test_header_nav_keeps_the_two_hash_destinations(page):
    # drop the <nav> opening tag (it carries its own nav* id) so only the link ids are collected
    nav = slice_between(page, 'id="navSections"', "</nav>").split(">", 1)[1]
    assert re.findall(r'id="(nav[\w-]+)"', nav) == ["navDash", "navSettings"]
    assert 'id="navDash" href="#/"' in nav
    assert 'id="navSettings" href="#/settings"' in nav
    assert nav.count("<a ") == 2


def test_switcher_is_one_compact_row_inside_the_header_bar(page):
    bar = page.index('<div class="header-bar">')
    assert "</div>" not in page[bar:page.index('id="navToggle"')]
    assert "</div>" not in page[bar:page.index('id="navSections"')]
    style = page[page.index("<style>"):page.index("</style>")]
    assert not re.search(r"\n\.tabs\s*\{[^}]*margin-top", style)
    base_tab = re.search(r"\n\.tab\s*\{([^}]*)\}", style).group(1)
    assert "var(--target-min)" not in base_tab


def test_mobile_nav_toggle_is_a_closed_disclosure(page):
    # anchor the slice on the toggle's own id, then walk back to that button's opening tag —
    # `type="button"` and the aria-* attributes live on the opening tag, so the slice must start
    # there; starting at `id="navToggle"` would begin after them and the checks below could not fail
    toggle = page.index('id="navToggle"')
    btn = page[page.rindex("<button", 0, toggle):page.index("</button>", toggle)]
    assert re.search(r'aria-expanded="false"', btn)          # closed by default
    assert re.search(r'aria-controls="navSections"', btn)    # controls the real menu
    assert re.search(r'<nav[^>]*id="navSections"', page)     # and that target is a real <nav>
    assert re.search(r'aria-label="[^"]+"', btn)              # icon-only button has a name
    assert 'type="button"' in btn
    assert "<svg" in btn and 'aria-hidden="true"' in btn      # inline SVG, decorative
    assert "class=\"icon\"" in btn


def test_nav_disclosure_is_wired_and_the_router_is_untouched(page):
    script = script_block(page)
    body = function_body(page, "setNavOpen")
    assert "aria-expanded" in body and "is-open" in body      # one place syncs both
    esc = slice_between(script, 'document.addEventListener("keydown"', "});")
    assert 'ev.key !== "Escape"' in esc
    assert "setNavOpen(false)" in esc and ".focus()" in esc   # Escape closes + returns focus
    assert 'closest("a")' in script                           # destination click closes
    assert "setNavOpen(false)" in function_body(page, "setView")   # route change closes
    # frozen router
    assert "let currentView = null;" in script
    assert script.count('if (currentView === "settings" || document.hidden) return;') == 2


def test_closed_mobile_menu_leaves_nothing_focusable(page):
    style = page[page.index("<style>"):page.index("</style>")]
    block = slice_between(style, "@media (max-width:720px)", "\n}\n")
    assert re.search(r"\.tabs\s*\{\s*display:\s*none", block)
    assert re.search(r"\.tabs\.is-open\s*\{[^}]*display:\s*flex", block)
    assert "visibility" not in block and "opacity" not in block


def test_skip_link_still_precedes_the_header(page):
    assert page.index('id="skip"') < page.index('class="app-header"')
    assert page.index('class="app-header"') < page.index('id="main"')
    assert 'id="skip" href="#main"' in page
    assert re.search(r'id="main"[^>]*tabindex="-1"', page)
    bar = page.index('<div class="header-bar">')
    assert "</div>" not in page[bar:page.index('id="printerPill"')]   # pill stays in the top bar


# ---------------------------------------------------------------- transfer progress / latest job
def upload_card(page: str) -> str:
    """The "Slice a model" card — the live surface for the most recent job."""
    return slice_between(page, '<section class="card" aria-labelledby="uploadTitle">', "</section>")


def test_upload_transfers_with_xmlhttprequest_not_fetch(page):
    """fetch cannot report request-body progress, and the silent transfer was the bug."""
    body = function_body(page, "upload")
    assert "new XMLHttpRequest()" in body
    assert 'xhr.open("POST", "/api/upload")' in body
    assert 'fetch("/api/upload"' not in page


def test_upload_onprogress_writes_a_percent_and_an_mb_figure(page):
    body = function_body(page, "upload")
    assert "xhr.upload.onprogress = ev =>" in body
    assert "setUploadProgress(ev.loaded, ev.lengthComputable ? ev.total : 0)" in body

    progress = function_body(page, "setUploadProgress")
    assert "Math.round((sent / total) * 100)" in progress
    assert "(sent / 1048576).toFixed(1)" in progress
    assert '`Uploading ${pct}% · ${mb} / ${(total / 1048576).toFixed(1)} MB`' in progress
    # an unknown total shows the sent bytes only, never a fraction
    assert '`Uploading ${mb} MB…`' in progress


def test_aria_valuenow_only_while_bytes_move(page):
    """A determinate transfer claims a value; the slice wait must not keep it."""
    progress = function_body(page, "setUploadProgress")
    assert 'track.setAttribute("aria-valuemin", "0")' in progress
    assert 'track.setAttribute("aria-valuemax", "100")' in progress
    assert 'track.setAttribute("aria-valuenow", String(pct))' in progress
    assert 'track.removeAttribute("aria-valuenow")' in progress      # unknown total
    assert "track.classList.add(\"is-determinate\")" in progress
    assert "track.classList.remove(\"is-determinate\")" in progress

    # the moment the job exists the box becomes indeterminate, and the poll's takeover strips
    # every value attribute it inherited from the transfer
    assert "setUploadWait({status: \"queued\", filename: file.name});" in function_body(page, "upload")
    wait = function_body(page, "setUploadWait")
    assert "resetUploadTrack()" in wait
    reset = function_body(page, "resetUploadTrack")
    for attr in ("aria-valuenow", "aria-valuemin", "aria-valuemax"):
        assert f'track.removeAttribute("{attr}")' in reset, attr

    # the static markup never claims a value either
    bar = slice_between(upload_card(page), 'class="slice-track"', "</span>")
    for attr in ("aria-valuenow", "aria-valuemin", "aria-valuemax"):
        assert attr not in bar, attr


def test_a_second_drop_or_pick_is_ignored_while_uploading(page):
    body = function_body(page, "upload")
    assert "if (uploading) return;" in body
    # the drop zone is marked busy for the whole flight and released in finally
    assert 'drop.setAttribute("aria-disabled", "true")' in body
    assert 'drop.setAttribute("aria-disabled", "false")' in body
    assert "} finally {" in body
    assert "if (!uploading) input.click();" in page

    drop_handler = slice_between(page, 'drop.addEventListener("drop"', ");")
    assert "if (uploading) return;" in drop_handler
    change_handler = slice_between(page, 'input.addEventListener("change"', ");")
    assert "if (uploading) return;" in change_handler


def test_slice_card_shows_the_latest_job_and_reuses_the_print_path(page):
    card = upload_card(page)
    for needle in ('id="uploadLatest"', 'id="uploadLatestName"', 'id="uploadLatestOut"',
                   'id="uploadLatestErr"', 'id="uploadLatestPrint"'):
        assert needle in card, needle

    latest = function_body(page, "setUploadLatest")
    assert "job.filename" in latest                  # the model that was uploaded
    assert "job.output_name" in latest               # → <output_name>.gcode
    assert ".gcode" in latest
    assert 'job.status === "sliced"' in latest
    assert "print.hidden = !printable" in latest
    assert "print.dataset.ref = job.id" in latest
    assert "job.error" in latest and "errEl.textContent" in latest

    # the print button is a real button with a name, hidden until the job is sliced
    button = card[card.rindex("<button", 0, card.index('id="uploadLatestPrint"')):]
    button = button[:button.index(">")]
    assert 'type="button"' in button
    assert "hidden" in button
    assert "aria-label=" in button

    # derived from the poll's first row, so a reload mid-slice lands in the same state
    refresh_body = function_body(page, "refresh")
    assert "setUploadLatest(jobs[0])" in refresh_body
    assert re.search(r"setUploadLatest\s*\(\s*null\s*\)", refresh_body.split("catch (e) {")[-1])

    # and it prints through the same implementation, not a second copy
    handler = slice_between(page, '$("#uploadLatest").addEventListener("click"', "});")
    assert "printJob(btn.dataset.ref)" in handler
    assert 'fetch(`/api/jobs/${id}/print`' in function_body(page, "printJob")


def test_a_failed_upload_clears_the_bars_state(page):
    body = function_body(page, "upload")
    catch = body[body.index("} catch (e) {"):]
    assert 'toast("Error: " + e.message' in catch   # the server's detail, as everywhere else
    assert "clearUploadWait();" in catch
    assert "setUploadLatest(null);" in catch

    clear = function_body(page, "clearUploadWait")
    assert "transferPhase = false" in clear
    assert '$("#uploadWait").hidden = true' in clear
    assert "resetUploadTrack()" in clear
    assert '$("#uploadWaitLabel").textContent = SLICE_LABELS.queued' in clear


def test_transfer_label_is_a_throttled_live_status(page):
    """The label is polite, but the transfer repaints it at most once a second."""
    label = slice_between(upload_card(page), 'class="slice-wait-label"', ">")
    assert 'role="status"' in label
    assert 'aria-live="polite"' in label
    progress = function_body(page, "setUploadProgress")
    assert "now - lastPaint >= 1000" in progress
