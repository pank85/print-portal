"""Reslice fork and local settings contracts; no worker or printer is started."""
import dataclasses
import json
import lzma
import struct
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def setup(tmp_path, datadir, monkeypatch):
    jobs = tmp_path / 'jobs'
    jobs.mkdir()
    monkeypatch.setattr(main, 'JOBS', jobs)
    monkeypatch.setattr(main, 'CONFIG', dataclasses.replace(main.CONFIG, orca_datadir=datadir,
                                                            profile_file=tmp_path / 'profile.json'))
    return TestClient(main.app), jobs, datadir


def source(jobs, status='sliced', filename='part.obj'):
    jid = '123456abcdef'
    d = jobs / jid
    (d / 'upload').mkdir(parents=True)
    (d / 'upload' / filename).write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
    data = {'id': jid, 'status': status, 'filename': filename, 'output_name': 'part',
            'profile_snapshot': {'machine': None, 'process': None, 'filament': None,
                                 'options': {'supports': False, 'brim': True}},
            'gcode': 'old.gcode', 'error': 'old', 'printed': True, 'slicer_input': 'old.3mf'}
    (d / 'job.json').write_text(json.dumps(data))
    return jid


def fork(client, jid, token=None):
    return client.post(f'/api/jobs/{jid}/reslice', json={'request_id': token or str(uuid.uuid4())})


def test_fork_copies_only_canonical_and_retry_survives_source_deletion(setup):
    client, jobs, _ = setup
    jid = source(jobs, 'failed')
    before = (jobs / jid / 'job.json').read_bytes()
    token = str(uuid.uuid4())
    r = fork(client, jid, token)
    assert r.status_code == 201, r.text
    child = r.json()
    assert child['revision'] == 0 and child['source_job_id'] == jid
    assert child['staging']['settings_editable']
    assert not {'gcode', 'error', 'printed', 'slicer_input'} & child.keys()
    d = jobs / child['id']
    assert (d / 'baseline').read_bytes() == (d / 'upload' / 'part.obj').read_bytes()
    assert (d / 'upload' / 'part.obj').stat().st_ino != (jobs / jid / 'upload' / 'part.obj').stat().st_ino
    assert (jobs / jid / 'job.json').read_bytes() == before
    assert client.delete(f'/api/jobs/{jid}').status_code == 200
    assert fork(client, jid, token).json()['id'] == child['id']
    assert (d / 'baseline').exists()
    assert client.post(f"/api/jobs/{child['id']}/slice").status_code == 422
    assert client.post(f"/api/jobs/{child['id']}/scale", json={}).status_code == 422


def test_concurrent_retry_creates_one_child(setup):
    client, jobs, _ = setup
    jid = source(jobs)
    token = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: fork(client, jid, token), range(8)))
    assert sorted(r.status_code for r in results) == [200] * 7 + [201]
    assert len({r.json()['id'] for r in results}) == 1
    assert fork(client, jid).json()['id'] != results[0].json()['id']


def test_bad_source_and_request_no_children(setup):
    client, jobs, _ = setup
    jid = source(jobs, 'queued')
    assert fork(client, jid).status_code == 409
    assert client.post(f'/api/jobs/{jid}/reslice', json={'request_id': 'bad'}).status_code == 422
    assert client.post(f'/api/jobs/{jid}/reslice', json={'request_id': str(uuid.uuid4()), 'x': 1}).status_code == 422
    assert fork(client, 'abcdef123456').status_code == 404
    main.write_job(jid, status='sliced', filename='../part.obj')
    assert fork(client, jid).status_code == 409
    main.write_job(jid, filename='part.obj')
    p = jobs / jid / 'upload' / 'part.obj'
    p.unlink()
    p.symlink_to('/etc/passwd')
    assert fork(client, jid).status_code == 409
    assert len(list(jobs.iterdir())) == 1


def test_copy_failure_cleans_staging(setup, monkeypatch):
    client, jobs, _ = setup
    jid = source(jobs)
    def fail(*args, **kwargs):
        raise OSError('disk failure')
    monkeypatch.setattr(main.shutil, 'copyfile', fail)
    with pytest.raises(OSError):
        fork(client, jid)
    assert [p.name for p in jobs.iterdir()] == [jid]


