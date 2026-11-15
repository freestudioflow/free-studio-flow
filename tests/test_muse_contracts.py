"""Local synthetic fixtures for the R13 voice, setup and destination contracts."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import wave

import pytest

from fsf.remote.contracts import RemoteError, digest, seal, sha, write_json
from fsf.remote.destination import existing_slot_proof, new_slot_proof, require_secret_capability, resolve, secret_capabilities
from fsf.remote.environment import install_environment, worker_credentials
from fsf.remote.setup_paths import setup_argv
from fsf.remote.verifiers import verify
from test_remote_extensibility import case, extension, prepare, remote_wheel


@pytest.mark.parametrize('fault', [None, 'clock', 'sidecar', 'tamper', 'verifier', 'truncated', 'weights', 'dtype'])
def test_voice_receipt_redecodes_and_runs_pinned_verifier(tmp_path, fault):
    outputs = tmp_path / 'out'
    outputs.mkdir()
    wav = outputs / 'voice.wav'
    with wave.open(str(wav), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x00\x00' * 320)
    if fault == 'truncated':
        wav.write_bytes(wav.read_bytes()[:-2])
    for name in ('config.json', 'tool-lock.json'):
        (outputs / name).write_text('{}')
    sidecars = {name: sha(outputs / name) for name in ('config.json', 'tool-lock.json')}
    program = tmp_path / 'verify.py'
    program.write_text("import json,sys\nr=json.load(sys.stdin)['receipt']\n"
                       "print(json.dumps(dict(accepted=True,receipt_digest=r['receipt_digest'])))\n")
    entry = dict(id='cpu.voice', command=[sys.executable, str(program)], code_path=str(program),
                 code_sha256=sha(program), program_sha256=sha(sys.executable),
                 timeout_seconds=10, receipt_versions=['fsf_voice_receipt_v1'])
    registry = tmp_path / 'registry.json'
    write_json(registry, dict(schema_version='rc11_verifier_registry_v1', authorized=True, entries=[entry]))
    dtype = {'status': 'known', 'value': 'fp32'}
    lock = dict(source={'content_sha256': 'a'*64}, components=[{'id': 'voice', 'compute_dtype': dtype}],
                runtime={'environment_digest': sidecars['tool-lock.json']}, capability_digest='c'*64)
    model = dict(lock=lock, lock_digest=digest(lock), receipt_path='receipt.json',
                 verifier_id=entry['id'], verifier_digest=digest(entry))
    request = dict(request_digest='b'*64, recipe=dict(execution_requirements={
        'model': model, 'render': {'device': 'cpu'}}, steps=[{'outputs': [
            {'path': name} for name in ('voice.wav', 'config.json', 'tool-lock.json', 'receipt.json')]}]))
    receipt = dict(schema_version='fsf_voice_receipt_v1', request_digest=request['request_digest'],
                   lock_digest=digest(lock), weights_sha256='a'*64, capability_digest='c'*64,
                   dtype={'voice': dtype}, config_path='config.json', tool_lock_path='tool-lock.json',
                   sidecars=sidecars, outputs={**sidecars, 'voice.wav': sha(wav)},
                   wav=dict(path='voice.wav', sha256=sha(wav), size_bytes=wav.stat().st_size,
                            sample_rate=16000, channels=1, sample_count=320))
    if fault == 'clock':
        receipt['wav']['sample_rate'] = 24000
    elif fault == 'sidecar':
        sidecars.pop('config.json')
    elif fault == 'tamper':
        (outputs / 'config.json').write_text('changed')
    elif fault == 'verifier':
        program.write_text('raise SystemExit(0)')
    elif fault == 'weights':
        receipt['weights_sha256'] = '0'*64
    elif fault == 'dtype':
        receipt['dtype'] = {'voice': {'status': 'unknown'}}
    receipt = seal(receipt, 'receipt_digest')
    write_json(outputs / 'receipt.json', receipt)
    if fault:
        with pytest.raises(RemoteError):
            verify(outputs, request, [], registry=registry)
    else:
        assert verify(outputs, request, [], registry=registry) == receipt


def test_setup_passes_space_as_single_argv_and_keeps_legacy_form(tmp_path):
    root = tmp_path / 'work directory'
    root.mkdir()
    args = ['{python}', '-c', 'import sys; print(sys.argv[1])', '{workdir}/file with spaces']
    resolved = setup_argv(args, root, {'python': sys.executable})
    result = subprocess.run(resolved, shell=False, check=True, capture_output=True, text=True, timeout=10)
    assert Path(result.stdout.strip()) == root / 'file with spaces'
    assert setup_argv(['{python}', '-m', 'pip'], root, {}) is None


@pytest.mark.parametrize('arg', ['prefix{workdir}', '{workdir}/../escape', '{unknown}', '/elsewhere',
                                 '--out=C:/elsewhere', '{workdir}/C:/elsewhere'])
def test_setup_rejects_unsafe_opt_in_argv(tmp_path, arg):
    with pytest.raises(RemoteError, match='REMOTE_SETUP_PATH'):
        setup_argv(['{workdir}', arg], tmp_path, {})


def test_setup_rejects_symlink_escape(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    try:
        (root / 'escape').symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('symlink unavailable')
    with pytest.raises(RemoteError, match='REMOTE_SETUP_PATH'):
        setup_argv(['{workdir}/escape/output'], root, {})


@pytest.mark.parametrize('mode', ['api_push', 'agent_browser'])
def test_secret_capability_and_install_isolation(mode):
    profile = {'transport': 'kaggle', 'settings': {'activation_mode': mode, 'worker_notebook': 'fsf-worker-fixed'},
               'destination': {'intent': 'existing', 'model_sources': ['pinned/model/1'],
                               'secret_aliases': ['VOICE_KEY']}}
    report = secret_capabilities(profile)
    assert report['submission_mode'] == mode
    assert report['provider_attachment']['status'] == 'unavailable'
    assert report['model_sources'] == ['pinned/model/1']
    with pytest.raises(RemoteError, match='CAPABILITY_UNAVAILABLE'):
        require_secret_capability(profile)
    credential = 'fixture-secret-value'
    worker = worker_credentials(['VOICE_KEY'], ['VOICE_KEY'], source={'VOICE_KEY': credential})
    assert worker == {'VOICE_KEY': credential}
    assert not install_environment(worker, ['VOICE_KEY'], source={})
    assert credential not in json.dumps(report)
    with pytest.raises(RemoteError, match='CREDENTIAL_MISSING'):
        worker_credentials(['VOICE_KEY'], ['VOICE_KEY'], source={})
    with pytest.raises(RemoteError, match='CREDENTIAL_NOT_AUTHORIZED'):
        worker_credentials(['VOICE_KEY'], [], source=worker)

    def broken(_):
        raise ConnectionError(credential)

    with pytest.raises(RemoteError, match='^REMOTE_HOST_SECRET_ACCESS_FAILED$'):
        worker_credentials(['VOICE_KEY'], ['VOICE_KEY'], source={}, resolve_secret=broken)


def test_kaggle_slot_proofs_are_distinct_and_fail_closed():
    ref = 'owner/fsf-worker-test'
    metadata = dict(ref=ref, is_private=True, current_version_number=2, id=100)
    assert existing_slot_proof(SimpleNamespace(**metadata), ref=ref, version=2) == 2
    for change in ({'ref': 'other/fsf-worker-test'}, {'is_private': False},
                   {'current_version_number': 3}, {'id': 0}):
        with pytest.raises(RemoteError, match='DESTINATION_UNVERIFIED'):
            existing_slot_proof(SimpleNamespace(**(metadata | change)), ref=ref, version=2)
    proof = dict(owner='owner', slug='fsf-worker-test', state='absent', can_create_private=True)
    assert new_slot_proof(proof, owner='owner', slug='fsf-worker-test') == proof
    for invalid in (None, 403, 404, {'private': True, 'sha256': '1'*64}, proof | {'state': 'existing'}):
        with pytest.raises(RemoteError, match='DESTINATION_UNVERIFIED'):
            new_slot_proof(invalid, owner='owner', slug='fsf-worker-test')
    with pytest.raises(RemoteError, match='DESTINATION_CONFLICT'):
        resolve({'transport': 'kaggle', 'settings': {},
                 'destination': {'intent': 'existing', 'model_sources': [], 'secret_aliases': []}})


def test_worker_and_parent_collect_synthetic_voice_receipt(extension):
    import copy
    import hashlib
    from fsf.core.edit_store import read_json
    from fsf.remote.v2 import collect_result, run_worker

    root, store, _, _, recipe, _, registry = extension
    recipe = copy.deepcopy(recipe)
    model = recipe['execution_requirements']['model']
    model['lock']['runtime']['environment_digest'] = hashlib.sha256(b'fixture tool lock').hexdigest()
    model['lock_digest'] = digest(model['lock'])
    policy = read_json(registry)
    entry = policy['entries'][0]
    verifier = Path(entry['code_path'])
    verifier.write_text("import json,sys\nr=json.load(sys.stdin)['receipt']\n"
                        "print(json.dumps(dict(accepted=True,receipt_digest=r['receipt_digest'])))\n")
    entry['code_sha256'] = sha(verifier)
    entry['receipt_versions'] = ['fsf_voice_receipt_v1']
    model['verifier_digest'] = digest(entry)
    write_json(registry, policy)
    outputs = [{'artifact_id': name, 'path': path, 'role': 'data', 'media_type': media}
               for name, path, media in [('voice', 'voice.wav', 'audio/wav'),
                                         ('config', 'config.json', 'application/json'),
                                         ('tool_lock', 'tool-lock.json', 'application/json'),
                                         ('receipt', 'receipt.json', 'application/json')]]
    script = """import json,sys,wave
