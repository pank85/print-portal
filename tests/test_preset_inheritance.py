import io
import json
from pathlib import Path

import pytest

import profiles as prof


def put(root, vendor, kind, name, **values):
    path = root / vendor / kind / (name + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'name': name, 'type': kind, **values}))
    return path


def test_nested_cross_root_merge_and_source_unchanged(tmp_path):
    datadir, resources = tmp_path / 'data', tmp_path / 'resources'
    grand = put(resources, 'Vendor', 'filament', 'grand', filament_max_volumetric_speed=['12'], x='root')
    put(resources, 'Vendor', 'filament', 'parent', inherits='grand', x='parent')
    child = put(datadir, 'Custom', 'filament', 'child', inherits='parent', y='child')
    before = child.read_bytes(), grand.read_bytes()
    result = prof.effective_preset(child, 'filament', datadir, resources)
    assert result['filament_max_volumetric_speed'] == ['12']
    assert result['x'] == 'parent' and result['y'] == 'child'
    assert 'inherits' not in result
    assert (child.read_bytes(), grand.read_bytes()) == before


def test_missing_cycle_and_duplicate_fail_closed(tmp_path):
    root = tmp_path / 'data'
    child = put(root, 'Custom', 'machine', 'child', inherits='parent')
    with pytest.raises(prof.ProfileError, match='missing machine parent'):
        prof.effective_preset(child, 'machine', root, tmp_path / 'resources')
    put(root, 'Vendor1', 'machine', 'parent', inherits='child')
    with pytest.raises(prof.ProfileError, match='cyclic'):
        prof.effective_preset(child, 'machine', root, tmp_path / 'resources')
    put(root, 'Vendor2', 'machine', 'parent')
    with pytest.raises(prof.ProfileError, match='ambiguous'):
        prof.effective_preset(child, 'machine', root, tmp_path / 'resources')


def test_effective_machine_clamps_inherited_area(tmp_path):
    root = tmp_path / 'data'
    put(root, 'Vendor', 'machine', 'parent', printable_area=['0x0', '300x0', '300x300', '0x300'])
    child = put(root, 'Custom', 'machine', 'child', inherits='parent')
    data = prof.effective_preset(child, 'machine', root, tmp_path / 'resources')
    prof.clamp_machine(data, (220, 220))
    assert data['printable_area'] == ['0x0', '220x0', '220x220', '0x220']


def test_local_parent_precedence_and_empty_override(tmp_path, monkeypatch):
    datadir, resources = tmp_path / 'data', tmp_path / 'resources'
    put(resources, 'Vendor', 'process', 'common', brim_width='7', compatible_printers=['other'])
    put(resources, 'Other', 'process', 'common', brim_width='99')
    parent = put(datadir, 'Custom', 'process', 'common', brim_width='8', compatible_printers=['other'])
    child = put(datadir, 'Custom', 'process', 'child', inherits='common', compatible_printers=[])
    monkeypatch.setattr(prof, 'ORCA_PROFILES', resources)
    with pytest.raises(prof.ProfileError, match='ambiguous'):
        prof.effective_preset(child, 'process', datadir)
    (resources / 'Vendor/process/common.json').unlink()
    (resources / 'Other/process/common.json').unlink()
    result = prof.effective_preset(child, 'process', datadir)
    assert result['brim_width'] == '8' and result['compatible_printers'] == []
    row = prof.summarise(child, 'process', datadir)
    assert row['compatible_printers'] == [] and row['brim_width'] == '8'
    assert prof.filter_process_by_machine([row], {'name': 'machine'}) == [row]
    assert prof.effective_process(child, datadir, {'brim': True})['brim_width'] == '8'
    parent.unlink()
    put(resources, 'Vendor', 'process', 'common', brim_width='7')
    put(resources, 'Other', 'process', 'common', brim_width='99')
    row = prof.summarise(child, 'process', datadir)
    assert 'ambiguous' in row['error']
    assert prof.filter_process_by_machine([row], {'name': 'machine'}) == []
    assert 'ambiguous' in prof.effective_process(child, datadir, {'brim': True})['error']
    assert 'ambiguous' in prof.preset_view(datadir, 'process', child)['error']


