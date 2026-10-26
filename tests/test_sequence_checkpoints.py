"""Portable checkpoints reuse generated pixels on a different worker disk."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from fsf.core.edit_store import read_json
from fsf.production.contracts import ProductionError, seal, publish
from fsf.production.control import import_control, verify_control, file_identity
from fsf.production.segments import compile_segments
from fsf.production.video_sequence import run_sequence
from fsf.production.sequence_checkpoints import export_checkpoint, verify_checkpoint, restore_checkpoint
from test_production_control import movie
from test_production_foundation import capability


def backend(frames, initialized, segment, deadline):
    if initialized:
        assert initialized[0].getpixel((0, 0)) == (23, 45, 67)
    result = [frame.copy() for frame in frames]
    for frame in result:
        frame.putpixel((0, 0), (23, 45, 67))
    return result


@pytest.fixture
def partial(tmp_path):
    imported = import_control(movie(tmp_path / 'input.mp4', count=37), output_root=tmp_path, control_asset_id='A')
    control = tmp_path / imported['bundle']
    plan = compile_segments(verify_control(control), capability(17, overlap=1, continuation='previous_frame'),
                            shot_id='A', policy_digest='a'*64)
    worker = tmp_path / 'worker1'; worker.mkdir()
    result = run_sequence(control, plan, output_root=worker, generator=backend, max_new_segments=2)
    assert result['status'] == 'paused'
    out = tmp_path / 'portable'; out.mkdir()
    exported = export_checkpoint(worker, plan, output_root=out)
    return control, plan, worker, out / exported['bundle'], exported


def test_export_restore_fresh_process_never_regenerates_accepted_segments(partial, tmp_path):
    control, plan, worker, bundle, exported = partial
    assert export_checkpoint(worker, plan, output_root=bundle.parent) == exported
    assert verify_checkpoint(bundle, plan)['completed']
    new = tmp_path / 'worker2'; new.mkdir()
    imported = restore_checkpoint(bundle, plan, output_root=new, expected_checkpoint_digest=exported['checkpoint_digest'])
    assert imported['completed_segments'] == 2 and not imported['reused']
    assert restore_checkpoint(bundle, plan, output_root=new)['reused']
    plan_file = tmp_path / 'plan.json'; plan_file.write_text(json.dumps(plan))
    code = '''import sys,json
from fsf.core.edit_store import read_json
from fsf.production.video_sequence import run_sequence
def backend(frames, initialized, segment, deadline):
 assert segment['segment_id'] not in ('segment_000001','segment_000002')
 assert initialized[0].getpixel((0,0))==(23,45,67)
 for frame in frames:frame.putpixel((0,0),(23,45,67))
 return frames
result=run_sequence(sys.argv[1],read_json(sys.argv[2]),output_root=sys.argv[3],generator=backend)
print(json.dumps({'status':result['status'],'new':result['new_segments'],'frames':result['measured']['frame_count']}))
'''
    child = subprocess.run([sys.executable, '-c', code, str(control), str(plan_file), str(new)],
                           capture_output=True, text=True, timeout=90)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == {'status':'completed','new':1,'frames':37}
    # Restoring an earlier accepted prefix must not erase later progress.
    assert restore_checkpoint(bundle, plan, output_root=new)['reused']
    original = run_sequence(control, plan, output_root=worker, generator=backend)
    assert file_identity(new/plan['plan_digest']/'movie.mp4') == original['output']


@pytest.mark.parametrize('mutation', ['chunk', 'page', 'policy', 'digest'])
def test_tampering_and_wrong_binding_reject_atomically(partial, tmp_path, mutation):
    _, plan, _, bundle, exported = partial
    target = tmp_path / 'new'; target.mkdir()
    expected = exported['checkpoint_digest']
    if mutation == 'chunk':
        chunk = next(p for p in (bundle/'chunks').iterdir() if len(p.name) == 64)
        chunk.write_bytes(b'changed')
    elif mutation == 'page':
        (bundle/'pages/000000.json').write_text('[]')
    elif mutation == 'policy':
        plan = seal({**plan, 'policy_digest':'b'*64}, 'plan_digest')
    else:
        expected = '0'*64
    with pytest.raises(ValueError):
        restore_checkpoint(bundle, plan, output_root=target, expected_checkpoint_digest=expected)
    assert not (target/plan['plan_digest']).exists()
    assert not list(target.glob('.sequence-restore-*'))


def test_partial_or_unlisted_segment_bytes_never_export(partial, tmp_path):
    _, plan, worker, _, _ = partial
    extra = worker/plan['plan_digest']/plan['segments'][0]['segment_id']/'private.txt'
    extra.write_text('unlisted')
    out = tmp_path/'rejected'; out.mkdir()
    with pytest.raises(ProductionError):
        export_checkpoint(worker, plan, output_root=out)
    assert not list(out.iterdir())


def test_incomplete_segment_and_export_limits_do_not_publish(partial, tmp_path):
    _, plan, worker, _, _ = partial
    out = tmp_path/'rejected'; out.mkdir()
    with pytest.raises(ProductionError):export_checkpoint(worker, plan, output_root=out, max_bytes=1)
    assert not list(out.iterdir())
    with pytest.raises(ProductionError):export_checkpoint(worker, plan, output_root=out, max_files=1)
    assert not list(out.iterdir())
    (worker/plan['plan_digest']/plan['segments'][2]['segment_id']).mkdir()
    with pytest.raises(ProductionError):export_checkpoint(worker, plan, output_root=out)
    assert not list(out.iterdir())


def test_resealed_traversal_metadata_and_false_whole_digest_are_rejected(partial, tmp_path):
    _, plan, _, bundle, _ = partial
    original_page = read_json(bundle/'pages/000000.json')
    original_index = read_json(bundle/'checkpoint.json')
    for mutation in ('path','whole'):
        rows = copy.deepcopy(original_page)
        if mutation == 'path':rows[0]['path']='../escape.json'
        else:
            rows[0]['chunks']['sha256']='0'*64
            rows[0]['chunks']=seal(rows[0]['chunks'],'manifest_digest')
            publish(bundle/'chunks'/(rows[0]['chunks']['manifest_digest']+'.json'),rows[0]['chunks'])
        publish(bundle/'pages/000000.json',rows)
        index=copy.deepcopy(original_index)
        index['pages'][0].update(file_identity(bundle/'pages/000000.json'))
        publish(bundle/'checkpoint.json',seal(index,'checkpoint_digest'))
        destination=tmp_path/('restore-'+mutation);destination.mkdir()
        with pytest.raises(ValueError):restore_checkpoint(bundle,plan,output_root=destination)
        assert not (destination/plan['plan_digest']).exists()
        assert not (tmp_path/'escape.json').exists()
        assert not list(destination.glob('.sequence-restore-*'))


def test_portable_checkpoint_exceeds_legacy_inline_limit_in_chunks(partial, tmp_path):
    # Transport stress uses an explicitly synthetic binary core payload. It is
    # not passed off as valid media or neural execution evidence.
    _, plan, worker, _, _ = partial
    folder = worker/plan['plan_digest']/plan['segments'][0]['segment_id']
    with (folder/'core.mp4').open('wb') as stream:
        for _ in range(65):stream.write(b'x'*1024*1024)
    receipt = read_json(folder/'segment.json')
    receipt['core_identity'] = file_identity(folder/'core.mp4')
    publish(folder/'segment.json', seal(receipt,'checkpoint_digest'))
    out = tmp_path/'large'; out.mkdir()
    result = export_checkpoint(worker, plan, output_root=out)
    assert result['size_bytes'] > 64*1024*1024
    new = tmp_path/'large-worker'; new.mkdir()
    restored = restore_checkpoint(out/result['bundle'], plan, output_root=new)
    assert restored['completed_segments'] == 2
    assert file_identity(new/plan['plan_digest']/plan['segments'][0]['segment_id']/'core.mp4') == receipt['core_identity']


def test_symlink_chunk_rejected(partial, tmp_path):
    _, plan, _, bundle, _ = partial
    chunk = next(p for p in (bundle/'chunks').iterdir() if len(p.name) == 64)
    external = tmp_path/'outside'; shutil.copyfile(chunk, external); chunk.unlink()
    try:chunk.symlink_to(external)
    except OSError:pytest.skip('symlink unavailable on this host')
    with pytest.raises(ValueError):verify_checkpoint(bundle, plan)