def test_local_settings_validation_and_revision(setup):
    client, jobs, datadir = setup
    jid = source(jobs)
    child = fork(client, jid).json()['id']
    endpoint = f'/api/jobs/{child}/settings'
    profile_before = main.CONFIG.profile_file.exists()
    r = client.patch(endpoint, json={'revision': 0, 'options': {'supports': True}})
    assert r.status_code == 200 and r.json()['revision'] == 1
    assert r.json()['profile_snapshot']['options'] == {'supports': True, 'brim': True}
    for body, status in [({'revision': 0, 'options': {'brim': False}}, 409),
                         ({'revision': 1, 'options': {'brim': 1}}, 422),
                         ({'revision': 1, 'options': {'hide_profiles': True}}, 422),
                         ({'revision': 1, 'profile': {'machine': None}}, 400),
                         ({'revision': 1, 'profile': {'machine': '/etc/passwd'}}, 400),
                         ({'revision': 1, 'profile': {'machine': '../outside'}}, 400),
                         ({'revision': True}, 422), ({'revision': 1, 'extra': 1}, 422)]:
        assert client.patch(endpoint, json=body).status_code == status
    assert client.get(f'/api/jobs/{child}').json()['revision'] == 1
    assert main.CONFIG.profile_file.exists() == profile_before
    assert client.get(f'/api/jobs/{jid}').json()['profile_snapshot']['options']['supports'] is False
    main.write_job(child, status='queued')
    assert client.patch(endpoint, json={'revision': 1}).status_code == 409


def test_select_local_presets_and_reject_incompatible_or_wrong_kind(setup):
    client, jobs, datadir = setup
    jid = source(jobs)
    child = fork(client, jid).json()['id']
    endpoint = f'/api/jobs/{child}/settings'
    preset_dir = datadir / 'local-test'
    preset_dir.mkdir()
    def preset(name, kind, **extra):
        p = preset_dir / f'{name}.json'
        p.write_text(json.dumps({'name': name, 'type': kind, **extra}))
        return p.relative_to(datadir).as_posix()
    machine = preset('My machine', 'machine')
    process = preset('process', 'process', compatible_printers=['My machine'])
    wrong = preset('wrong', 'process', compatible_printers=['Other machine'])
    filament = preset('filament', 'filament')
    response = client.patch(endpoint, json={'revision': 0, 'profile': {'machine': machine,
                                        'process': process, 'filament': filament}})
    assert response.status_code == 200, response.text
    assert response.json()['profile_snapshot']['filament'] == filament
    assert client.patch(endpoint, json={'revision': 1, 'profile': {'process': wrong}}).status_code == 400
    assert client.patch(endpoint, json={'revision': 1, 'profile': {'filament': process}}).status_code == 400
    assert client.get(f'/api/jobs/{child}').json()['revision'] == 1
    assert not main.CONFIG.profile_file.exists()


def test_preview_scale_reset_and_cache_invalidation(setup):
    client, jobs, _ = setup
    jid = source(jobs)
    child = fork(client, jid).json()['id']
    d = jobs / child
    model = d / 'upload' / 'part.obj'
    baseline = model.read_bytes()
    response = client.get(f'/api/jobs/{child}/preview')
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == 0
    assert response.json()['unit'] == 'millimeter'
    assert response.json()['bbox']['size'] == [1, 1, 0]
    (d / 'shots').mkdir()
    (d / 'shots' / 'front.png').write_bytes(b'old')
    (d / 'upload' / 'slice-part.3mf').write_bytes(b'old')
    main.write_job(child, slicer_input='slice-part.3mf')
    r = client.post(f'/api/jobs/{child}/scale', json={'revision': 0, 'width': 2})
    assert r.status_code == 200, r.text
    assert r.json()['size'] == [2, 2, 0] and r.json()['manual_scale'] == 2
    assert not (d / 'shots').exists() and not (d / 'upload' / 'slice-part.3mf').exists()
    r = client.post(f'/api/jobs/{child}/scale', json={'revision': 1, 'depth': 6})
    assert r.status_code == 200 and r.json()['manual_scale'] == 6
    r = client.post(f'/api/jobs/{child}/scale/reset', json={'revision': 2})
    assert r.status_code == 200 and r.json()['size'] == [1, 1, 0]
    assert r.json()['revision'] == 3 and r.json()['manual_scale'] == 1
    assert model.read_bytes() == baseline
    assert (jobs / jid / 'upload' / 'part.obj').read_bytes() == baseline