def test_nested_vendor_parent_precedes_other_vendors(tmp_path):
    root = tmp_path / 'resources'
    put(root, 'Vendor', 'filament', 'common', value='correct')
    put(root, 'Other', 'filament', 'common', value='wrong')
    nested = root / 'Vendor' / 'filament' / 'base' / 'child.json'
    nested.parent.mkdir(parents=True)
    nested.write_text(json.dumps({'name': 'child', 'inherits': 'common'}))
    assert prof.effective_preset(nested, 'filament', tmp_path / 'data', root)['value'] == 'correct'
    (root / 'Vendor/filament/common.json').unlink()
    with pytest.raises(prof.ProfileError, match='missing filament parent'):
        prof.effective_preset(nested, 'filament', tmp_path / 'data', root)


def test_security_rejects_escape_malformed_parent_and_depth(tmp_path):
    root = tmp_path / 'data'
    outside = tmp_path / 'outside.json'
    outside.write_text(json.dumps({'name': 'parent', 'secret': 'not a preset'}))
    child = put(root, 'Custom', 'machine', 'child', inherits='parent')
    (child.parent / 'parent.json').symlink_to(outside)
    with pytest.raises(prof.ProfileError, match='missing machine parent'):
        prof.effective_preset(child, 'machine', root, tmp_path / 'resources')
    with pytest.raises(prof.ProfileError, match='escapes'):
        prof.effective_preset(child.parent / 'parent.json', 'machine', root, tmp_path / 'resources')
    (child.parent / 'parent.json').unlink()
    (child.parent / 'parent.json').write_text('{broken')
    with pytest.raises(prof.ProfileError, match='missing machine parent'):
        prof.effective_preset(child, 'machine', root, tmp_path / 'resources')
    (child.parent / 'parent.json').unlink()
    child.write_text(json.dumps({'name': 'child', 'inherits': '../../outside'}))
    with pytest.raises(prof.ProfileError, match='missing machine parent'):
        prof.effective_preset(child, 'machine', root, tmp_path / 'resources')
    for n in range(130):
        put(root, 'Custom', 'machine', f'p{n}', inherits=f'p{n+1}' if n < 129 else '')
    with pytest.raises(prof.ProfileError, match='too deep'):
        prof.effective_preset(root / 'Custom/machine/p0.json', 'machine', root, tmp_path / 'resources')


def test_import_warns_and_does_not_auto_select_incomplete(tmp_path, monkeypatch):
    import main
    root = tmp_path / 'data'
    monkeypatch.setattr(prof, 'ORCA_PROFILES', tmp_path / 'resources')
    blob = json.dumps({'type': 'filament', 'name': 'child', 'inherits': 'missing'}).encode()
    result = prof.import_into(root, blob, 'child.json')
    row = result['imported'][0]
    assert 'missing filament parent' in row['error']
    assert any('import the correct parent' in note for note in result['notes'])
    assert main.selection_from_import(result) == {}
    assert json.loads((root / row['path']).read_text())['inherits'] == 'missing'
    put(root, 'Custom', 'filament', 'missing', filament_max_volumetric_speed=['14'])
    assert prof.effective_preset(root / row['path'], 'filament', root)['filament_max_volumetric_speed'] == ['14']


def test_missing_or_invalid_snapshot_never_uses_live_profile(monkeypatch):
    import main
    monkeypatch.setattr(main, 'active_paths', lambda: pytest.fail('live profile used'))
    monkeypatch.setattr(main, '_preset_path', lambda rel: None)
    for snapshot in ({'machine': 'removed', 'process': 'p', 'filament': 'f'}, 'invalid', None):
        with pytest.raises(prof.ProfileError, match='pinned'):
            main.slice_profile({'profile_snapshot': snapshot})


