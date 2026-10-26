"""Bind a real remote video to its pre-existing production workorder.

This is a fulfiller bridge, not a journal writer or a review authority.
"""
from pathlib import Path
import copy
import shutil,tempfile,os
from fsf.core.asset_library import _root,_no_links,_path_parts
from fsf.core.edit_store import edit_lock,read_json
from fsf.production.contracts import ProductionError,digest,publish
from fsf.production.control import file_identity
from fsf.runtime.handoff import ingest_workorder_result


def bind_remote_contracts(plan):
    """Prepare a new plan with generation inputs bound to its existing QC contract.

    Never change an in-flight plan: the caller writes a new file, and normal
    execution plan-digest checks reject reuse of a different prior execution.
    """
    from pdx_artifact_core import validate_execution_plan
    from fsf.conformance.media import validate_media_expected
    if validate_execution_plan(plan):raise ProductionError('PRODUCTION_PLAN_INVALID')
    result=copy.deepcopy(plan)
    for step in result['steps']:
        if step.get('kind')!='tool' or step.get('tool')!='studio.video.generate':continue
        outputs=step.get('outputs',[])
        candidates=[q for q in result['steps'] if q.get('tool')=='pdx.media.technical_conform'
            and q.get('inputs',{}).get('target_artifact') in outputs and step['id'] in q.get('depends_on',[])]
        if len(outputs)!=1 or len(candidates)!=1:raise ProductionError('PRODUCTION_QC_CONTRACT_REQUIRED')
        qc=candidates[0]['inputs'];contract=qc.get('expected_media_contract')
        try:validate_media_expected(contract)
        except (TypeError,ValueError):raise ProductionError('PRODUCTION_QC_CONTRACT_REQUIRED') from None
        if digest(contract)!=qc.get('expected_media_contract_digest'):
            raise ProductionError('PRODUCTION_QC_CONTRACT_MISMATCH')
        inputs=step['inputs']
        if inputs.get('expected_media_contract') is not None and inputs['expected_media_contract']!=contract:
            raise ProductionError('PRODUCTION_QC_CONTRACT_MISMATCH')
        if inputs.get('expected_media_contract_digest') is not None and inputs['expected_media_contract_digest']!=digest(contract):
            raise ProductionError('PRODUCTION_QC_CONTRACT_MISMATCH')
        inputs['expected_media_contract']=copy.deepcopy(contract)
        inputs['expected_media_contract_digest']=digest(contract)
    if validate_execution_plan(result):raise ProductionError('PRODUCTION_PLAN_INVALID')
    return result


def _workorder(value):
    if (not isinstance(value,dict) or value.get('schema_version')!='fsf_workorder_v1'
            or digest({k:v for k,v in value.items() if k!='workorder_digest'})!=value.get('workorder_digest')):
        raise ProductionError('PRODUCTION_WORKORDER_INVALID')
    _path_parts(value['target_artifact'])
    if len(_path_parts(value['workorder_id']))!=1:
        raise ProductionError('PRODUCTION_WORKORDER_INVALID')
    return value


def definition_from_workorder(workorder,*,parameters,inputs):
    """Use identifiers emitted by production; never rebind a previously run job."""
    work=_workorder(workorder)
    parent={k:work[k] for k in ('job_id','run_id','step_id','attempt_id','plan_digest','inputs_digest','expected_contract_digest','requirements_digest')}
    return {'parent':parent,'parameters':parameters,'inputs':inputs,
            'expected_media_contract':work['expected_media_contract'],
            'consistency_requirements':work.get('consistency_requirements'),
            'video_condition':work.get('video_condition')}


def fulfill_remote_video(workorder,request,*,profile,store,dispatch_directory,output_root,artifact_id='video'):
    """Revalidate current remote evidence, then publish Handoff result last.

    Production execute must resume normally to create its own journal/QC.
    No completed production entries or human approvals are manufactured here.
    """
    from fsf.remote.v2 import check_authorization,collect_result
    work=_workorder(workorder)
    expected=definition_from_workorder(work,parameters=request['parameters'],inputs={})
    if (request['parent']!=expected['parent'] or request['expected_media_contract']!=expected['expected_media_contract']
            or request['consistency_requirements']!=expected['consistency_requirements']
            or request['video_condition']!=expected['video_condition']):
        raise ProductionError('PRODUCTION_WORKORDER_MISMATCH')
    from fsf.remote.v2_dispatch import _read as read_dispatch
    state=read_dispatch(Path(dispatch_directory)/'ledger.json')
    if (state.get('status')!='verified' or state.get('request_digest')!=request['request_digest']
            or state.get('profile_digest')!=digest(profile)):
        raise ProductionError('PRODUCTION_REMOTE_NOT_VERIFIED')
    check_authorization(request,profile,store,new_submission=False)
    result=collect_result(request,store=store,directory=dispatch_directory)
    if state.get('result_digest')!=result['result_digest']:
        raise ProductionError('PRODUCTION_REMOTE_NOT_VERIFIED')
    declared={s['artifact_id']:s for step in request['recipe']['steps'] for s in step['outputs']}
    spec=declared.get(artifact_id)
    if spec is None or spec['role']!='video':raise ProductionError('PRODUCTION_VIDEO_REQUIRED')
    source=Path(dispatch_directory)/'accepted/outputs'/spec['path']
    identity=file_identity(source)
    root=_root(output_root)
    target=root.joinpath(*_path_parts(work['target_artifact']))
    for existing in (target,*target.parents):
        if existing.exists() or existing.is_symlink():_no_links(existing)
    projection={k:work[k] for k in ('workorder_id','job_id','run_id','step_id','attempt_id','plan_digest','inputs_digest')}
    projection.update(schema_version='fsf_workorder_result_v1',status='completed',
        expected_contract_digest=work['expected_contract_digest'],artifact_path=work['target_artifact'],
        artifact_sha256=identity['sha256'])
    if work.get('consistency_requirements'):
        if not isinstance(result.get('consistency_acknowledgement'),dict):
            raise ProductionError('PRODUCTION_ACK_REQUIRED')
        projection['consistency_acknowledgement']=result['consistency_acknowledgement']
    scratch=Path(tempfile.mkdtemp(prefix='.remote-fulfill-',dir=root))
    try:
        staged=scratch.joinpath(*_path_parts(work['target_artifact']));staged.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,staged)
        if file_identity(staged)!=identity:raise ProductionError('PRODUCTION_SOURCE_CHANGED')
        publish(scratch/'result.json',projection)
        ingest_workorder_result(scratch/'result.json',work,scratch)
        results=root/'handoff/results';results.mkdir(parents=True,exist_ok=True);_no_links(results)
        result_path=results/(work['workorder_id']+'.result.json')
        with edit_lock(result_path):
            if result_path.exists():
                if read_json(result_path)!=projection or file_identity(target)!=identity:
                    raise ProductionError('PRODUCTION_RESULT_CONFLICT')
            else:
                target.parent.mkdir(parents=True,exist_ok=True);_no_links(target.parent)
                if target.exists():
                    if file_identity(target)!=identity:raise ProductionError('PRODUCTION_RESULT_CONFLICT')
                else:os.replace(staged,target)
                # Handoff readers cannot accept an orphan target before this.
                publish(result_path,projection)
        return {'workorder_id':work['workorder_id'],'artifact_sha256':identity['sha256'],
                'remote_result_digest':result['result_digest'],'production_journal_written':False,
                'motion_review':'not_reviewed','semantic_quality':'not_evaluated'}
    finally:
        shutil.rmtree(scratch)
