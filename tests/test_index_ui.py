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


def test_hide_profiles_hides_machine_and_process_but_keeps_filament(page):
    """`hide_profiles` gates the Machine and Process pickers — never Filament, the options or
    the upload control.

    Rewritten from `test_hide_profiles_still_hides_only_the_selectors`, which asserted that the
    option gated all three selects. It did so by hiding the whole `#profileSelectors` wrapper,
    and a user who turned the switch on lost the Filament picker with them. The switch is now
    per field, so the guard is pinned to the two ids it names — and Filament is not one of them,
    in either state.
    """
    body = function_body(page, "applyProfileVisibility")
    # judge the code, not the prose: the comment above it names the wrapper it replaced
    code = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    # one assignment driven by the `hide` argument, so both states come from the same line
    assert code.count("el.hidden = !!hide") == 1
    assert "!!hide" in code
    assert '"#fieldMachine", "#fieldProcess"' in code
    assert "#fieldFilament" not in code, "the filament picker must never be hidden"
    for untouched in ("#profileSelectors", "#profOptions", "#profFile", "#profNotes",
                      "#optSupports", "#optBrim"):
        assert untouched not in code, untouched

    # nothing is latched into the DOM: every load and every toggle re-applies the saved value,
    # so turning the option back off brings both fields back
    assert "applyProfileVisibility(hide)" in function_body(page, "applyProfileOptions")

    # the three pickers are three `.field` siblings, each owning its own label/select pair —
    # that is what lets one be hidden while the filament label stays attached to its select
    for field_id, select_id in (("fieldMachine", "selMachine"),
                                ("fieldProcess", "selProcess"),
                                ("fieldFilament", "selFilament")):
        assert page.count(f'id="{field_id}"') == 1, field_id
        block = slice_between(page, f'id="{field_id}"', "</div>")
        assert f'for="{select_id}"' in block and f'id="{select_id}"' in block, field_id

    # a `.field` is display:grid, so a per-field `hidden` needs its own rule to win — the rule
    # the wrapper used to carry went away with the wrapper-wide hide
    assert ".field[hidden]{ display:none; }" in page
    assert "#profileSelectors[hidden]" not in page

    # the group keeps its accessible name, and the options stay outside the wrapper
    assert 'role="group" aria-labelledby="profileGroupTitle"' in page
    assert '<h3 id="profileGroupTitle">Profile</h3>' in page
    seg = page[page.index('id="profileSelectors"'):page.index('id="profOptions"')]
    assert seg.count("<div") == seg.count("</div>")


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

    # the stats live in the one body both hosts render; the row and the card call it rather than
    # carrying a second copy of the template (the card passes withWait=false: #uploadWait above
    # it already paints the live slice state)
    body = function_body(page, "renderJobBody")
    assert 'typeof j.fit_scale === "number" && j.fit_scale < 1' in body
    assert "Math.round(j.fit_scale * 100)" in body
    stats = slice_between(body, 'class="job-stats"', "</div>")
    assert "scaled <b>${scaled}%</b> to fit" in stats
    assert re.search(r'\$\{\s*scaled === null \? "" :', stats)   # no span at all unless it scaled
    assert "renderJobBody(j)" in function_body(page, "renderJobRow")
    assert "renderJobBody(job, false)" in function_body(page, "setUploadLatest")
    assert "jobs.map(renderJobRow)" in function_body(page, "refresh")


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
    row = function_body(page, "renderJobBody")
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
    row = function_body(page, "renderJobBody")
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
    body = function_body(page, "renderJobBody")
    assert "const active = isSlicing(j.status) && !j.error;" in body
    assert "isSlicing(j.status)" in body
    assert "!j.error" in body
    # the guard only means anything if the markup is driven by it: `active` picks the bar and
    # the other branch is the empty string, so no finished or failed row can render one
    assert re.search(r"const wait\s*=\s*active\s*\?", body)
    assert "${waitBlock}" in body
    assert re.search(r'const wait\s*=\s*active\s*\?\s*`[^`]*`\s*:\s*""\s*;', body)
    # the card's body is the same template with the bar suppressed, and it is suppressed at the
    # template (withWait), not by hiding a rendered bar after the fact
    latest = function_body(page, "setUploadLatest")
    assert "renderJobBody(job, false)" in latest
    # the error itself is still rendered — nothing is hidden, only the bar is withheld.
    # The row markup is pinned inside the row template, not page-wide: a page-wide match would
    # still pass off the catch-path markup if the per-row error line were deleted.
    assert re.search(r'<p\s+class="err">\$\{esc\(j\.error\)\}</p>', body)
    assert re.search(r'\$\{j\.error\s*\?\s*`<p\s+class="err"\s*>\$\{\s*esc\(j\.error\)\s*\}', body)
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
def test_header_nav_keeps_the_three_hash_destinations(page):
    # drop the <nav> opening tag (it carries its own nav* id) so only the link ids are collected
    nav = slice_between(page, 'id="navSections"', "</nav>").split(">", 1)[1]
    assert re.findall(r'id="(nav[\w-]+)"', nav) == ["navDash", "navPrinter", "navPresets", "navFilaments", "navSettings"]
    assert 'id="navDash" href="#/"' in nav
    assert 'id="navPrinter" href="#/printer"' in nav
    assert 'id="navPresets" href="#/presets"' in nav
    assert 'id="navFilaments" href="#/filaments"' in nav
    assert 'id="navSettings" href="#/settings"' in nav
    assert nav.count("<a ") == 5
    # one markup/class contract for all tabs
    assert nav.count('class="tab"') == 5


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


