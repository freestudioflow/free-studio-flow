"""Unmeasured models can run experimentally without weakening resource authority."""
import copy,pytest
from test_kaggle_native import native,case,remote_wheel
from test_model_execution_evidence import model_recipe
from fsf.remote.contracts import RemoteError,seal
from fsf.remote.v2 import prepare_request,check_authorization,validate_profile


def setup(native,monkeypatch):
    root,store,profile,old=native
    monkeypatch.setenv('FSF_REMOTE_GPU','1')
    recipe=model_recipe();recipe['recipe_id']='unmeasured_operator_model'
    recipe=seal(recipe,'recipe_digest')
    profile.update(schema_version='fsf_remote_profile_v3',allow_recipe_experiments=True,approved_recipes=[])
    profile['policy']['gpu']=True
    profile['gpu_limits']={'allowed':True,'backends':['cuda'],'max_devices':1,'max_vram_mb':24576}
    definition={k:old[k] for k in ('parent','parameters','video_condition','consistency_requirements','expected_media_contract')}
    definition['inputs']={}
    return root,store,profile,old,recipe,definition


def test_unlisted_model_recipe_is_allowed_but_cannot_drop_experimental_label(native,monkeypatch):
    root,store,profile,old,recipe,definition=setup(native,monkeypatch)
    request=prepare_request(definition,recipe,profile=profile,store=store,inputs={},wheel=old['worker_wheel'],
                            segment=old['segment'],output=root/'prepared')
    assert request['execution_mode']=='experimental' and profile['approved_recipes']==[]
    assert check_authorization(request,profile,store)==request
    altered=copy.deepcopy(request);altered.pop('execution_mode');altered=seal(altered,'request_digest')
    with pytest.raises(RemoteError):check_authorization(altered,profile,store)


def test_experiment_does_not_grant_extra_vram_or_enable_gpu_without_parent_switch(native,monkeypatch):
    root,store,profile,old,recipe,definition=setup(native,monkeypatch)
    profile['gpu_limits']['max_vram_mb']=1024
    with pytest.raises(RemoteError):prepare_request(definition,recipe,profile=profile,store=store,inputs={},
        wheel=old['worker_wheel'],segment=old['segment'],output=root/'prepared')
    profile['gpu_limits']['max_vram_mb']=24576;monkeypatch.delenv('FSF_REMOTE_GPU')
    with pytest.raises(RemoteError):prepare_request(definition,recipe,profile=profile,store=store,inputs={},
        wheel=old['worker_wheel'],segment=old['segment'],output=root/'prepared')


def test_strict_profile_still_requires_recipe_digest_and_old_v2_cannot_enable_mode(native,monkeypatch):
    root,store,profile,old,recipe,definition=setup(native,monkeypatch)
    profile['allow_recipe_experiments']=False
    with pytest.raises(RemoteError):prepare_request(definition,recipe,profile=profile,store=store,inputs={},
        wheel=old['worker_wheel'],segment=old['segment'],output=root/'prepared')
    profile['schema_version']='fsf_remote_profile_v2'
    with pytest.raises(RemoteError):validate_profile(profile)


def test_experimental_authority_also_allows_unlisted_cpu_recipes(native):
    root,store,profile,old=native
    profile.update(schema_version='fsf_remote_profile_v3',allow_recipe_experiments=True,approved_recipes=[])
    definition={k:old[k] for k in ('parent','parameters','video_condition','consistency_requirements','expected_media_contract')}
    definition['inputs']={}
    request=prepare_request(definition,old['recipe'],profile=profile,store=store,inputs={},wheel=old['worker_wheel'],
                            segment=old['segment'],output=root/'prepared')
    assert request['execution_mode']=='experimental' and not request['policy']['gpu']
    assert check_authorization(request,profile,store)==request
