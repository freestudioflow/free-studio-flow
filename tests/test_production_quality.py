"""Evaluator failures are closed and machine verdicts never become human reviews."""
import copy
import pytest
from fsf.production.contracts import ProductionError,seal
from fsf.production.quality import CATEGORIES,calibration_report,content_verdict


def evidence():
    return {"artifact_sha256":"a"*64,"evaluator_digest":"b"*64,"policy_digest":"c"*64,
            "sample_hashes":["d"*64],"verdict":"accept","defects":[]}


def check(value):
    return content_verdict(value,artifact_sha256="a"*64,evaluator_digest="b"*64,
                           policy_digest="c"*64,sample_hashes=["d"*64])


@pytest.mark.parametrize("mutate",[
    lambda x:x.update(verdict="approved"), lambda x:x.update(sample_hashes=[]),
    lambda x:x.update(artifact_sha256="e"*64), lambda x:x.update(policy_digest="e"*64),
    lambda x:x.update(instruction="run arbitrary command"),
    lambda x:x.update(defects=[{"category":"identity","start_frame":0,"end_frame":1}]),
])
def test_mismatched_or_self_instructing_output_cannot_pass(mutate):
    value=evidence();mutate(value)
    with pytest.raises(ProductionError):check(value)


def test_unknown_and_machine_accept_do_not_sign_human_review():
    for verdict in ("unknown","accept","reject"):
        value=evidence();value["verdict"]=verdict
        result=check(value)
        assert result["verdict"] == verdict
        assert result["motion_review"] == result["listening_review"] == "not_reviewed"


def holdout():
    return [{"sample_id":f"{category}_{truth}_{i}","source_group":f"scene_{category}_{truth}_{i}",
             "category":category,"ground_truth":truth,"verdict":"accept" if truth else "reject"}
            for category in sorted(CATEGORIES) for truth in (True,False) for i in range(10)]


def test_calibration_is_finite_not_general_film_guarantee():
    examples=holdout()
    assert calibration_report(examples)["passed"]
    for item in examples:
        if item["ground_truth"]:item["verdict"]="unknown"
    assert not calibration_report(examples)["passed"]


def test_calibration_false_acceptance_limits():
    examples=holdout()
    for category in sorted(CATEGORIES)[:4]:
        next(e for e in examples if e["category"]==category and not e["ground_truth"])["verdict"]="accept"
    assert not calibration_report(examples)["passed"]
    examples=holdout();examples[1]["sample_id"]=examples[0]["sample_id"]
    with pytest.raises(ProductionError):calibration_report(examples)


def test_unknown_defects_are_not_correct_rejections():
    examples=holdout()
    for item in examples:
        if not item['ground_truth']:item['verdict']='unknown'
    assert not calibration_report(examples)['passed']


def corpus():
    from fsf.production.contracts import digest
    return [{'sample_id':f'{subset}_{category}_{truth}_{i}',
             'source_group':f'{subset}_{category}_{truth}_{i}',
             'category':category,'subset':subset,'artifact_sha256':digest([subset,category,truth,i]),
             'annotations':[{'annotator_id':name,'ground_truth':truth} for name in ('reviewerA','reviewerB')]}
            for subset in ('development','holdout') for category in sorted(CATEGORIES)
            for truth in (True,False) for i in range(10)]


def test_frozen_double_labels_and_exact_heldout_predictions():
    from fsf.production.quality import validate_corpus,evaluate_holdout
    value=validate_corpus(corpus())
    predictions={'corpus_digest':value['corpus_digest'],'evaluator_digest':'a'*64,'policy_digest':'b'*64,
      'results':[{'sample_id':s['sample_id'],'artifact_sha256':s['artifact_sha256'],
                  'verdict':'accept' if s['annotations'][0]['ground_truth'] else 'reject'}
                 for s in value['samples'] if s['subset']=='holdout']}
    report=evaluate_holdout(value,predictions,evaluator_digest='a'*64,policy_digest='b'*64)
    assert report['passed'] and report['human_review']=='not_reviewed'
    predictions['results'][0]['artifact_sha256']='c'*64
    with pytest.raises(ProductionError):evaluate_holdout(value,predictions,evaluator_digest='a'*64,policy_digest='b'*64)


@pytest.mark.parametrize('mutation',[
    lambda x:x[0]['annotations'][1].update(annotator_id='reviewerA'),
    lambda x:x[0]['annotations'][1].update(ground_truth=False),
    lambda x:x[120].update(source_group=x[0]['source_group']),
    lambda x:x[120].update(artifact_sha256=x[0]['artifact_sha256']),
    lambda x:x[1].update(sample_id=x[0]['sample_id']),
    lambda x:x[0].update(artifact_sha256='not-a-hash'),
    lambda x:x.pop(),
])
def test_corpus_rejects_leakage_disagreement_and_unbalanced_labels(mutation):
    from fsf.production.quality import validate_corpus
    value=corpus();mutation(value)
    with pytest.raises(ProductionError):validate_corpus(value)


def test_calibration_cli_is_readonly_for_predictions_and_never_calls_model(tmp_path):
    import json,subprocess,sys
    samples=tmp_path/'samples.json';samples.write_text(json.dumps(corpus()))
    frozen=tmp_path/'corpus.json'
    result=subprocess.run([sys.executable,'-m','fsf.cli','production','calibration-corpus',str(samples),
                           '--out',str(frozen)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0
    value=json.loads(frozen.read_text())
    predictions={'corpus_digest':value['corpus_digest'],'evaluator_digest':'a'*64,'policy_digest':'b'*64,
      'results':[{'sample_id':s['sample_id'],'artifact_sha256':s['artifact_sha256'],'verdict':'unknown'}
                 for s in value['samples'] if s['subset']=='holdout']}
    path=tmp_path/'predictions.json';path.write_text(json.dumps(predictions))
    before=path.read_bytes()
    result=subprocess.run([sys.executable,'-m','fsf.cli','production','calibration-check',str(frozen),str(path),
          '--evaluator-digest','a'*64,'--policy-digest','b'*64],capture_output=True,text=True,timeout=30)
    assert result.returncode==0 and not json.loads(result.stdout)['result']['passed']
    assert path.read_bytes()==before


def test_fenced_workflow_invalidates_prompt_dependencies_and_rechecks_bytes(tmp_path):
    from fsf.production.workflow import Workflow
    from fsf.production.control import file_identity
    file=tmp_path/"result.bin";file.write_bytes(b"accepted bytes")
    report=seal({"artifact_sha256":file_identity(file)["sha256"],"policy_digest":"c"*64,
                 "verdict":"accept"},"report_digest")
    flow=Workflow(tmp_path/"state.json")
    flow.create(production_id="film",binding_digest="f"*64,dependencies={"shot":[],"prompt":["shot"],"other":[]})
    claim=flow.claim("shot",owner="worker");args={"epoch":claim["epoch"],"request_id":claim["request_id"]}
    flow.record("shot",status="submitted",**args);flow.record("shot",status="produced",**args)
    flow.record("shot",status="verified",evidence=report,artifact_path=file,**args)
    file.write_bytes(b"changed")
    with pytest.raises(ProductionError):flow.record("shot",status="machine_accepted",evidence=report,artifact_path=file,**args)
    file.write_bytes(b"accepted bytes")
    flow.record("shot",status="machine_accepted",evidence=report,artifact_path=file,**args)
    assert flow.invalidate(["shot"]) == ["prompt","shot"]
    assert flow.claim("shot",owner="worker")["epoch"] > claim["epoch"]