def test_incomplete_and_incompatible_import_preserves_existing_selection(client, datadir, monkeypatch, tmp_path):
    import dataclasses
    import main
    profile = tmp_path / 'profile.json'
    monkeypatch.setattr(main, 'CONFIG', dataclasses.replace(main.CONFIG, orca_datadir=datadir, profile_file=profile))
    original = {k: str(v.relative_to(datadir)) for k, v in main.partial_selection().items()}
    profile.write_text(json.dumps(original))
    incomplete = {'type': 'machine', 'name': 'broken machine', 'inherits': 'no such parent'}
    response = client.post('/api/profile/upload', files={'file': ('broken.json', json.dumps(incomplete), 'application/json')})
    assert response.status_code == 200
    assert any('previous selection' in note for note in response.json()['notes'])
    assert json.loads(profile.read_text()) == original
    incompatible = {'type': 'machine', 'name': 'different machine'}
    response = client.post('/api/profile/upload', files={'file': ('different.json', json.dumps(incompatible), 'application/json')})
    assert response.status_code == 200
    assert any('not selected' in note for note in response.json()['notes'])
    assert json.loads(profile.read_text()) == original


def test_import_cannot_make_selected_parent_ambiguous(client, monkeypatch, tmp_path):
    import dataclasses
    import main
    datadir, resources = tmp_path / 'data', tmp_path / 'resources'
    machine = put(datadir, 'system/Custom', 'machine', 'active', inherits='protected-parent', instantiation='true')
    put(resources, 'Vendor', 'machine', 'protected-parent', printable_height='200')
    monkeypatch.setattr(prof, 'ORCA_PROFILES', resources)
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'machine': str(machine.relative_to(datadir))}))
    monkeypatch.setattr(main, 'CONFIG', dataclasses.replace(main.CONFIG, orca_datadir=datadir, profile_file=profile, machine_slot=None))
    before = prof.effective_preset(machine, 'machine', datadir)
    parent = {'type': 'machine', 'name': 'protected-parent', 'printable_height': '999'}
    response = client.post('/api/profile/upload', files={'file': ('parent.json', json.dumps(parent), 'application/json')})
    assert response.status_code == 400
    assert 'selected machine chain' in response.text
    assert prof.effective_preset(machine, 'machine', datadir) == before
    assert not (datadir / 'system/Custom/machine/protected-parent.json').exists()


def test_import_cannot_overwrite_selected_safe_stem_alias(client, monkeypatch, tmp_path):
    import dataclasses
    import main
    datadir = tmp_path / 'data'
    child = put(datadir, 'system/Custom', 'machine', 'A_B', instantiation='true', setting_id='same')
    original = json.loads(child.read_text())
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'machine': str(child.relative_to(datadir))}))
    monkeypatch.setattr(prof, 'ORCA_PROFILES', tmp_path / 'resources')
    monkeypatch.setattr(main, 'CONFIG', dataclasses.replace(main.CONFIG, orca_datadir=datadir, profile_file=profile, machine_slot=None))
    changed = {'type': 'machine', 'name': 'A B', 'setting_id': 'same', 'printer_model': 'changed'}
    response = client.post('/api/profile/upload', files={'file': ('alias.json', json.dumps(changed), 'application/json')})
    assert response.status_code == 400
    assert json.loads(child.read_text()) == original


