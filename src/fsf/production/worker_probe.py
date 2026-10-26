"""CPU recipe diagnostic; explicitly not a neural model or movie acceptance."""
import argparse
from pathlib import Path

from fsf.production.contracts import digest, publish, seal
from fsf.production.ledger import Ledger
from fsf.production.workflow import Workflow
from fsf.production.segments import affected_tasks


def run_probe(out):
    out=Path(out)
    folder=out.parent/"probe-state"
    folder.mkdir(exist_ok=True)
    authorization=seal({"schema_version":"fsf_production_budget_v1","production_id":"cpu_probe",
        "currency":"USD","unit_scale":100,"authorized":True,"total_units":1000,
        "cleanup_units":100,"fixed_delivery_units":80,"route_limits":{"local":900},
        "max_generated_frames":1000,"logical_shot_ids":[]},"authorization_digest")
    ledger=Ledger(folder/"ledger.json")
    ledger.create(authorization)
    ledger.reserve("probe",route="local",worst_units=10,generated_frames=0,binding_digest=digest(authorization))
    ledger.reconcile("probe",settled_units=0)
    flow=Workflow(folder/"workflow.json")
    graph={"first":[],"dependent_prompt":["first"],"unrelated":[]}
    flow.create(production_id="cpu_probe",binding_digest=digest(graph),dependencies=graph)
    report={"schema_version":"fsf_production_cpu_probe_v1","scope":"production_cpu_contracts",
            "budget_revision":ledger.inspect()["revision"],"workflow_revision":flow.inspect()["revision"],
            "affected_tasks":affected_tasks(graph,["first"]),"generation_model_calls":0,
            "gpu_render_verified":False,"runtime_contract_probe":True}
    publish(out,seal(report,"report_digest"))


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--out",required=True)
    run_probe(parser.parse_args().out)