# ---------------------------------------------------------------- printer view (iframe embed)
def test_the_printer_view_is_its_own_panel_between_the_other_two(page):
    assert re.search(r'<section class="view" id="viewPrinter"[^>]*aria-labelledby="printerViewTitle"[^>]*hidden',
                     page)
    assert 'id="printerViewTitle" tabindex="-1"' in page
    # DOM order is the tab order: dashboard → printer → settings
    assert page.index('id="viewDashboard"') < page.index('id="viewPrinter"') < page.index('id="viewSettings"')


def test_printer_is_a_cold_loadable_route(page):
    body = function_body(page, "route")
    assert 'if (raw === "printer") return setView("printer");' in body
    # an unknown hash keeps replacing the history entry with the dashboard, as before
    assert 'history.replaceState(null, "", "#/")' in body
    setview = function_body(page, "setView")
    assert 'if (name === "printer") loadPrinterUi();' in setview
    # exactly one call site, so a deep link cannot be half-wired into the view
    assert setview.count("loadPrinterUi()") == 1


def test_the_printer_tab_shares_the_active_state_contract(page):
    script = script_block(page)
    assert 'const VIEWS = { dashboard: DASH, settings: SETT, printer: PRIN, presets: PRES, filaments: FILS };' in script
    assert 'const VIEW_TABS = { dashboard: "#navDash", settings: "#navSettings", printer: "#navPrinter",' in script
    assert 'presets: "#navPresets", filaments: "#navFilaments" };' in script
    body = function_body(page, "setView")
    assert "panel().hidden = view !== name" in body
    assert 'setAttribute("aria-current", "page")' in body
    assert 'removeAttribute("aria-current")' in body


def test_the_frame_is_a_real_unsandboxed_iframe(page):
    # Fluidd/Mainsail are SPAs with their own storage: a sandboxed frame is a blank page
    assert "sandbox" not in page
    tpl = slice_between(page, '<template id="printerFrameTpl">', "</template>")
    frame = tpl[tpl.index("<iframe"):tpl.index(">", tpl.index("<iframe")) + 1]
    assert 'id="printerFrame"' in frame
    assert re.search(r'title="[^"]+"', frame)                       # meaningful, not "frame"
    assert 'loading="lazy"' in frame
    assert 'referrerpolicy="no-referrer"' in frame
    assert "fullscreen" in re.search(r'allow="([^"]*)"', frame).group(1)
    # inert until the portal reports a URL: no iframe, no empty src, no failed load
    assert '<template id="printerFrameTpl">' in page
    assert 'src=""' not in tpl


def test_the_frame_fills_a_reserved_box_and_cannot_collapse(page):
    style = page[page.index("<style>"):page.index("</style>")]
    mount = re.search(r"\n\.frame-mount\s*\{([^}]*)\}", style).group(1)
    assert "height:min(" in mount and "min-height:" in mount
    frame = re.search(r"\n\.printer-frame\s*\{([^}]*)\}", style).group(1)
    assert "width:100%" in frame and "height:100%" in frame
    assert "height:0" not in mount + frame
    # the frame scrolls itself; nothing around it traps the page's scroll
    assert not re.search(r"\n\.frame-mount\s*\{[^}]*overflow", style)
    assert not re.search(r"\n#viewPrinter\s*\{[^}]*overflow", style)


def test_the_way_out_is_a_real_link_in_the_markup(page):
    card = slice_between(page, 'id="printerFrameCard"', "</section>")
    link = card[card.index("<a "):card.index("</a>") + len("</a>")]
    assert 'id="printerOpen"' in link
    assert 'target="_blank"' in link
    assert 'rel="noopener noreferrer"' in link
    assert "href=" in link
    assert re.search(r">\s*Open in a new tab\s*<", link)            # a visible, readable name
    # markup, never built or rebuilt by the script — only its destination moves
    assert "open.href = uiUrl" in function_body(page, "renderPrinterUi")
    assert 'createElement("a")' not in script_block(page)


def test_empty_state_names_the_variable_and_the_browser(page):
    empty = slice_between(page, 'id="printerUiEmpty"', "</p>")
    assert "PRINTER_UI_URL" in empty
    assert "browser" in empty and "server" in empty                  # reachable from the browser
    assert 'role="status"' in empty
    body = function_body(page, "renderPrinterUi")
    assert "empty.hidden = !!uiUrl" in body
    assert "card.hidden = !uiUrl" in body
    # .frame-card is display:grid, which loses to the hidden attribute unless the sheet says so
    style = page[page.index("<style>"):page.index("</style>")]
    assert re.search(r"\n\.frame-card\[hidden\]\s*\{\s*display:\s*none;?\s*\}", style)
    # no URL → no iframe at all, which is why the empty state exists
    assert "if (!uiUrl) return;" in body
    assert body.index("if (!uiUrl) return;") < body.index("cloneNode")


def test_an_https_portal_warns_instead_of_framing_a_plain_http_ui(page):
    warn = slice_between(page, 'id="printerUiMixed"', "</p>")
    assert "mixed content" in warn and "PRINTER_UI_URL" in warn
    assert 'role="status"' in warn
    helper = function_body(page, "isMixedContent")
    assert 'location.protocol === "https:"' in helper               # detected, not assumed
    assert "http:" in helper
    body = function_body(page, "renderPrinterUi")
    assert "const blocked = isMixedContent(uiUrl);" in body
    assert "mixed.hidden = !blocked;" in body
    # the link is set before the block decision, so the way forward survives the warning
    assert body.index("open.href = uiUrl") < body.index("isMixedContent(uiUrl)")
    assert "mount.hidden = blocked;" in body                         # blocked → no reserved box
    assert "if (blocked) return;" in body                           # blocked → no frame
    # an existing frame is repointed instead of rebuilt, so switching the active printer
    # navigates the frame once and an unchanged visit navigates it not at all
    assert "const frame = mount.firstElementChild;" in body
    assert "if (frame) { frame.src = uiUrl; return; }" in body
    assert body.index("const frame = mount.firstElementChild;") < body.index("cloneNode")


