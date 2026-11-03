"""One actual content hash per sweep item, never a replayed stat-only proof."""
import hashlib
import json

import pytest

from fsf.production.control import ArtifactObservation
from fsf.production.contracts import ProductionError
from fsf.production.store_runner import run_store_batch
from test_production_store import store_case
from test_production_command_driver import driver


def test_observation_is_bound_to_one_nonce_and_consumed_once(tmp_path):
    path=tmp_path/'source.bin'
    path.write_bytes(b'actual content')
    challenge=object()
    observation=ArtifactObservation(path,challenge)
    with pytest.raises(ProductionError,match='OBSERVATION_INVALID'):
        observation.consume(path,object())
    value=observation.consume(path,challenge)
    assert value['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ProductionError,match='OBSERVATION_INVALID'):
        observation.consume(path,challenge)


def test_actual_command_full_sweep_hashes_each_artifact_once_and_rejects_mutation(tmp_path,monkeypatch):
    import fsf.production.control as control
    plan,store=store_case(tmp_path,'v1')
    backend,_=driver(tmp_path,plan)
    assert run_store_batch(plan,store=store,driver=backend)['status']=='machine_accepted'
    calls=[]
    original=control.file_identity
    def observed(path,*args,**kwargs):
        if str(path).endswith('artifact.bin'):calls.append(str(path))
        return original(path,*args,**kwargs)
    monkeypatch.setattr(control,'file_identity',observed)
    assert run_store_batch(plan,store=store,driver=backend)['status']=='machine_accepted'
    assert len(calls)==len(set(calls))==2
    requests=json.loads((tmp_path/'submitted.json').read_text())
    (tmp_path/'artifacts'/requests[0]/'artifact.bin').write_bytes(b'mutated after last sweep')
    with pytest.raises(ProductionError,match='SOURCE_CHANGED'):
        run_store_batch(plan,store=store,driver=backend)
    assert len(json.loads((tmp_path/'submitted.json').read_text()))==2
    store.close()
