"""Optional backend failures expose stable diagnostics, never exception text."""
import subprocess,sys
from fsf.tools.wan_video_worker import failure_codes,generation_parameters
import pytest


def test_diagnostic_allowlist_does_not_echo_private_exception_text():
    secret='SYNTHETIC_PRIVATE_VALUE'
    codes=failure_codes('DOWNLOAD',RuntimeError(secret+' protobuf Xet'))
    assert secret not in str(codes)
    assert codes==['REMOTE_WAN_DOWNLOAD_RUNTIMEERROR','REMOTE_WAN_HINT_PROTOBUF','REMOTE_WAN_HINT_XET']
    assert failure_codes(secret,ValueError(secret))==['REMOTE_WAN_IMPORT_VALUEERROR']


def test_worker_help_never_imports_optional_model_libraries():
    result=subprocess.run([sys.executable,'-m','fsf.tools.wan_video_worker','--help'],capture_output=True,text=True,timeout=30)
    assert result.returncode==0 and '--control' in result.stdout


def test_real_generation_parameters_have_no_placeholder_prompt_or_unbounded_windows():
    value={'weight_bytes':100,'prompt':'a red cup rotates','negative_prompt':'','seed':42,
           'inference_steps':8,'window_frames':17,'overlap_frames':1}
    assert generation_parameters(value)==value
    for field,bad in [('prompt',''),('seed',True),('window_frames',96),('overlap_frames',17),('inference_steps',0)]:
        with pytest.raises(ValueError):generation_parameters({**value,field:bad})


def test_text_only_ack_cannot_claim_unconsumed_reference_or_changed_prompt():
    from fsf.tools.wan_video_worker import prompt_acknowledgement
    from fsf.runtime.handoff import build_consistency_requirements
    from fsf.remote.contracts import RemoteError
    req=build_consistency_requirements({'generation_intent':{'effective_prompt':'a sphere moves'}})
    ack=prompt_acknowledgement({'prompt':'a sphere moves'},req)
    assert ack['acknowledged_digest']==req['requirements_digest'] and ack['ref_assets_used']==[]
    with pytest.raises(RemoteError):prompt_acknowledgement({'prompt':'another prompt'},req)
    req=build_consistency_requirements({'generation_intent':{'effective_prompt':'a sphere moves'},'ref_images':[{'sha256':'a'*64}]})
    with pytest.raises(RemoteError):prompt_acknowledgement({'prompt':'a sphere moves'},req)
def test_recipe_ack_output_has_the_remote_worker_envelope():
    from fsf.tools.wan_video_worker import acknowledgement_output
    value={'acknowledged_digest':'a'*64,'capability_gap':None,'ref_assets_used':[],'continuity_method':'prompt_only'}
    encoded=acknowledgement_output(value)
    assert encoded['consistency_acknowledgement']==value
    assert encoded['condition_acknowledgement'] is None