def test_the_poller_pauses_on_the_printer_view_like_settings(page):
    helper = function_body(page, "pollPaused")
    assert 'currentView !== "dashboard"' in helper
    assert "document.hidden" in helper
    script = script_block(page)
    assert script.count("if (pollPaused()) return;") == 2           # refresh() and the interval
    assert "if (pollPaused()) return;" in function_body(page, "refresh")
    assert "if (pollPaused()) return;" in script[script.index("setInterval("):]


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
    """The card's "Last job" block is the job row's own markup, with the row's own actions.

    Before this it showed the model name, the output name and an error — no status, no stats, no
    delete, no download. Now the block renders ``renderJobBody`` (the row's body) inside a
    ``<h3>`` subgroup, so every field the list shows is here too; the three actions are static
    elements driven by ``jobPrintable()`` and routed through the row's delegated handler.
    """
    card = upload_card(page)
    for needle in ('id="uploadLatest"', 'id="uploadLatestBody"', 'id="uploadLatestPrint"',
                   'id="uploadLatestGcode"', 'id="uploadLatestDelete"'):
        assert needle in card, needle

    # a subgroup heading, one level under the card's own <h2 id="uploadTitle">, and the block is a
    # named group so the heading names the region for a screen reader
    assert '<h3 id="uploadLatestTitle">Last job</h3>' in card
    assert '<h2 id="uploadTitle">' in card
    assert 'aria-labelledby="uploadLatestTitle"' in slice_between(card, 'id="uploadLatest"', ">")
    # the heading is the card's existing subgroup rule, not a new type scale
    assert 'class="upload-latest subgroup"' in card

    # one template for both hosts: the card's body is the row's body with the bar suppressed
    # (#uploadWait, directly above the block, already paints the live slice state and takes over
    # for the transfer bar) — so status, stats, output name and error cannot drift apart
    latest = function_body(page, "setUploadLatest")
    assert "renderJobBody(job, false)" in latest
    row = function_body(page, "renderJobRow")
    assert "renderJobBody(j)" in row

    # the actions are the row's three, and Print is gated by the row's own predicate
    assert "jobPrintable(j)" in row and "jobPrintable(job)" in latest
    printable = function_body(page, "jobPrintable")
    assert 'j.status === "sliced" && !j.error' in printable
    assert "print.hidden = !printable" in latest
    assert "gcode.hidden = !printable" in latest
    assert 'gcode.href = printable ? `/api/jobs/${job.id}/gcode` : "#";' in latest
    assert "print.dataset.ref = printable ? job.id : " in latest
    assert "remove.dataset.ref = job.id" in latest

    # re-pointing, not re-parsing: the actions are static markup, so a 2-second poll cannot
    # throw away the keyboard focus resting on Print. The structure that buys that: the only
    # node the poll rewrites is #uploadLatestBody, and the three actions live after it, outside
    # it — so the buttons are never re-created
    assert "body.innerHTML" in latest
    assert "box.innerHTML" not in latest
    rewritten = '<div id="uploadLatestBody"></div>'
    assert rewritten in card            # empty in markup: the poll is its only writer
    body_at = card.index('id="uploadLatestBody"')
    for act in ('id="uploadLatestPrint"', 'id="uploadLatestGcode"', 'id="uploadLatestDelete"'):
        assert card.index(act) > body_at, act

    # the shared body carries every stats field the list shows, so "the card shows the same
    # stats" is one template, not a second list of fields to keep in sync
    shared = function_body(page, "renderJobBody")
    for field in ("time", "filament", "layers", "height", "slice", "scaled"):
        assert field in slice_between(shared, 'class="job-stats"', "</div>"), field

    # the print button is a real button with a name, hidden until the job is sliced
    button = card[card.rindex("<button", 0, card.index('id="uploadLatestPrint"')):]
    button = button[:button.index(">")]
    assert 'type="button"' in button
    assert "hidden" in button
    assert "aria-label=" in button

    # Delete carries no hidden: every job can be thrown away, finished or not
    delete = card[card.rindex("<button", 0, card.index('id="uploadLatestDelete"')):]
    assert "hidden" not in delete[:delete.index(">")]

    # derived from the poll's first row, so a reload mid-slice lands in the same state
    refresh_body = function_body(page, "refresh")
    assert "setUploadLatest(jobs[0])" in refresh_body
    assert re.search(r"setUploadLatest\s*\(\s*null\s*\)", refresh_body.split("catch (e) {")[-1])

    # and it prints and deletes through the same implementations: one delegated handler for the
    # rows and the card, so there is no second copy of printJob(id) or del(id)
    assert '$("#uploadLatest").addEventListener("click", onJobAction);' in page
    assert '$("#jobs").addEventListener("click", onJobAction);' in page
    handler = function_body(page, "onJobAction")
    assert 'ev.target.closest("[data-act]")' in handler
    assert 'btn.dataset.act === "print"' in handler and "printJob(btn.dataset.ref)" in handler
    assert 'btn.dataset.act === "delete"' in handler and "del(btn.dataset.ref)" in handler
    assert 'fetch(`/api/jobs/${id}/print`' in function_body(page, "printJob")
    assert 'fetch(`/api/jobs/${id}`' in function_body(page, "del")