def test_repaired_first_machine_can_be_selected(client, monkeypatch, tmp_path):
    import dataclasses
    import main
    datadir = tmp_path / 'empty'
    datadir.mkdir()
    monkeypatch.setattr(main, 'CONFIG', dataclasses.replace(main.CONFIG, orca_datadir=datadir, profile_file=tmp_path / 'profile.json', machine_slot=None))
    monkeypatch.setattr(prof, 'ORCA_PROFILES', tmp_path / 'resources')
    machine = {'type': 'machine', 'name': 'repair child', 'inherits': 'repair parent'}
    response = client.post('/api/profile/upload', files={'file': ('child.json', json.dumps(machine), 'application/json')})
    assert response.json()['imported'][0].get('error')
    parent = {'type': 'machine', 'name': 'repair parent'}
    assert client.post('/api/profile/upload', files={'file': ('parent.json', json.dumps(parent), 'application/json')}).status_code == 200
    rel = response.json()['imported'][0]['path']
    selected = client.post('/api/profile/select', json={'machine': rel})
    assert selected.status_code == 200, selected.text
    assert main.partial_selection()['machine'] == datadir / rel


def test_explicit_binding_selects_duplicate_parent_without_guessing(tmp_path):
    datadir, resources = tmp_path / 'data', tmp_path / 'resources'
    ginger = put(resources, 'Ginger Additive', 'machine', 'fdm_machine_common', marker='ginger')
    put(resources, 'Z-Bolt', 'machine', 'fdm_machine_common', marker='zbolt')
    child = put(datadir, 'Custom', 'machine', 'imported', inherits='fdm_machine_common')
    with pytest.raises(prof.ProfileError, match='ambiguous'):
        prof.effective_preset(child, 'machine', datadir, resources)
    listed = prof.list_parent_candidates(child, 'machine', datadir, resources)
    assert 'recommended' not in listed
    chosen = next(item for item in listed['candidates'] if item['id'].startswith('resources:Ginger'))
    prof.bind_parent(datadir, resources, listed['child'], chosen['id'], 'fdm_machine_common', chosen['sha256'])
    assert prof.effective_preset(child, 'machine', datadir, resources)['marker'] == 'ginger'
    ginger.write_text(ginger.read_text().replace('ginger', 'changed'))
    with pytest.raises(prof.ProfileError, match='changed'):
        prof.effective_preset(child, 'machine', datadir, resources)


def test_parent_changed_after_digest_is_rejected_not_sliced(tmp_path, monkeypatch):
    datadir, resources = tmp_path / 'data', tmp_path / 'resources'
    parent = put(resources, 'Vendor', 'machine', 'fdm_machine_common', marker='verified')
    put(resources, 'Other', 'machine', 'fdm_machine_common', marker='other')
    child = put(datadir, 'Custom', 'machine', 'imported', inherits='fdm_machine_common')
    listed = prof.list_parent_candidates(child, 'machine', datadir, resources)
    chosen = next(item for item in listed['candidates'] if item['id'].startswith('resources:Vendor'))
    prof.bind_parent(datadir, resources, listed['child'], chosen['id'], 'fdm_machine_common', chosen['sha256'])
    original = parent.read_bytes()
    changed = json.dumps({'name': 'fdm_machine_common', 'type': 'machine', 'marker': 'sliced-changed',
                          'filament_max_volumetric_speed': ['2']}).encode()
    parent.write_bytes(changed)
    with pytest.raises(prof.ProfileError, match='changed'):
        prof.effective_preset(child, 'machine', datadir, resources)
    reads = {'n': 0}
    real_open = Path.open

    def open_parent(self, mode='r', *args, **kwargs):
        if self.resolve() == parent.resolve():
            reads['n'] += 1
            payload = original if reads['n'] == 1 else changed
            return io.BytesIO(payload) if 'b' in mode else io.StringIO(payload.decode())
        return real_open(self, mode, *args, **kwargs)

    parent.write_bytes(original)
    monkeypatch.setattr(Path, 'open', open_parent)
    result = prof.effective_preset(child, 'machine', datadir, resources)
    assert result['marker'] == 'verified'
    assert 'sliced-changed' not in json.dumps(result)
    assert reads['n'] == 1