def test_scale_validation_failure_preserves_model_and_metadata(setup):
    client, jobs, _ = setup
    child = fork(client, source(jobs)).json()['id']
    d = jobs / child
    model = d / 'upload' / 'part.obj'
    before = model.read_bytes(), (d / 'job.json').read_bytes()
    for body in ({'revision': 1, 'width': 2}, {'revision': 0, 'width': True},
                 {'revision': 0, 'width': 0}, {'revision': 0, 'width': -1},
                 {'revision': 0, 'width': 10001}, {'revision': 0, 'width': 2, 'depth': 2},
                 {'revision': 0, 'height': 2}, {'revision': True, 'width': 2}):
        assert client.post(f'/api/jobs/{child}/scale', json=body).status_code in (400, 409, 422)
        assert (model.read_bytes(), (d / 'job.json').read_bytes()) == before
    assert client.post(f'/api/jobs/{child}/scale/reset', json={'revision': 1}).status_code == 409
    assert (model.read_bytes(), (d / 'job.json').read_bytes()) == before


def test_geometry_commit_metadata_failure_rolls_back(setup, monkeypatch):
    client, jobs, _ = setup
    child = fork(client, source(jobs)).json()['id']
    d = jobs / child
    model = d / 'upload' / 'part.obj'
    before = model.read_bytes(), (d / 'job.json').read_bytes()
    def fail(*args, **kwargs):
        raise OSError('metadata failure')
    monkeypatch.setattr(main, 'write_job', fail)
    for endpoint, body in [('scale', {'revision': 0, 'width': 2}),
                           ('scale/reset', {'revision': 0})]:
        assert client.post(f'/api/jobs/{child}/{endpoint}', json=body).status_code == 409
        assert (model.read_bytes(), (d / 'job.json').read_bytes()) == before


@pytest.mark.parametrize('compression', [zipfile.ZIP_DEFLATED, zipfile.ZIP_LZMA], ids=['deflate', 'lzma'])
def test_corrupt_compressed_fork_remains_listable_and_geometry_unchanged(setup, compression, monkeypatch):
    import queue
    client, jobs, datadir = setup
    monkeypatch.setattr(main, 'job_queue', queue.Queue())
    jid = source(jobs, 'failed', 'part.3mf')
    source_model = jobs / jid / 'upload' / 'part.3mf'
    with zipfile.ZipFile(source_model, 'w', compression=compression) as archive:
        archive.writestr('3D/3dmodel.model', '<model>geometry</model>')
    corrupt = bytearray(source_model.read_bytes())
    name_len, extra_len = struct.unpack_from('<HH', corrupt, 26)
    data_offset = 30 + name_len + extra_len
    if compression == zipfile.ZIP_DEFLATED:
        corrupt[data_offset] = 0x07  # Reserved DEFLATE block type.
    else:
        corrupt[data_offset + 4] = 0xff  # Invalid LZMA properties.
    source_model.write_bytes(corrupt)
    if compression == zipfile.ZIP_LZMA:
        with zipfile.ZipFile(source_model) as archive, pytest.raises(lzma.LZMAError):
            archive.read('3D/3dmodel.model')

    response = fork(client, jid)
    assert response.status_code == 201, response.text
    child = response.json()
    assert child['staging']['settings_editable']
    assert not child['staging']['preview_available']
    assert not child['staging']['resize_available']
    assert 'not a readable 3MF archive' in child['staging']['reason']
    cid = child['id']
    model = jobs / cid / 'upload' / 'part.3mf'
    baseline = jobs / cid / 'baseline'
    metadata = jobs / cid / 'job.json'
    before = (source_model.read_bytes(), model.read_bytes(), baseline.read_bytes(), metadata.read_bytes())
    assert before[:3] == (bytes(corrupt),) * 3
    assert client.get(f'/api/jobs/{cid}').json()['staging'] == child['staging']
    listing = client.get('/api/jobs')
    assert listing.status_code == 200, listing.text
    assert next(job for job in listing.json() if job['id'] == cid)['staging'] == child['staging']
    assert client.get(f'/api/jobs/{cid}/preview').status_code == 409
    assert client.post(f'/api/jobs/{cid}/scale', json={'revision': 0, 'width': 2}).status_code == 409
    assert (source_model.read_bytes(), model.read_bytes(), baseline.read_bytes(), metadata.read_bytes()) == before
    preset_dir = datadir / 'corrupt-confirm'
    preset_dir.mkdir()
    refs = {}
    for kind in ('machine', 'process', 'filament'):
        path = preset_dir / f'{kind}.json'
        path.write_text(json.dumps({'name': kind, 'type': kind}))
        refs[kind] = path.relative_to(datadir).as_posix()
    assert client.patch(f'/api/jobs/{cid}/settings', json={'revision': 0, 'profile': refs,
                                                          'options': {'brim': False}}).status_code == 200
    before_confirm = (source_model.read_bytes(), model.read_bytes(), baseline.read_bytes(), metadata.read_bytes())
    response = client.post(f'/api/jobs/{cid}/slice', json={'revision': 1})
    assert response.status_code == 400, response.text
    assert 'unreadable' in response.text
    assert main.job_queue.empty()
    assert (source_model.read_bytes(), model.read_bytes(), baseline.read_bytes(), metadata.read_bytes()) == before_confirm