def test_the_last_job_block_renders_the_shared_body_below_a_heading(page):
    """R3/R4 as structure: one body template, two hosts, and actions that the poll never re-parses.

    The point of the split is what it makes impossible — a field added to the list row cannot be
    missing from the card, because there is only one template — and what it preserves: the three
    action elements are static markup outside ``#uploadLatestBody``, so the browser keeps focus
    on Print across the 2-second refresh instead of re-creating it.
    """
    body = function_body(page, "renderJobBody")
    row = function_body(page, "renderJobRow")
    latest = function_body(page, "setUploadLatest")

    # both call sites name the same renderer, with the card suppressing only the bar
    assert "renderJobBody(j)" in row
    assert "renderJobBody(job, false)" in latest
    # ...and the suppression is a template flag, not post-render hiding: nothing in the card's
    # path can emit a .slice-wait for the poll to blank out
    assert "withWait ? wait : \"\"" in body
    assert 'class="slice-wait"' in body

    # the row keeps its own frame and its own three actions; the card has them statically
    assert '<article class="job" id="job-${esc(j.id)}">' in row
    assert 'class="job-actions"' in row and 'class="job-actions"' in upload_card(page)
    assert 'data-act="print"' in row and 'data-act="delete"' in row

    # one delegated listener per host, both pointing at the single handler
    assert page.count('addEventListener("click", onJobAction)') == 2
    handler = function_body(page, "onJobAction")
    assert handler.count("printJob(") == 1 and handler.count("del(") == 1
    # neither host keeps a private inline listener next to the shared one
    for host in ("#jobs", "#uploadLatest"):
        assert f'$("{host}").addEventListener("click", ev =>' not in page, host


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


def test_the_printer_tab_reads_its_url_from_the_always_200_route(page):
    """The loader must not depend on /api/printer, which fails while the printer is offline."""
    body = function_body(page, "loadPrinterUi")
    assert 'fetch("/api/ui")' in body
    # the comment above the call names the old route, so assert on the call, not the word
    assert 'fetch("/api/printer")' not in body
    assert 'p.printer_ui_url' in body


# ---------------------------------------------------------------- per-printer embedded UI url
def test_the_printer_form_offers_a_labelled_web_ui_url_field(page):
    """The address is a property of a printer, so it is edited in the printer form."""
    form = slice_between(page, 'id="printerForm"', "</form>")
    field = slice_between(form, 'id="pfUiUrl"', "</p>")
    assert 'type="url"' in field
    assert 'name="ui_url"' in field
    assert 'aria-describedby="pfUiUrlHint"' in field
    assert 'placeholder="http://' in field

    label = slice_between(form, 'for="pfUiUrl"', "</label>")
    assert "Web UI URL" in label

    hint = slice_between(form, 'id="pfUiUrlHint"', "</p>")
    for needle in ("Printer", "browser", "mixed content", "Moonraker"):
        assert needle in hint, needle
    assert "empty" in hint                       # blank = no embedded UI, not an error

    # it belongs with the connection fields, above the presets
    assert form.index('id="pfUrl"') < form.index('id="pfKey"') < form.index('id="pfUiUrl"')
    assert form.index('id="pfUiUrl"') < form.index('id="pfMachine"')
    # and it is a different control from the Moonraker address, never the same input
    assert 'id="pfUiUrl" name="ui_url"' in form
    assert 'id="pfUrl" name="url"' in form


def test_the_web_ui_field_is_edited_sent_and_cleared(page):
    edit = function_body(page, "editPrinter")
    assert '$("#pfUiUrl").disabled = false' in edit
    assert '$("#pfUiUrl").value = p.ui_url || ""' in edit

    pairing = function_body(page, "editPairing")
    assert '$("#pfUiUrl").disabled = true' in pairing     # the env printer's address is fixed
    assert '$("#pfUiUrl").value = p.ui_url || ""' in pairing

    reset = function_body(page, "resetPrinterForm")
    assert '$("#pfUiUrl").disabled = false' in reset      # form.reset() empties the box

    request = function_body(page, "submitPrinter")
    # always sent, so an emptied box clears the stored address (the API spells that "")
    assert 'ui_url: $("#pfUiUrl").value.trim(),' in request
    branch = slice_between(request, "if (pairingOnly) {", "\n    return;")
    assert "ui_url" not in branch, "the env endpoint rejects the deployment-owned field"


def test_the_printer_list_shows_the_embedded_ui_address_when_set(page):
    render = function_body(page, "renderPrinters")
    line = slice_between(render, "const uiLine =", ': "";')

    assert "p.ui_url" in line
    assert "web UI" in line              # labelled, so it cannot read as the Moonraker URL
    assert 'class="printer-pair"' in line
    assert "${uiLine}" in render
    assert render.index("printer-url mono") < render.index("${uiLine}")

    # a long address wraps inside its row instead of widening the list
    style = page[page.index("<style>"):page.index("</style>")]
    assert re.search(r"\n\.printer-pair \.mono\s*\{[^}]*overflow-wrap:\s*anywhere", style)


def test_switching_the_active_printer_reframes_the_tab(page):
    """Same page load, different active printer: the tab has to show the new printer's UI.

    The once-per-load flag is gone; what is left is a "same address as on screen" guard, so an
    unchanged visit (and the 2s poll, which does not run on this view) rebuilds nothing, while
    a switch repoints the existing frame instead of reloading the whole section.
    """
    assert "let framedUiUrl = null;" in page
    assert "printerUiLoaded" not in page

    body = function_body(page, "loadPrinterUi")
    assert 'fetch("/api/ui")' in body
    # nothing returns before the fetch: the previous printer's answer is not this one's
    assert "return" not in body[:body.index('fetch("/api/ui")')]
    assert "if (uiUrl === framedUiUrl) return;" in body          # unchanged visit: no reload
    assert body.index("if (uiUrl === framedUiUrl) return;") < body.index("framedUiUrl = uiUrl;")
    assert body.index("framedUiUrl = uiUrl;") < body.index("renderPrinterUi(uiUrl);")

    # a failed read keeps whatever is framed and retries on the next visit
    assert body.index("if (!r.ok) return;") < body.index("if (uiUrl === framedUiUrl) return;")

    render = function_body(page, "renderPrinterUi")
    assert "const frame = mount.firstElementChild;" in render
    assert "if (frame) { frame.src = uiUrl; return; }" in render   # repoint, never re-clone
    assert render.index("const frame = mount.firstElementChild;") < render.index("cloneNode")


