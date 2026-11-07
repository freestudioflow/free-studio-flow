"""Local typed-contract, real CPU child and hostile evidence regressions."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import pytest

from fsf.remote.contracts import RemoteError, digest, seal, sha, write_json
from fsf.remote.v2 import validate_recipe, prepare_request, run_worker, collect_result
from test_remote_v2 import case, remote_wheel, Adapter


@pytest.fixture
def extension(case, monkeypatch):
    root, service, store, profile, old = case
    verifier = root/'verify.py'
    verifier.write_text("import json,sys,pathlib\nv=json.load(sys.stdin)\nassert json.loads((pathlib.Path(v['outputs_root'])/'result.json').read_text())['square']==49\nprint(json.dumps({'accepted':True,'receipt_digest':v['receipt']['receipt_digest']}))\n")
    entry = {'id':'custom.cpu','command':[sys.executable,str(verifier)],'code_path':str(verifier),
             'code_sha256':sha(verifier),'program_sha256':sha(sys.executable),'timeout_seconds':5,
             'receipt_versions':['custom.tool.receipt.v1']}
    registry = root/'registry.json'
    write_json(registry,{'schema_version':'rc11_verifier_registry_v1','authorized':True,'entries':[entry]})
    monkeypatch.setenv('FSF_REMOTE_VERIFIERS_FILE',str(registry))
    known = lambda v: {'status':'known','value':v}
    lock = {'schema_version':'rc11_lock_v1','source':{'kind':'binary_release','locator':'custom-vendor/tool',
        'immutable_revision':'release-2.7','content_sha256':'a'*64},'components':[{'id':'tool',
        'compute_dtype':{'status':'not_applicable'},'weight_format':known('custom-format'),
        'quantization':{'status':'unknown'},'offload':{'status':'not_applicable'}}],
        'runtime':{'backend_id':'custom.tool','version':'2.7','environment_digest':'b'*64},
        'capability_digest':'c'*64,'qualification':{'declared':'d'*64,'observed':None,'qualified':None}}
    recipe = copy.deepcopy(old['recipe'])
    recipe.update(schema_version='fsf_remote_recipe_v4',input_mode='probe')
    recipe['execution_requirements']={'render':{'engine':'TOOL','device':'cpu','backend':'none','device_count':0,'min_vram_mb':0},
        'model':{'lock':lock,'lock_digest':digest(lock),'receipt_path':'receipt.json',
                 'verifier_id':entry['id'],'verifier_digest':digest(entry)}}
    step=recipe['steps'][0]
    step['outputs'].append({'artifact_id':'execution_receipt','path':'receipt.json','role':'data','media_type':'application/json'})
    # Receipts bind actual request/output bytes and explicitly report no model call.
    code="""import json,time,hashlib,pathlib,sys