def test_preview_unavailable_does_not_block_settings_or_reset(setup):
    client, jobs, _ = setup
    jid = source(jobs)
    (jobs / jid / 'upload' / 'part.obj').write_text('not a model')
    child = fork(client, jid).json()
    assert not child['staging']['preview_available']
    assert child['staging']['settings_editable']
    cid = child['id']
    assert client.get(f'/api/jobs/{cid}/preview').status_code == 409
    assert client.post(f'/api/jobs/{cid}/preview-shot').status_code == 405
    assert client.get(f'/api/jobs/{cid}/preview-shot').status_code == 409
    assert client.patch(f'/api/jobs/{cid}/settings', json={'revision': 0, 'options': {'brim': False}}).status_code == 200
    assert client.post(f'/api/jobs/{cid}/scale/reset', json={'revision': 1}).status_code == 200


def test_concurrent_scale_and_queued_guards(setup):
    client, jobs, _ = setup
    child = fork(client, source(jobs)).json()['id']
    def scale(_):
        return client.post(f'/api/jobs/{child}/scale', json={'revision': 0, 'width': 2})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(scale, range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    main.write_job(child, status='queued')
    for endpoint, body in [('scale', {'revision': 1, 'width': 3}), ('scale/reset', {'revision': 1})]:
        assert client.post(f'/api/jobs/{child}/{endpoint}', json=body).status_code == 409
    assert client.get(f'/api/jobs/{child}/preview').status_code == 409


def test_scale_nonfinite_and_scale_helper_failure(setup, monkeypatch):
    client, jobs, _ = setup
    child = fork(client, source(jobs)).json()['id']
    d = jobs / child
    model = d / 'upload' / 'part.obj'
    before = model.read_bytes(), (d / 'job.json').read_bytes()
    for value in ('NaN', 'Infinity', '-Infinity', '1e999'):
        response = client.post(f'/api/jobs/{child}/scale', content=f'{{"revision":0,"width":{value}}}',
                               headers={'content-type': 'application/json'})
        assert response.status_code == 422
    def fail(*args):
        raise main.preview.PreviewError('bad geometry')
    monkeypatch.setattr(main.preview, 'scale_file', fail)
    assert client.post(f'/api/jobs/{child}/scale', json={'revision': 0, 'width': 2}).status_code == 409
    assert (model.read_bytes(), (d / 'job.json').read_bytes()) == before


def test_settings_and_scale_compete_on_revision(setup):
    client, jobs, _ = setup
    child = fork(client, source(jobs)).json()['id']
    import threading
    barrier = threading.Barrier(2)
    def scale():
        barrier.wait()
        return client.post(f'/api/jobs/{child}/scale', json={'revision': 0, 'width': 2})
    def settings():
        barrier.wait()
        return client.patch(f'/api/jobs/{child}/settings', json={'revision': 0, 'options': {'brim': False}})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(scale), pool.submit(settings)]
        assert sorted(f.result().status_code for f in futures) == [200, 409]
    assert client.get(f'/api/jobs/{child}').json()['revision'] == 1


