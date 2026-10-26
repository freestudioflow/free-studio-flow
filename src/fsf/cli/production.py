"""Production source preparation and durable coordination, with fixed diagnostics."""
import json

from fsf.cli.edit import _Parser, _Once, _integer
from fsf.core.edit_store import EditStoreError, edit_lock, read_json
from fsf.core.asset_library import AssetValidationError
from fsf.production.contracts import ProductionError, publish
from fsf.remote.contracts import RemoteError


def main(argv=None):
    parser = _Parser(prog="fsf production")
    commands = parser.add_subparsers(dest="operation",required=True)
    imported = commands.add_parser("import-control",help="Import a full control movie without conversion")
    imported.add_argument("source")
    imported.add_argument("--out",required=True,action=_Once)
    imported.add_argument("--control-id",required=True,action=_Once)
    verify = commands.add_parser("verify-control",help="Verify the retained source bytes")
    verify.add_argument("bundle")
    segments = commands.add_parser("segments",help="Compile global core and context frame mappings")
    segments.add_argument("bundle")
    segments.add_argument('--qualification-probe',action='store_true',help='Compile an explicitly probe-only unqualified capability')
    for flag in ("capability","shot-id","policy-digest","out"):
        segments.add_argument("--"+flag,required=True,action=_Once)
    split = commands.add_parser("split-artifact",help="Create verified chunks on a worker disk")
    split.add_argument("source");split.add_argument("--out",required=True,action=_Once)
    join = commands.add_parser("assemble-artifact",help="Resume verified chunk assembly")
    join.add_argument("manifest")
    join.add_argument("--chunks",required=True,action=_Once);join.add_argument("--out",required=True,action=_Once)
    create = commands.add_parser("budget-create",help="Create a versioned exact-unit budget")
    create.add_argument("authorization");create.add_argument("--out",required=True,action=_Once)
    inspect = commands.add_parser("budget-inspect",help="Read the current budget without inference")
    inspect.add_argument("ledger")
    reserve = commands.add_parser("budget-reserve",help="Atomically reserve an idempotent task")
    reserve.add_argument("ledger")
    for flag in ("request-id","route","units","frames","binding-digest"):
        reserve.add_argument("--"+flag,required=True,action=_Once)
    for flag in ("shot-id","segment-id","repair-round"):
        reserve.add_argument("--"+flag,action=_Once)
    for name in ("workflow-inspect","pause","resume"):
        op=commands.add_parser(name,help="Inspect or change the finite parent pause state")
        op.add_argument("workflow")
    snap=commands.add_parser('asset-snapshot',help='Fix explicit asset versions in a production snapshot')
    snap.add_argument('selections');snap.add_argument('--library',required=True,action=_Once)
    snap.add_argument('--out',required=True,action=_Once)
    pack=commands.add_parser('pack-export',help='Publish declared fixed assets as a self-contained pack')
    pack.add_argument('snapshot');pack.add_argument('--library',required=True,action=_Once)
    pack.add_argument('--out',required=True,action=_Once)
    pack_check=commands.add_parser('pack-verify',help='Verify every declared pack resource')
    pack_check.add_argument('bundle')
    corpus=commands.add_parser('calibration-corpus',help='Validate operator-supplied double labels and split isolation')
    corpus.add_argument('samples');corpus.add_argument('--out',required=True,action=_Once)
    calibration=commands.add_parser('calibration-check',help='Score exactly the frozen held-out predictions')
    calibration.add_argument('corpus');calibration.add_argument('predictions')
    for flag in ('evaluator-digest','policy-digest'):
        calibration.add_argument('--'+flag,required=True,action=_Once)
    definition=commands.add_parser('workorder-definition',help='Bind a remote definition to an existing production workorder')
    definition.add_argument('workorder')
    for name in ('parameters','inputs','out'):definition.add_argument('--'+name,required=True,action=_Once)
    fulfill=commands.add_parser('fulfill-remote',help='Revalidate a completed remote video and publish its matching Handoff result')
    fulfill.add_argument('workorder');fulfill.add_argument('request')
    for name in ('profile','store','dispatch','out'):fulfill.add_argument('--'+name,required=True,action=_Once)
    fulfill.add_argument('--artifact-id',default='video')
    bind=commands.add_parser('bind-remote-contracts',help='Write a new plan binding generation inputs to their existing QC contracts')
    bind.add_argument('plan');bind.add_argument('--out',required=True,action=_Once)
    world_create=commands.add_parser('world-create',help='Freeze declared spatial/identity/event memory, not visual qualification')
    world_create.add_argument('definition');world_create.add_argument('--out',required=True,action=_Once)
    world_state=commands.add_parser('world-state',help='Inspect global state across segments and chapters')
    world_state.add_argument('world');world_state.add_argument('--frame',required=True,type=_integer,action=_Once)
    world_window=commands.add_parser('world-window',help='Compile immutable world requirements for a global interval')
    world_window.add_argument('world')
    for flag in ('start-frame','end-frame'):world_window.add_argument('--'+flag,required=True,type=_integer,action=_Once)
    world_window.add_argument('--out',required=True,action=_Once)
    for name in ('checkpoint-export','checkpoint-restore','checkpoint-verify'):
        item=commands.add_parser(name,help='Portable generated frame state; never semantic approval')
        item.add_argument('bundle');item.add_argument('--plan',required=True,action=_Once)
        if name!='checkpoint-verify':item.add_argument('--out',required=True,action=_Once)
        if name!='checkpoint-export':item.add_argument('--digest',required=True,action=_Once)
    run=commands.add_parser('run-batch',help='Run a finite durable parent batch through an operator command driver')
    run.add_argument('plan')
    for flag in ('driver','ledger','state'):run.add_argument('--'+flag,required=True,action=_Once)
    run.add_argument('--seconds',default=300,type=_integer,action=_Once)
    forecast=commands.add_parser('budget-forecast',help='Record a bound-plan automatic worst-case completion estimate')
    forecast.add_argument('ledger')
    for flag in ('remaining-units','plan-digest'):forecast.add_argument('--'+flag,required=True,action=_Once)
    speech=commands.add_parser('speech-check',help='Verify declared ASR/dialogue events against explicit sample policy')
    speech.add_argument('expected')
    for flag in ('transcript','policy','artifact-sha256'):speech.add_argument('--'+flag,required=True,action=_Once)
    duck=commands.add_parser('ducking-envelope',help='Build explicit additive gain envelope without altering user gain')
    duck.add_argument('coverage')
    for flag in ('track-id','attenuation-millidb','attack-samples','release-samples','out'):duck.add_argument('--'+flag,required=True,action=_Once)
    rollup=commands.add_parser('quality-rollup',help='Require exact segment/seam/chapter/film coverage without hiding failures')
    rollup.add_argument('manifest');rollup.add_argument('--reports',required=True,action=_Once)
    rollup.add_argument('--out',required=True,action=_Once)
    try:
        args = parser.parse_args(argv)
        if args.operation=='quality-rollup':
            from fsf.production.quality_rollup import quality_rollup
            result=quality_rollup(read_json(args.manifest),read_json(args.reports))
            with edit_lock(args.out) as path:
                if path.exists():raise ProductionError('PRODUCTION_TARGET_EXISTS')
                publish(path,result)
        elif args.operation in {'world-create','world-state','world-window'}:
            from fsf.production.memory import create_world,state_at,interval_requirements
            if args.operation=='world-state':result=state_at(read_json(args.world),args.frame)
            else:
                result=create_world(read_json(args.definition)) if args.operation=='world-create' else interval_requirements(read_json(args.world),args.start_frame,args.end_frame)
                with edit_lock(args.out) as path:
                    if path.exists():raise ProductionError('PRODUCTION_TARGET_EXISTS')
                    publish(path,result)
        elif args.operation.startswith('checkpoint-'):
            from fsf.production.sequence_checkpoints import export_checkpoint,restore_checkpoint,verify_checkpoint
            plan=read_json(args.plan)
            if args.operation=='checkpoint-export':result=export_checkpoint(args.bundle,plan,output_root=args.out)
            elif args.operation=='checkpoint-restore':result=restore_checkpoint(args.bundle,plan,output_root=args.out,expected_checkpoint_digest=args.digest)
            else:result=verify_checkpoint(args.bundle,plan,expected_checkpoint_digest=args.digest)
        elif args.operation=='run-batch':
            from pathlib import Path
            from fsf.production.runner import run_batch,validate_plan
            from fsf.production.command_driver import CommandDriver
            from fsf.production.ledger import Ledger
            state=Path(args.state);state.mkdir(parents=True,exist_ok=True)
            plan=read_json(args.plan);validate_plan(plan)
            driver_root=state/'driver';driver_root.mkdir(exist_ok=True)
            driver=CommandDriver(read_json(args.driver),plan=plan,state_root=driver_root)
            result=run_batch(plan,state_root=state,ledger=Ledger(args.ledger),driver=driver,seconds=args.seconds)
        elif args.operation=='budget-forecast':
            from fsf.production.ledger import Ledger
            result=Ledger(args.ledger).record_forecast(remaining_worst_units=_integer(args.remaining_units),plan_digest=args.plan_digest)
        elif args.operation=='speech-check':
            from fsf.production.speech import evaluate_dialogue_coverage
            result=evaluate_dialogue_coverage(read_json(args.expected),read_json(args.transcript),read_json(args.policy),artifact_sha256=args.artifact_sha256)
        elif args.operation=='ducking-envelope':
            from fsf.production.speech import build_ducking_envelope
            result=build_ducking_envelope(read_json(args.coverage),target_track_id=args.track_id,
                attenuation_millidb=_integer(args.attenuation_millidb),attack_samples=_integer(args.attack_samples),release_samples=_integer(args.release_samples))
            with edit_lock(args.out) as path:
                if path.exists():raise ProductionError('PRODUCTION_TARGET_EXISTS')
                publish(path,result)
        elif args.operation=='bind-remote-contracts':
            from fsf.production.remote_handoff import bind_remote_contracts
            result=bind_remote_contracts(read_json(args.plan))
            with edit_lock(args.out) as path:
                if path.exists():raise ProductionError('PRODUCTION_TARGET_EXISTS')
                publish(path,result)
        elif args.operation in {'workorder-definition','fulfill-remote'}:
            from fsf.production.remote_handoff import definition_from_workorder,fulfill_remote_video
            if args.operation=='workorder-definition':
                result=definition_from_workorder(read_json(args.workorder),parameters=read_json(args.parameters),inputs=read_json(args.inputs))
                with edit_lock(args.out) as path:
                    if path.exists():raise ProductionError('PRODUCTION_TARGET_EXISTS')
                    publish(path,result)
            else:
                from fsf.remote.artifacts import ArtifactStore
                result=fulfill_remote_video(read_json(args.workorder),read_json(args.request),profile=read_json(args.profile),
                    store=ArtifactStore(read_json(args.store)),dispatch_directory=args.dispatch,output_root=args.out,artifact_id=args.artifact_id)
        elif args.operation == 'calibration-corpus':
            from fsf.production.quality import validate_corpus
            result=validate_corpus(read_json(args.samples))
            with edit_lock(args.out) as path:
                if path.exists():raise ProductionError('PRODUCTION_TARGET_EXISTS')
                publish(path,result)
        elif args.operation == 'calibration-check':
            from fsf.production.quality import evaluate_holdout
            result=evaluate_holdout(read_json(args.corpus),read_json(args.predictions),
                                    evaluator_digest=args.evaluator_digest,policy_digest=args.policy_digest)
        elif args.operation == "import-control":
            from fsf.production.control import import_control
            result = import_control(args.source,output_root=args.out,control_asset_id=args.control_id)
        elif args.operation == "verify-control":
            from fsf.production.control import verify_control
            result = verify_control(args.bundle)
        elif args.operation == "segments":
            from fsf.production.control import verify_control
            from fsf.production.segments import compile_segments
            result = compile_segments(verify_control(args.bundle),read_json(args.capability),
                                      shot_id=args.shot_id,policy_digest=args.policy_digest,qualification_probe=args.qualification_probe)
            with edit_lock(args.out) as path:
                if path.exists():raise ProductionError("PRODUCTION_TARGET_EXISTS")
                publish(path,result)
        elif args.operation == "split-artifact":
            from fsf.production.artifacts import split_artifact
            result = split_artifact(args.source,chunk_root=args.out)
        elif args.operation == "assemble-artifact":
            from fsf.production.artifacts import assemble_artifact
            result = assemble_artifact(read_json(args.manifest),chunk_root=args.chunks,output=args.out)
        elif args.operation in {'asset-snapshot','pack-export','pack-verify'}:
            from fsf.production.assets import snapshot_assets,export_pack,verify_pack
            if args.operation=='pack-verify':result=verify_pack(args.bundle)
            elif args.operation=='pack-export':result=export_pack(read_json(args.snapshot),library=args.library,output_root=args.out)
            else:
                result=snapshot_assets(read_json(args.selections),library=args.library)
                with edit_lock(args.out) as path:
                    if path.exists():raise ProductionError('PRODUCTION_TARGET_EXISTS')
                    publish(path,result)
        elif args.operation in {"workflow-inspect","pause","resume"}:
            from fsf.production.workflow import Workflow
            workflow=Workflow(args.workflow)
            result=workflow.inspect() if args.operation=="workflow-inspect" else workflow.pause(args.operation=="pause")
        else:
            from fsf.production.ledger import Ledger
            if args.operation == "budget-create":
                result = Ledger(args.out).create(read_json(args.authorization))
            elif args.operation == "budget-inspect":
                result = Ledger(args.ledger).inspect()
            else:
                slot=None
                if any(getattr(args,k) is not None for k in ("shot_id","segment_id","repair_round")):
                    slot={"logical_shot_id":args.shot_id,"segment_id":args.segment_id,
                          "repair_round":_integer(args.repair_round or "")}
                result = Ledger(args.ledger).reserve(args.request_id,route=args.route,worst_units=_integer(args.units),
                    generated_frames=_integer(args.frames),binding_digest=args.binding_digest,generation_slot=slot)
        print(json.dumps({"ok":True,"result":result},ensure_ascii=True))
        return 0
    except ProductionError as exc:
        code = exc.code
    except RemoteError as exc:
        code = exc.code
    except EditStoreError as exc:
        code = "PRODUCTION_LOCK_TIMEOUT" if exc.code == "EDIT_LOCK_TIMEOUT" else "PRODUCTION_INVALID"
    except AssetValidationError:
        code = "PRODUCTION_SOURCE_INVALID"
    except KeyboardInterrupt:
        code = "PRODUCTION_CANCELLED"
    except Exception:
        code = "PRODUCTION_INVALID"
    print(json.dumps({"ok":False,"error":{"code":code,"message":"Operation could not be completed"}}))
    return 130 if code == "PRODUCTION_CANCELLED" else 2