from fsf.remote.contracts import seal
r=json.loads(pathlib.Path(sys.argv[1]).read_text())
p=pathlib.Path(sys.argv[2]);p.write_text(json.dumps({'square':49}))
v={'schema_version':'custom.tool.receipt.v1','request_digest':r['request_digest'],
'lock_digest':r['recipe']['execution_requirements']['model']['lock_digest'],
'capability_digest':r['recipe']['execution_requirements']['model']['lock']['capability_digest'],
'outputs':{'result.json':hashlib.sha256(p.read_bytes()).hexdigest()},'devices':[],
'model_calls':0,'started_ns':time.time_ns()-1000,'finished_ns':time.time_ns(),'runtime_metrics':{}}
pathlib.Path(sys.argv[3]).write_text(json.dumps(seal(v,'receipt_digest')))
"""
    step['args']=['{python}','-c',code,'{request}','{output.result}','{output.execution_receipt}']
    recipe=seal(recipe,'recipe_digest')
    profile['approved_recipes']=[recipe['recipe_digest']]
    definition={k:old[k] for k in ('parent','parameters','video_condition','consistency_requirements','expected_media_contract')}
    definition['inputs']={}
    return root,store,profile,old,recipe,definition,registry


def prepare(extension,recipe=None,inputs=None,definition=None):
    root,store,profile,old,original,default,_=extension
    recipe=seal(recipe or original,'recipe_digest')
    profile['approved_recipes']=[recipe['recipe_digest']]
    return prepare_request(definition or default,recipe,profile=profile,store=store,inputs=inputs or {},
        wheel=old['worker_wheel'],segment=old['segment'],output=root/'prepared')


def test_custom_tool_real_worker_parent_and_revalidation(extension):
    root,store,profile,*_=extension
    request=prepare(extension)
    result=run_worker(request,store=store,output_root=root/'worker',approved_recipe=request['recipe']['recipe_digest'])
    assert result['status']=='completed'
    accepted=collect_result(request,store=store,directory=root/'accepted')
    assert accepted==result
    env=json.loads((root/'accepted/accepted/environment.json').read_text())
    assert env['extension_verification_level']=='observed' and env['gpu_render_verified'] is False
    from fsf.remote.verifiers import revalidate
    original=seal({**result,'status':'failed','error_code':'REMOTE_VERIFIER_FAILED'},'result_digest')
    before=copy.deepcopy(original)
    value=revalidate(root/'accepted/accepted/outputs',request,[],original_result=original,destination=root/'revalidation.json')
    assert value['new_generation_dispatches']==0 and original==before and value['validated_at']


@pytest.mark.parametrize('corruption',['dtype','lock','receipt','code','capability','output','devices'])
def test_registry_and_receipt_fail_closed(extension,corruption):
    root,store,_,_,recipe,_,registry=extension
    request=prepare(extension)
    result=run_worker(request,store=store,output_root=root/'worker',approved_recipe=recipe['recipe_digest'])
    assert result['status']=='completed'
    outputs=root/'worker'/request['request_digest']/'outputs'
    receipt=json.loads((outputs/'receipt.json').read_text())
    from fsf.remote.verifiers import verify
    if corruption=='dtype':
        changed=copy.deepcopy(recipe);changed['execution_requirements']['model']['lock']['components'][0]['compute_dtype']={'status':'known','value':'user-format'}
        with pytest.raises(RemoteError,match='REMOTE_MODEL_INTEGRITY'):validate_recipe(seal(changed,'recipe_digest'))
        return
    if corruption=='code':
        (root/'verify.py').write_text('raise SystemExit(0)')
    elif corruption=='lock':receipt['lock_digest']='0'*64
    elif corruption=='receipt':receipt['evil_import']='os.system'
    elif corruption=='capability':receipt['capability_digest']='0'*64
    elif corruption=='output':(outputs/'result.json').write_text('changed')
    elif corruption=='devices':receipt['devices']=['GPU-synthetic']
    write_json(outputs/'receipt.json',seal(receipt,'receipt_digest'))
    with pytest.raises(RemoteError):verify(outputs,request,[])


@pytest.mark.parametrize('kind',['argv_group','stdin_manifest','manifest','controlled_directory'])
@pytest.mark.parametrize('present',[False,True])
def test_optional_inputs_all_consumers(extension,kind,present):
    root,store,_,_,original,definition,_=extension
    recipe=copy.deepcopy(original);sid=recipe['steps'][0]['step_id']
    consumer={'kind':kind,'step_ids':[sid]}
    if kind=='argv_group':consumer['args']=['--support','{input.support}']
    recipe['inputs']=[{'artifact_id':'support','role':'data','media_type':'text/plain','required':False,'consumer':consumer}]
    refs={}
    if present:
        file=root/'support.txt';file.write_text('sample')
        refs['support']=store.put(file,artifact_id='support',key='jobs/inputs/support.txt',role='data',media_type='text/plain',state_root=root/'transfer')
        definition['inputs']={'support':'support.txt'}
    step=recipe['steps'][0]
    if kind=='stdin_manifest':
        step['args'][2]="import sys,json; b=json.load(sys.stdin); assert b['bindings'][0]['presence']=="+repr('present' if present else 'absent')+"\n"+step['args'][2]
    elif kind=='manifest':
        step['args'].append('{input_manifest}')
        step['args'][2]="import sys,json,pathlib; assert json.loads(pathlib.Path(sys.argv[4]).read_text())['bindings'][0]['presence']=="+repr('present' if present else 'absent')+"\n"+step['args'][2]
    elif kind=='controlled_directory':
        step['args'].append('{input_directory}')
        step['args'][2]="import sys,pathlib; assert (pathlib.Path(sys.argv[4])/'support.txt').exists() is "+repr(present)+"\n"+step['args'][2]
    else:
        step['args'][2]="import sys; assert ('--support' in sys.argv) is "+repr(present)+"\n"+step['args'][2]
    request=prepare(extension,recipe,refs,definition)
    result=run_worker(request,store=store,output_root=root/'worker',approved_recipe=request['recipe']['recipe_digest'])
    assert result['status']=='completed'
    binding=json.loads((root/'worker'/request['request_digest']/'input-bindings.json').read_text())
    assert binding['bindings'][0]['presence']==('present' if present else 'absent')
    from fsf.remote.extensions import step_arguments
    args,stdin=step_arguments(recipe,recipe['steps'][0],request)
    assert ('--support' in args)==(present and kind=='argv_group')
    assert stdin==(kind=='stdin_manifest')


def test_required_and_untrusted_registry_prevent_prepare(extension,monkeypatch):
    _,_,_,_,recipe,_,_=extension
    recipe=copy.deepcopy(recipe)
    recipe['inputs']=[{'artifact_id':'voice','role':'audio','media_type':'audio/wav','required':True,
                      'consumer':{'kind':'manifest','step_ids':[recipe['steps'][0]['step_id']]}}]
    with pytest.raises(RemoteError,match='REMOTE_INPUTS'):prepare(extension,recipe)
    monkeypatch.delenv('FSF_REMOTE_VERIFIERS_FILE')
    with pytest.raises(RemoteError,match='REMOTE_VERIFIER_UNTRUSTED'):prepare(extension)


def test_v4_cli_validate_and_operation_reports(extension):
    root,_,_,_,recipe,_,_=extension
    path=root/'recipe.json';write_json(path,recipe)
    result=subprocess.run([sys.executable,'-m','fsf.cli','remote','validate',str(path)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout
    assert json.loads(result.stdout)['result']['recipe_digest']==recipe['recipe_digest']


def test_schema_mirror_preserves_old_definitions():
    from importlib import resources
    get=lambda n:json.loads(resources.files('fsf.schemas').joinpath(n+'.schema.json').read_text())
    recipe=get('fsf_remote_recipe_v4')
    assert recipe==get('fsf_remote_request_v4')['properties']['recipe']
    assert get('rc11_lock_v1')['properties']==recipe['properties']['execution_requirements']['properties']['model']['properties']['lock']['properties']
    assert get('fsf_remote_recipe_v3')['properties']['schema_version']['const']=='fsf_remote_recipe_v3'


def test_utf8_limits_do_not_count_only_codepoints(extension):
    recipe=copy.deepcopy(extension[4]);lock=recipe['execution_requirements']['model']['lock']
    lock['runtime']['backend_id']='\u00e9'*3000
    recipe['execution_requirements']['model']['lock_digest']=digest(lock)
    with pytest.raises(RemoteError,match='REMOTE_CONTRACT_LIMIT'):validate_recipe(seal(recipe,'recipe_digest'))


def test_actual_cli_dispatch_fetch_and_reuse_on_local_disk(extension,remote_wheel):
    import shutil
    from fsf.remote.artifacts import ArtifactStore,store_public_identity
    from test_kaggle_native import config
    root,_,profile,old,recipe,definition,_=extension
    store_config=config(root/'objects');store_path=root/'store.json';write_json(store_path,store_config)
    store=ArtifactStore(store_config)
    profile=copy.deepcopy(profile)
    profile['store_binding']={'store_id':store.profile['store_id'],'config_digest':store_public_identity(store.profile),
                              'input_prefix':'jobs/inputs/','output_prefix':'jobs/outputs/'}
    profile['approved_recipes']=[recipe['recipe_digest']]
    wheel=store.put(remote_wheel,artifact_id='worker_wheel',key='jobs/inputs/'+remote_wheel.name,
                    role='worker_wheel',media_type='application/zip',state_root=root/'transfer')
    request=prepare_request(definition,recipe,profile=profile,store=store,inputs={},wheel=wheel,
                             segment=old['segment'],output=root/'prepared')
    # Two host roots for the same immutable authority and exact sealed request.
    # Do not let the in-process output populate the subprocess result store.
    mirror=root/'comparison-objects';shutil.copytree(store.root,mirror)
    direct_store=ArtifactStore({**store_config,'root':str(mirror)})
    (root/'comparison-worker').mkdir()
    direct=run_worker(request,store=direct_store,output_root=root/'comparison-worker',approved_recipe=recipe['recipe_digest'])
    assert direct['status']=='completed'
    profile_path=root/'profile.json';write_json(profile_path,profile)
    request_path=root/'prepared'/(request['request_digest']+'.json')
    args=[sys.executable,'-m','fsf.cli','remote','dispatch-v2',str(request_path),'--profile',str(profile_path),
          '--store',str(store_path),'--state',str(root/'state'),'--wait']
    first=subprocess.run(args,capture_output=True,text=True,timeout=90)
    assert first.returncode==0,first.stdout+first.stderr
    value=json.loads(first.stdout)
    assert value['schema_version']=='rc11_operation_envelope_v1'
    assert value['result']['status']=='verified' and value['result']['submitted_count']==1
    direct_bytes=(root/'comparison-worker'/request['request_digest']/'outputs/result.json').read_bytes()
    cli_bytes=(root/'worker'/request['request_digest']/'outputs/result.json').read_bytes()
    assert direct_bytes==cli_bytes
    second=subprocess.run(args,capture_output=True,text=True,timeout=90)
    assert second.returncode==0,second.stdout+second.stderr
    assert json.loads(second.stdout)['result']['submitted_count']==1