def test_existing_pins_are_replaced_and_restored_if_publish_fails(setup, monkeypatch):
    client, jobs, datadir = setup
    child = fork(client, source(jobs)).json()['id']
    preset_dir = datadir / 'local-replace'
    preset_dir.mkdir()
    refs = {}
    for kind in ('machine', 'process', 'filament'):
        path = preset_dir / f'{kind}.json'
        data = {'name': kind, 'type': kind}
        if kind == 'process':
            data['compatible_printers'] = ['machine']
        path.write_text(json.dumps(data))
        refs[kind] = path.relative_to(datadir).as_posix()
    assert client.patch(f'/api/jobs/{child}/settings', json={'revision': 0, 'profile': refs}).status_code == 200
    previous = jobs / child / 'pinned'
    previous.mkdir()
    (previous / 'machine.json').write_text('{"keep": true}')
    (previous / 'process.json').write_text('{"keep": true}')
    (previous / 'filament.json').write_text('{"keep": true}')
    before = {path.name: path.read_bytes() for path in previous.iterdir()}
    job_before = (jobs / child / 'job.json').read_bytes()
    original_write = main.write_job

    def fail_write(jid, **fields):
        raise OSError('publish failed')

    monkeypatch.setattr(main, 'write_job', fail_write)
    quiet = TestClient(main.app, raise_server_exceptions=False)
    failed = quiet.post(f'/api/jobs/{child}/slice', json={'revision': 1})
    assert failed.status_code == 500
    assert {path.name: path.read_bytes() for path in (jobs / child / 'pinned').iterdir()} == before
    assert (jobs / child / 'job.json').read_bytes() == job_before
    assert not list((jobs / child).glob('.pinned-*'))

    monkeypatch.setattr(main, 'write_job', original_write)
    ok = client.post(f'/api/jobs/{child}/slice', json={'revision': 1})
    assert ok.status_code == 200, ok.text
    pinned = jobs / child / 'pinned'
    assert json.loads((pinned / 'machine.json').read_text())['name'] == 'machine'
    assert not list((jobs / child).glob('.pinned-*'))


def test_reslice_slice_profile_never_falls_back_to_globals(setup, monkeypatch):
    client, jobs, _ = setup
    child = fork(client, source(jobs)).json()['id']
    def forbidden():
        raise AssertionError('global selection was consulted')
    monkeypatch.setattr(main, 'active_paths', forbidden)
    with pytest.raises(main.prof.ProfileError, match='pinned presets'):
        main.slice_profile(main.read_job(child))