from pathlib import Path
from fsf.remote.contracts import seal,sha
req,wav,config,tool,receipt=map(Path,sys.argv[1:])
request=json.loads(req.read_text())
model=request['recipe']['execution_requirements']['model']
lock=model['lock']
with wave.open(str(wav),'wb') as out:
 out.setparams((1,2,16000,0,'NONE','not compressed'))
 out.writeframes(bytes(640))
config.write_bytes(b'{}')
tool.write_bytes(b'fixture tool lock')
sidecars={p.name:sha(p) for p in (config,tool)}
value=dict(schema_version='fsf_voice_receipt_v1',request_digest=request['request_digest'],
 lock_digest=model['lock_digest'],weights_sha256=lock['source']['content_sha256'],
 capability_digest=lock['capability_digest'],dtype={'tool':lock['components'][0]['compute_dtype']},
 config_path=config.name,tool_lock_path=tool.name,sidecars=sidecars,outputs=dict(sidecars,**{wav.name:sha(wav)}),
 wav=dict(path=wav.name,sha256=sha(wav),size_bytes=wav.stat().st_size,sample_rate=16000,channels=1,sample_count=320))
receipt.write_text(json.dumps(seal(value,'receipt_digest')))
"""
    recipe['steps'][0].update(outputs=outputs, args=['{python}', '-c', script, '{request}',
        '{output.voice}', '{output.config}', '{output.tool_lock}', '{output.receipt}'])
    request = prepare(extension, recipe)
    result = run_worker(request, store=store, output_root=root/'worker',
                        approved_recipe=request['recipe']['recipe_digest'])
    assert result['status'] == 'completed', result.get('error_code')
    assert collect_result(request, store=store, directory=root/'accepted') == result


def test_real_v4_worker_setup_keeps_credentials_in_worker_only(extension, monkeypatch):
    import copy
    from fsf.remote.v2 import run_worker

    root, store, profile, _, recipe, _, _ = extension
    recipe = copy.deepcopy(recipe)
    source = 'https://pypi.org/simple'
    profile['policy']['network_sources'] = [source]
    profile['policy']['credential_env_names'] = ['VOICE_KEY']
    recipe['environment']['credential_env_names'] = ['VOICE_KEY']
    monkeypatch.setenv('VOICE_KEY', 'fixture-worker-credential')
    recipe['environment']['setup'] = [{'source': source, 'args': [
        '{python}', '-c', "import os,pathlib,sys; assert 'VOICE_KEY' not in os.environ; "
        "pathlib.Path(sys.argv[1]).write_text('setup passed')", '{workdir}/path with spaces']}]
    recipe['steps'][0]['args'][2] = (
        "import os; assert os.environ.get('VOICE_KEY');\n" + recipe['steps'][0]['args'][2])
    request = prepare(extension, recipe)
    result = run_worker(request, store=store, output_root=root/'worker',
                        approved_recipe=request['recipe']['recipe_digest'])
    assert result['status'] == 'completed'
    assert (root/'worker'/request['request_digest']/'path with spaces').read_text() == 'setup passed'