# ---------------------------------------------------------------- support fins: upload
def test_the_upload_card_offers_the_fins_checkbox_off_by_default(page):
    card = upload_card(page)
    start = card.rindex("<input", 0, card.index('id="supportFins"'))
    tag = card[start:card.index(">", start)]

    assert 'id="supportFins"' in tag
    assert 'name="support_fins"' in tag
    assert 'type="checkbox"' in tag
    assert 'value="true"' in tag
    assert "checked" not in tag                       # forced landing, opt-in feature
    assert "Add designed-in support fins" in card and "(off by default)" in card
    # the hint says what it costs: the upload is finned locally, the original is untouched
    assert "leaves the original file untouched" in card


def test_the_fins_flag_is_sent_only_when_the_box_is_ticked(page):
    body = function_body(page, "upload")

    assert 'const fins = !!$("#supportFins")?.checked;' in body
    assert 'if (fins) fd.append("support_fins", "true");' in body
    assert body.index('const fins = !!$("#supportFins")?.checked;') < \
        body.index('fd.append("support_fins", "true")')
    # a finning upload takes long enough to deserve its own wording before the bar moves
    assert "Adding support fins: ${file.name}" in body


def test_a_staged_upload_stays_on_the_card_instead_of_opening_the_preview(page):
    """Fins stay staged, but the dialog must not open itself.

    Preview and Slice it on the card are the decision. Calling /slice from upload() would queue
    the model before the operator chose.
    """
    body = function_body(page, "upload")
    branch = slice_between(body, 'if (j.status === "awaiting_confirm") {', "return;")

    assert "setUploadWait(null)" in branch            # keep the slicer bar for the real queue
    assert "setUploadLatest(j)" in branch
    assert "openPreview(" not in branch
    assert "refresh();" in branch
    assert "/slice" not in body                       # nothing is queued from an upload
    # and it is checked before the queued path takes over
    assert body.index('if (j.status === "awaiting_confirm")') < \
        body.index('setUploadWait({status: "queued"')


# ---------------------------------------------------------------- support fins: dialog
def preview_dialog(page: str) -> str:
    return slice_between(page, '<dialog id="previewDlg"', "</dialog>")


def test_the_preview_dialog_is_a_labelled_modal_with_a_fallback(page):
    dlg = preview_dialog(page)

    assert 'class="dialog"' in dlg
    assert 'aria-labelledby="previewTitle"' in dlg
    assert 'aria-describedby="previewHint"' in dlg
    assert '<h2 id="previewTitle">Support fins preview</h2>' in dlg
    assert 'id="previewHint"' in dlg and 'class="hint"' in dlg

    canvas = dlg[dlg.rindex("<canvas", 0, dlg.index('id="previewCanvas"')):]
    canvas = canvas[:canvas.index(">")]
    assert 'role="img"' in canvas                     # no pixels without a description
    assert 'tabindex="0"' in canvas                   # keyboard orbit needs focus
    assert "Drag to rotate" in canvas and "arrow keys to rotate" in canvas

    assert '<p id="previewFallback" hidden></p>' in dlg   # the no-canvas path, inert until used
    assert 'role="group" aria-label="Objects in the preview"' in dlg
    assert 'id="previewError" class="err" role="alert" hidden' in dlg


def test_the_dialog_actions_put_the_decision_last(page):
    dlg = preview_dialog(page)
    order = ['id="previewDiscard"', 'id="previewClose"', 'id="previewConfirm"']

    assert [dlg.index(needle) for needle in order] == sorted(dlg.index(n) for n in order)
    for needle, label in zip(order, ("Discard", "Keep for later", "Slice it")):
        start = dlg.rindex("<button", 0, dlg.index(needle))
        tag = dlg[start:dlg.index(">", start)]
        assert 'type="button"' in tag, needle          # inside a dialog: never a submit
        assert label in dlg[dlg.index(needle):dlg.index("</button>", dlg.index(needle))]
    confirm = dlg[dlg.rindex("<button", 0, dlg.index('id="previewConfirm"')):]
    assert "btn-primary" in confirm[:confirm.index(">")]


def test_the_dialog_fits_a_phone_and_keeps_the_decision_visible(page):
    """A 3-D canvas needs the screen; the three actions must not scroll away from it."""
    css = slice_between(page, "#previewDlg{", ".preview-key{")
    assert "max-width:96vw" in css
    assert "max-height:92vh" in css
    assert "height:min(60vh,520px)" in css
    assert "touch-action:none" in page                 # a drag on the canvas is not a page scroll
    assert "position:sticky; bottom:0" in page
    mobile = slice_between(page, "@media (max-width:640px){", ".preview-actions .btn{")
    assert "height:100dvh" in mobile                   # the phone layout is the whole screen
    assert ".preview-actions .btn{ flex:1 1 100%; }" in page