def test_confirm_pins_presets_once_and_worker_uses_pins(setup, monkeypatch):
    import queue
    client, jobs, datadir = setup
    monkeypatch.setattr(main, 'job_queue', queue.Queue())
    child = fork(client, source(jobs)).json()['id']
    preset_dir = datadir / 'local-confirm'
    preset_dir.mkdir()
    refs = {}
    parent = datadir / 'system/Custom/process/reslice-test-parent.json'
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(json.dumps({'name': 'reslice-test-parent', 'type': 'process', 'brim_width': '7'}))
    for kind in ('machine', 'process', 'filament'):
        path = preset_dir / f'{kind}.json'
        data = {'name': kind, 'type': kind}
        if kind == 'process':
            data['compatible_printers'] = ['machine']
            data['inherits'] = 'reslice-test-parent'
        path.write_text(json.dumps(data))
        refs[kind] = path.relative_to(datadir).as_posix()
    r = client.patch(f'/api/jobs/{child}/settings', json={'revision': 0, 'profile': refs})
    assert r.status_code == 200, r.text
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(f'/api/jobs/{child}/slice', json={'revision': 1}), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert main.job_queue.qsize() == 1
    job = main.read_job(child)
    assert job['revision'] == 2
    paths, options = main.slice_profile(job)
    assert all(p.parent == jobs / child / 'pinned' for p in paths.values())
    before = {k: p.read_bytes() for k, p in paths.items()}
    assert json.loads(before['process'])['brim_width'] == '7'
    parent.write_text(json.dumps({'name': 'reslice-test-parent', 'type': 'process', 'brim_width': '19'}))
    for rel in refs.values():
        (datadir / rel).unlink()
    assert {k: p.read_bytes() for k, p in main.slice_profile(job)[0].items()} == before
    assert options['brim'] is True
    from types import SimpleNamespace
    seen = {}
    def run(cmd, **kwargs):
        seen['cmd'] = cmd
        machine, process = cmd[cmd.index('--load-settings') + 1].split(';')
        names = {'machine': machine, 'process': process,
                 'filament': cmd[cmd.index('--load-filaments') + 1]}
        seen['presets'] = {kind: json.loads(Path(path).read_text()) for kind, path in names.items()}
        (Path(kwargs['cwd']) / 'out' / 'part.gcode').write_text('; generated')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(main.subprocess, 'run', run)
    monkeypatch.setattr(main, 'SLICED', jobs.parent / 'sliced')
    assert main.claim_queued_job(child)
    main.run_slice(child)
    assert seen['presets'] == {kind: json.loads(blob) for kind, blob in before.items()}
    assert all('inherits' not in preset for preset in seen['presets'].values())
    assert main.read_job(child)['status'] == 'sliced'
    assert not main.CONFIG.profile_file.exists()
    assert client.post(f'/api/jobs/{child}/scale/reset', json={'revision': 2}).status_code == 409


def test_confirm_missing_local_preset_never_falls_back(setup, monkeypatch):
    import queue
    client, jobs, _ = setup
    monkeypatch.setattr(main, 'job_queue', queue.Queue())
    child = fork(client, source(jobs)).json()['id']
    monkeypatch.setattr(main, 'active_paths', lambda: pytest.fail('global fallback'))
    r = client.post(f'/api/jobs/{child}/slice', json={'revision': 0})
    assert r.status_code == 400
    assert main.read_job(child)['status'] == 'awaiting_confirm'
    assert main.job_queue.empty()


def test_confirm_3mf_rewrites_current_geometry_and_rejects_bad_settings(setup, monkeypatch, zip_bytes):
    import queue
    import threemf
    import io
    import zipfile
    client, jobs, datadir = setup
    monkeypatch.setattr(main, 'job_queue', queue.Queue())
    jid = source(jobs, filename='part.3mf')
    model = jobs / jid / 'upload' / 'part.3mf'
    settings = threemf.PROJECT_SETTINGS_MEMBER
    geometry = b'<model>current geometry</model>'
    model.write_bytes(zip_bytes({'3D/3dmodel.model': geometry, settings: b'{bad'}))
    child = fork(client, jid).json()['id']
    refs = {}
    preset_dir = datadir / 'confirm-3mf'
    preset_dir.mkdir()
    for kind in ('machine', 'process', 'filament'):
        path = preset_dir / (kind + '.json')
        path.write_text(json.dumps({'name': kind, 'type': kind}))
        refs[kind] = path.relative_to(datadir).as_posix()
    assert client.patch(f'/api/jobs/{child}/settings', json={'revision': 0, 'profile': refs}).status_code == 200
    r = client.post(f'/api/jobs/{child}/slice', json={'revision': 1})
    assert r.status_code == 400 and main.job_queue.empty()
    assert main.read_job(child)['revision'] == 1
    local = jobs / child / 'upload' / 'part.3mf'
    local.write_bytes(zip_bytes({'3D/3dmodel.model': geometry, settings: json.dumps({'printer_settings_id': 'old', 'print_settings_id': 'old'}).encode()}))
    r = client.post(f'/api/jobs/{child}/slice', json={'revision': 1})
    assert r.status_code == 200, r.text
    job = main.read_job(child)
    with zipfile.ZipFile(io.BytesIO((local.parent / job['slicer_input']).read_bytes())) as archive:
        assert archive.read('3D/3dmodel.model') == geometry
        identity = json.loads(archive.read(settings))
        assert identity['printer_settings_id'] == ['machine']
        assert identity['print_settings_id'] == ['process (portal options)']
    assert not main.CONFIG.profile_file.exists()