def test_unreadable_other_vendor_does_not_block_imported_parent(tmp_path):
    datadir, resources = tmp_path / 'data', tmp_path / 'resources'
    put(resources, 'VendorA', 'machine', 'fdm_machine_common', marker='a')
    bad = resources / 'VendorB' / 'machine' / 'fdm_machine_common.json'
    bad.parent.mkdir(parents=True)
    bad.write_text('{')
    child = put(datadir, 'Custom', 'machine', 'imported', inherits='fdm_machine_common')
    assert prof.effective_preset(child, 'machine', datadir, resources)['marker'] == 'a'
    put(resources, 'VendorC', 'machine', 'fdm_machine_common', marker='c')
    with pytest.raises(prof.ProfileError, match='ambiguous'):
        prof.effective_preset(child, 'machine', datadir, resources)


def test_unreadable_other_vendor_does_not_block_valid_chain(tmp_path):
    resources = tmp_path / 'resources'
    put(resources, 'VendorA', 'machine', 'fdm_machine_common', marker='a')
    bad = resources / 'VendorB' / 'machine' / 'fdm_machine_common.json'
    bad.parent.mkdir(parents=True)
    bad.write_text('{')
    child = put(resources, 'VendorA', 'machine', 'printer', inherits='fdm_machine_common')
    assert prof.effective_preset(child, 'machine', tmp_path / 'data', resources)['marker'] == 'a'


def test_parent_map_rejects_symlink_and_traversal(tmp_path):
    datadir = tmp_path / 'data'
    child = put(datadir, 'Custom', 'machine', 'child', inherits='parent')
    link = datadir / '.print-portal-parent-map.json'
    link.symlink_to('/etc/passwd')
    with pytest.raises(prof.ProfileError, match='invalid parent mapping'):
        prof.effective_preset(child, 'machine', datadir, tmp_path / 'resources')
    link.unlink()
    prof.save_parent_edges(datadir, {
        'datadir:Custom/machine/child.json': {
            'parent': 'resources:../../outside.json', 'inherits': 'parent', 'sha256': 'a' * 64,
        }
    })
    with pytest.raises(prof.ProfileError, match='invalid explicit parent path'):
        prof.effective_preset(child, 'machine', datadir, tmp_path / 'resources')


def test_profile_import_waits_for_lock_off_event_loop(datadir, monkeypatch, tmp_path):
    import asyncio
    import dataclasses
    import threading
    import httpx
    import main
    monkeypatch.setattr(main, 'CONFIG', dataclasses.replace(main.CONFIG, orca_datadir=datadir, profile_file=tmp_path / 'profile.json'))
    held = threading.Event()
    release = threading.Event()
    def hold_lock():
        with main.DERIVED_LOCK:
            held.set()
            release.wait(3)
    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert held.wait(1)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            pending = asyncio.create_task(client.post('/api/profile/upload', files={'file': ('x.json', json.dumps({'type': 'filament', 'name': 'new filament'}), 'application/json')}))
            await asyncio.sleep(0.05)
            try:
                health = await asyncio.wait_for(client.get('/api/health'), timeout=0.5)
                assert health.status_code == 200
                assert not pending.done()
            finally:
                release.set()
            assert (await pending).status_code == 200
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        holder.join()


def test_rejected_import_keeps_implicit_selection(client, datadir, monkeypatch, tmp_path):
    import dataclasses
    import main
    profile = tmp_path / 'profile.json'
    monkeypatch.setattr(main, 'CONFIG', dataclasses.replace(main.CONFIG, orca_datadir=datadir, profile_file=profile))
    original = {k: str(v.relative_to(datadir)) for k, v in main.partial_selection().items()}
    response = client.post('/api/profile/upload', files={'file': ('different.json', json.dumps({'type': 'machine', 'name': 'different machine'}), 'application/json')})
    assert any('not selected' in note for note in response.json()['notes'])
    assert {k: str(v.relative_to(datadir)) for k, v in main.partial_selection().items()} == original