def test_the_legend_is_one_real_toggle_per_drawable_object(page):
    chrome = function_body(page, "renderPreviewChrome")

    assert "legend.replaceChildren();" in chrome       # rewritten per payload, not appended to
    assert 'document.createElement("button")' in chrome
    assert "key.type = \"button\";" in chrome
    assert "key.dataset.previewObject = previewObjectId(o);" in chrome
    assert 'key.setAttribute("aria-pressed", "true");' in chrome
    # an object with no triangles is not offered: an empty toggle would hide nothing
    assert "if (!Array.isArray(o.triangles) || o.triangles.length < 3) continue;" in chrome
    assert "key.textContent = `${previewObjectName(o)} · ${count}`;" in chrome

    toggle = function_body(page, "previewToggle")
    assert 'ev.target.closest("[data-preview-object]")' in toggle
    assert "previewHidden.add(id)" in toggle and "previewHidden.delete(id)" in toggle
    assert 'key.setAttribute("aria-pressed", previewHidden.has(id) ? "false" : "true");' in toggle


def test_the_stats_line_names_the_fins_the_objects_and_the_size(page):
    chrome = function_body(page, "renderPreviewChrome")

    assert 'fins.finTriangles ? `${fins.finTriangles} fin triangles` : "no fin geometry"' in chrome
    assert '`${objects.length} object${objects.length === 1 ? "" : "s"}`' in chrome
    assert 'summary.push(`${size.map(fmtMm).join("×")} mm`);' in chrome
    assert 'stats.textContent = summary.join(" · ");' in chrome


def test_the_preview_colours_the_supports_and_budgets_one_frame(page):
    """The fins are the reason the dialog exists, so they are the only saturated colour."""
    assert 'supports: [34, 197, 94]' in page
    assert 'part: [148, 163, 184]' in page
    assert "const PREVIEW_TRIANGLE_BUDGET = 200000;" in page
    draw = function_body(page, "drawPreview")
    assert "let budget = PREVIEW_TRIANGLE_BUDGET;" in draw
    assert "previewHidden.has(id)" in draw             # a toggled-off object is skipped, not drawn


def test_the_view_repaints_on_interaction_only(page):
    """"Nothing animates while the dialog sits open" is the promise; a rAF loop would break it."""
    schedule = function_body(page, "schedulePreview")

    assert "if (previewFrame) return;" in schedule     # one frame per burst
    assert "requestAnimationFrame(() => { previewFrame = 0; drawPreview(); })" in schedule
    assert "prefersReducedMotion()" in schedule         # drawn synchronously, no frame to cancel
    assert "setInterval" not in page.split("support-fins preview")[1][:12000]
    # the close path cancels a pending frame instead of letting it paint a torn-down scene
    assert "cancelAnimationFrame(previewFrame)" in function_body(page, "initPreview")


# ---------------------------------------------------------------- support fins: flow
def test_a_preview_failure_still_leaves_the_job_queueable(page):
    err = function_body(page, "showPreviewError")

    assert "The job is still staged" in err
    assert '$("#previewConfirm").disabled = false;' in err   # Slice it survives a dead preview
    assert '$("#previewCanvas").hidden = true;' in function_body(page, "previewFallbackText")

    open_ = function_body(page, "openPreview")
    assert 'fetch(`/api/jobs/${encodeURIComponent(id)}/preview`)' in open_
    assert "if (!r.ok) throw new Error(p.detail || r.statusText" in open_
    assert "showPreviewError(e.message)" in open_            # no unhandled rejection in the dialog


def test_slice_it_queues_exactly_once_and_hands_the_card_back(page):
    body = function_body(page, "confirmSlice")

    assert 'fetch(`/api/jobs/${encodeURIComponent(id)}/slice`, { method: "POST" })' in body
    assert 'if (btn.dataset.busy === "true") return;' in body   # a double click queues once
    assert "btn.disabled = true;" in body
    # close, then repaint the card from the response, so the wait bar is right before the poll
    assert body.index("closePreview();") < body.index("setUploadWait(job)")
    assert "setUploadLatest(job)" in body
    assert 'toast(j.queue_position ? `Queued for slice (#${j.queue_position})`' in body
    assert '"Could not queue the slice: " + e.message' in body


def test_closing_keeps_the_job_and_discarding_asks_first(page):
    close = function_body(page, "closePreview")
    assert "previewJob = null;" in close
    assert "fetch(" not in close                        # Escape must not delete anything
    assert 'if (typeof dlg.close === "function")' in close

    discard = function_body(page, "discardPreview")
    assert 'await ask("Discard this staged job?"' in discard
    assert "if (!ok) return;" in discard
    assert "method: \"DELETE\"" in discard
    assert "refresh();" in discard


def test_the_last_job_block_offers_preview_and_slice_for_a_staged_job(page):
    """A finned upload lands on the card with both actions, and neither fires by itself.

    Both buttons are static markup (the poll only re-aims them) and share the row handler, so
    there is one preview path and one slice path.
    """
    card = upload_card(page)
    tag = card[card.rindex("<button", 0, card.index('id="uploadLatestPreview"')):]
    tag = tag[:tag.index(">")]
    assert 'data-act="preview"' in tag
    assert "hidden" in tag                              # only a staged job shows it
    assert "aria-label=" in tag
    assert "Preview fins" in card[card.index('id="uploadLatestPreview"'):]
    slice_tag = card[card.rindex("<button", 0, card.index('id="uploadLatestSlice"')):]
    slice_tag = slice_tag[:slice_tag.index(">")]
    assert 'data-act="slice"' in slice_tag
    assert "btn-primary" in slice_tag
    assert "hidden" in slice_tag
    assert "aria-label=" in slice_tag
    assert card.index('id="uploadLatestPreview"') < card.index('id="uploadLatestSlice"')
    assert "Slice it" in card[card.index('id="uploadLatestSlice"'):card.index('id="uploadLatestReslice"')]

    latest = function_body(page, "setUploadLatest")
    assert 'const staged = job.status === "awaiting_confirm" && !job.error;' in latest
    assert "preview.hidden = !staged;" in latest
    assert 'preview.dataset.ref = staged ? job.id : "";' in latest
    assert "preview.setAttribute(\"aria-label\", `Preview the support fins on ${name}`);" in latest
    assert 'const sliceable = staged && job.kind !== "reslice";' in latest
    assert "slice.hidden = !sliceable;" in latest
    assert 'slice.dataset.ref = sliceable ? job.id : "";' in latest

    handler = function_body(page, "onJobAction")
    assert 'btn.dataset.act === "preview"' in handler
    assert "openPreview(btn.dataset.ref)" in handler
    assert 'btn.dataset.act === "slice"' in handler
    assert "confirmSlice(btn.dataset.ref)" in handler
    assert '$("#uploadLatest").addEventListener("click", onJobAction);' in page
    assert page.count("/preview`") == 1                 # one dialog, one request path


