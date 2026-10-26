"""Real frame materialization and durable continuation; synthetic backend is explicit."""
import copy
import pytest
from PIL import Image
from fsf.production.control import import_control
from fsf.production.segments import compile_segments
from fsf.production.contracts import seal,ProductionError
from fsf.production.video_sequence import run_sequence
from test_production_control import movie
from test_production_foundation import capability


@pytest.mark.parametrize('rate',['24','24000/1001'])
def test_materialized_windows_resume_and_use_actual_generated_predecessor(tmp_path,rate):
    source=movie(tmp_path/'input.mp4',rate,count=103)
    result=import_control(source,output_root=tmp_path,control_asset_id='A')
    bundle=tmp_path/result['bundle']
    from fsf.production.control import verify_control
    spec=verify_control(bundle)
    cap=capability(17,overlap=1,continuation='previous_frame')
    cap['clocks']=[spec['clock']];cap['qualification']='unverified';cap=seal(cap,'capability_digest')
    with pytest.raises(ProductionError):compile_segments(spec,cap,shot_id='A',policy_digest='a'*64)
    plan=compile_segments(spec,cap,shot_id='A',policy_digest='a'*64,qualification_probe=True)
    out=tmp_path/'run';out.mkdir();calls=[]
    def backend(frames,initialized,segment,deadline):
        calls.append(segment['segment_id'])
        if initialized:
            assert initialized[0].getpixel((0,0))==(23,45,67)
        generated=[frame.copy() for frame in frames]
        for frame in generated:frame.putpixel((0,0),(23,45,67))
        return generated
    with pytest.raises(ProductionError,match='PROBE_ONLY'):
        run_sequence(bundle,plan,output_root=out,generator=backend)
    paused=run_sequence(bundle,plan,output_root=out,generator=backend,max_new_segments=2,allow_probe=True)
    assert paused['status']=='paused' and len(calls)==2
    completed=run_sequence(bundle,plan,output_root=out,generator=backend,allow_probe=True)
    assert completed['status']=='completed' and completed['measured']['frame_count']==103
    assert len(calls)==len(set(calls))==len(plan['segments'])
    reused=run_sequence(bundle,plan,output_root=out,generator=backend,allow_probe=True)
    assert reused['new_segments']==0 and reused['output']==completed['output']
    assert completed['motion_review']=='not_reviewed' and completed['semantic_quality']=='not_evaluated'
    first=out/plan['plan_digest']/plan['segments'][0]['segment_id']/'frames/000000.png'
    first.write_bytes(b'tamper')
    with pytest.raises(ProductionError):run_sequence(bundle,plan,output_root=out,generator=backend,allow_probe=True)


def test_wrong_model_frame_count_never_publishes_segment(tmp_path):
    result=import_control(movie(tmp_path/'input.mp4',count=9),output_root=tmp_path,control_asset_id='A')
    bundle=tmp_path/result['bundle']
    from fsf.production.control import verify_control
    plan=compile_segments(verify_control(bundle),capability(17),shot_id='A',policy_digest='a'*64)
    out=tmp_path/'run';out.mkdir()
    with pytest.raises(ProductionError,match='MODEL_FRAMES'):
        run_sequence(bundle,plan,output_root=out,generator=lambda *args:[])
    assert not list(out.rglob('segment.json'))


def test_resume_in_new_python_process_never_regenerates_completed_segment(tmp_path):
    import json,subprocess,sys
    from fsf.production.control import verify_control
    result=import_control(movie(tmp_path/'input.mp4',count=37),output_root=tmp_path,control_asset_id='A')
    bundle=tmp_path/result['bundle']
    plan=compile_segments(verify_control(bundle),capability(17,overlap=1,continuation='previous_frame'),shot_id='A',policy_digest='a'*64)
    out=tmp_path/'run';out.mkdir()
    first=run_sequence(bundle,plan,output_root=out,generator=lambda frames,*args:frames,max_new_segments=1)
    assert first['status']=='paused'
    file=tmp_path/'plan.json';file.write_text(json.dumps(plan))
    code="""import sys,json
from fsf.production.video_sequence import run_sequence
from fsf.core.edit_store import read_json
def backend(frames,initialized,segment,deadline):
 assert segment['segment_id']!='segment_000001', 'completed segment dispatched again'
 assert initialized, 'previous generated frame missing'
 return frames
value=run_sequence(sys.argv[1],read_json(sys.argv[2]),output_root=sys.argv[3],generator=backend)
print(json.dumps({'status':value['status'],'new':value['new_segments']}))
"""
    child=subprocess.run([sys.executable,'-c',code,str(bundle),str(file),str(out)],capture_output=True,text=True,timeout=90)
    assert child.returncode==0,child.stderr
    assert json.loads(child.stdout)=={'status':'completed','new':2}