def test_a_staged_row_offers_both_preview_and_slice_it(page):
    row = function_body(page, "renderJobRow")
    staged = slice_between(row, "const staged =", ': "";')

    assert 'data-act="preview"' in staged
    assert 'data-act="slice"' in staged
    assert staged.index('data-act="preview"') < staged.index('data-act="slice"')
    assert "Preview fins" in staged and "Slice it" in staged
    # a failed or finished job gets neither: neither can be reviewed or queued any more
    assert 'j.status === "awaiting_confirm" && !j.error' in row


def test_the_preview_wires_every_input_and_tears_down_on_close(page):
    init = function_body(page, "initPreview")

    for listener in ('canvas.addEventListener("pointerdown", previewOrbit)',
                     'canvas.addEventListener("pointermove", previewOrbit)',
                     'canvas.addEventListener("pointerup", previewOrbit)',
                     'canvas.addEventListener("pointercancel", previewOrbit)',
                     'canvas.addEventListener("wheel", previewZoom, { passive: false })',
                     'canvas.addEventListener("keydown", previewKey)',
                     '$("#previewLegend").addEventListener("click", previewToggle)',
                     '$("#previewConfirm").addEventListener("click", () => withPreviewBusy(',
                     '$("#previewClose").addEventListener("click", keepPreview)',
                     '$("#previewDiscard").addEventListener("click", () => withPreviewBusy('):
        assert listener in init, listener
    assert 'dlg.addEventListener("close", () => {' in init
    assert "previewScene = null;" in init               # Escape and the native close land here
    assert "if (previewScene) schedulePreview();" in init   # a resize never paints a dead scene
    assert "initPreview();" in page

    orbit = function_body(page, "previewOrbit")
    assert "canvas.setPointerCapture(ev.pointerId)" in orbit
    assert 'if (ev.button) return;' in orbit            # right-drag is not an orbit
    assert "previewView.pitch = Math.max(-1.45, Math.min(1.45" in orbit   # no flipping over
    keys = function_body(page, "previewKey")
    assert 'key === "ArrowLeft"' in keys and 'key === "0" || key === "Home"' in keys
    assert "ev.preventDefault();" in keys
    zoom = function_body(page, "previewZoom")
    assert "ev.preventDefault();" in zoom and "Math.max(0.35, Math.min(4," in zoom


# ---------------------------------------------------------------- reslice staged editor
def test_reslice_actions_are_capability_gated_and_drafts_resume(page):
    row = function_body(page, "renderJobRow")
    assert 'j.reslice?.eligible' in row
    assert 'data-act="reslice"' in row
    assert 'Resume reslice' in row and 'j.kind === "reslice"' in row
    assert 'source_job_id' in function_body(page, "renderJobBody")
    assert 'uploadLatestReslice' in function_body(page, "setUploadLatest")


def test_reslice_uses_idempotent_token_and_local_revisioned_mutations(page):
    create = function_body(page, "createReslice")
    assert 'resliceTokens.get(id) || newRequestId()' in create
    assert 'reslicePending.has(id)' in create
    assert 'request_id:token' in create
    save = function_body(page, "saveResliceSettings")
    assert '/settings`' in save and 'revision:previewJob.revision' in save
    assert 'method:"PATCH"' in save
    assert '/api/profile/select' not in save and '/api/profile/options' not in save
    assert 'revision:previewJob.revision' in function_body(page, "confirmSlice")
    assert 'revision:previewJob.revision' in function_body(page, "resetPreviewScale")


def test_reslice_scale_submits_only_last_edited_axis(page):
    follow = function_body(page, "previewScaleFollow")
    apply = function_body(page, "applyPreviewScale")
    assert 'previewAxis = src' in follow
    assert 'const axis = previewAxis' in apply
    assert '[apiAxis === "length" ? "depth" : apiAxis]:value' in apply
    assert 'Number.isFinite(value)' in apply
    assert 'filled.length' not in apply


def test_reslice_settings_preview_and_errors_are_independent(page):
    dlg = preview_dialog(page)
    for id_ in ("resliceMachine", "resliceProcess", "resliceFilament", "resliceSupports", "resliceBrim", "resliceOrient", "resliceFit", "resliceCopy", "resliceSave", "scaleReset"):
        assert f'id="{id_}"' in dlg
    assert 'role="alert"' in dlg
    open_ = function_body(page, "openPreview")
    assert 'previewJob = job' in open_
    assert 'p.job ||' not in open_
    assert 'settings can still be saved and sliced' in open_
    assert 'previewReturnFocus' in function_body(page, "closePreview")


def test_reslice_keep_saves_before_close_and_recovers_stale_edits(page):
    """Exercise the actual async handlers, including a delayed request and stale revision."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    names = ("withPreviewBusy", "rememberDimensions", "resliceValues", "saveResliceSettings",
             "keepPreview", "reloadResliceRevision", "previewActionError")
    functions = []
    for name in names:
        body = function_body(page, name) + "\n}"
        if name in ("withPreviewBusy", "saveResliceSettings", "keepPreview", "reloadResliceRevision"):
            body = "async " + body
        functions.append(body)
    js = r'''
const assert = require('assert');
const elements = {};
for (const id of ['previewDlg','previewBusy','resliceMachine','resliceProcess','resliceFilament',
 'resliceSupports','resliceBrim','resliceOrient','resliceFit','reslicePresetError','resliceReload',
 'previewError','scaleHeight','scaleWidth','scaleLength']) {
 elements[id] = {value:'', checked:false, hidden:true, textContent:'', inert:false, focus(){},
 setAttribute(){}, removeAttribute(){}};
}
elements.previewDlg.children = [elements.resliceMachine, elements.previewBusy];
const $ = s => elements[s.slice(1)];
const document = {getElementById: id => elements[id]};
let previewBusy = false, savedResliceValues = null, previewAxis = 'scaleWidth';
let previewJob = {id:'abc', kind:'reslice', status:'awaiting_confirm', revision:0};
const pendingDimensions = new Map();
let closed = 0;
function closePreview() { closed++; }
''' + "\n".join(functions) + r'''
(async () => {
 for (const id of ['resliceMachine','resliceProcess','resliceFilament']) elements[id].value = id;
 savedResliceValues = JSON.stringify(resliceValues());
 elements.resliceBrim.checked = true; elements.scaleWidth.value = '80';
 let resolveFetch;
 global.fetch = () => new Promise(resolve => { resolveFetch = resolve; });
 const closing = keepPreview();
 assert.equal(previewBusy, true);
 await keepPreview(); assert.equal(closed, 0);
 resolveFetch({ok:false, status:409, json:async()=>({detail:'stale revision'})});
 await closing;
 assert.equal(closed, 0); assert.equal(elements.resliceBrim.checked, true);
 assert.equal(elements.resliceReload.hidden, false); assert.match(elements.previewError.textContent, /not closed/);
 assert.equal(previewBusy, false);
 global.fetch = async () => ({ok:true, json:async()=>({id:'abc',kind:'reslice',status:'awaiting_confirm',revision:4})});
 await reloadResliceRevision();
 assert.equal(previewJob.revision, 4); assert.equal(elements.resliceBrim.checked, true);
 let body;
 global.fetch = async (url, options) => {body=JSON.parse(options.body); return {ok:true, json:async()=>({id:'abc',kind:'reslice',revision:5})};};
 await keepPreview();
 assert.equal(body.revision, 4); assert.equal(body.options.brim, true); assert.equal(closed, 1);
 assert.deepEqual(pendingDimensions.get('abc'), {axis:'scaleWidth', values:['','80','']});
})().catch(e => {console.error(e); process.exit(1);});
'''
    subprocess.run([node, "-e", js], check=True, capture_output=True, text=True)


def test_reslice_cancel_and_mutations_are_guarded(page):
    init = function_body(page, "initPreview")
    assert 'ev.preventDefault(); if (!previewBusy) keepPreview();' in init
    for label in ("Loading dashboard settings", "Loading compatible presets", "Applying size", "Resetting size", "Queuing slice", "Discarding draft"):
        assert label in init
    assert 'resliceResponseError(r, j)' in function_body(page, "applyPreviewScale")
    assert 'resliceResponseError(r, result)' in function_body(page, "resetPreviewScale")
    assert 'pendingDimensions.get(id)' in function_body(page, "openPreview")
    assert "Apply the entered size or clear it before slicing" in function_body(page, "confirmSlice")


def test_create_reslice_restores_focus_after_refresh_replaces_trigger(page):
    """Run the real create/open/close handlers with a refresh that detaches the trigger."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    functions = []
    for name in ("createReslice", "openPreview", "closePreview"):
        functions.append(("async " if name != "closePreview" else "") + function_body(page, name) + "\n}")
    js = r'''
const assert = require('assert');
const body = {isConnected:true, focus(){ throw Error('body should not receive focus'); }};
let focused;
const heading = {isConnected:true, focus(){ focused = 'heading'; }};
const button = {isConnected:true, disabled:false, textContent:'', focus(){ focused = 'button'; }};
const elements = new Proxy({}, {get(target, id) {
 if (!target[id]) target[id] = {hidden:false, textContent:'', replaceChildren(){}, focus(){},
  showModal(){this.open=true}, close(){this.open=false}, setAttribute(){}, removeAttribute(){}};
 return target[id];
}});
const $ = s => s === '#jobsTitle' ? heading : elements[s.slice(1)];
const document = {activeElement:button, getElementById: id => elements[id]};
const resliceTokens = new Map(), reslicePending = new Set();
const newRequestId = () => 'token';
let previewBusy = false, previewReturnFocus, previewJob, previewAxis, previewScene, previewDrag, previewHidden, previewView;
const withPreviewBusy = async (_, action) => action();
const refresh = async () => {button.isConnected = false; document.activeElement = body;};
const toast = () => {};
const showPreviewError = () => {};
global.fetch = async (url, opts) => opts ? {ok:true, json:async()=>({id:'child',status:'awaiting_confirm'})}
 : {ok:false, statusText:'unavailable', json:async()=>({})};
''' + "\n".join(functions) + r'''
(async () => {
 await createReslice('source', button);
 assert.equal(previewReturnFocus, button);
 assert.equal(elements.previewDlg.open, true);
 closePreview();
 assert.equal(focused, 'heading');
 button.isConnected = true;
 document.activeElement = button;
 await openPreview('child');
 closePreview();
 assert.equal(focused, 'button');
})().catch(e => {console.error(e); process.exit(1);});
'''
    subprocess.run([node, "-e", js], check=True, capture_output=True, text=True)
