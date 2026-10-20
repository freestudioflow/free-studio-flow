"""Explicit opt-in PlanExecutor backend using the parent dispatch service."""
import os
from pathlib import Path
import shutil
import tempfile

from fsf.core.asset_library import _root
from fsf.core.edit_store import read_json
from fsf.executors.base import BaseVideoExecutor
from fsf.remote.bundle import prepare_bundle, copy_bounded
from fsf.remote.contracts import RemoteError, digest, sha, safe_path, validate_profile, validate_recipe
from fsf.remote.dispatch import submit_bundle, accepted_result, update_dispatch


class RemoteExecutor(BaseVideoExecutor):
    def __init__(self, *, executor_id, tool_name, profile, recipe, worker_wheel, state_root,
                 output_id, parameters=None, parameter_bindings=None, input_files=None):
        self._id=executor_id
        self._tool=tool_name
        self.profile_path=Path(profile)
        self.recipe_path=Path(recipe)
        self.wheel=Path(worker_wheel)
        self.state_root=Path(state_root)
        self.output_id=output_id
        self.parameters=dict(parameters or {})
        self.bindings=dict(parameter_bindings or {})
        self.input_files=dict(input_files or {})
        self.last_provenance=None
        self.last_submitted_delta=0

    @property
    def executor_id(self):return self._id

    @property
    def tool_name(self):return self._tool

    @property
    def execution_kind(self):return "remote"

    @property
    def config_digest(self):
        return digest({"profile":digest(read_json(self.profile_path)),"recipe":digest(read_json(self.recipe_path)),
            "wheel":sha(self.wheel),"output_id":self.output_id,"parameters":self.parameters,"bindings":self.bindings,
            "inputs":{k:sha(p) for k,p in self.input_files.items()}})

    def is_available(self):
        try:
            validate_profile(read_json(self.profile_path));validate_recipe(read_json(self.recipe_path))
            return self.wheel.is_file() and self.state_root.is_dir()
        except Exception:return False

    def validate_resume(self, record):
        provenance=record.get("remote_provenance")
        if not provenance:return False
        try:
            manifest,_=accepted_result(provenance["dispatch_id"],profile=read_json(self.profile_path),state_root=self.state_root)
            return manifest["result_digest"]==provenance["result_digest"]
        except Exception:return False

    def execute_shot(self, task, output_dir):
        self.last_provenance=None;self.last_submitted_delta=0
        profile=validate_profile(read_json(self.profile_path))
        recipe=validate_recipe(read_json(self.recipe_path))
        root=_root(self.state_root)
        scratch=Path(tempfile.mkdtemp(prefix=".remote-task-",dir=root))
        try:
            parameters=dict(self.parameters)
            for name,field in self.bindings.items():
                if field not in {"prompt","duration_seconds","id"}:raise RemoteError("REMOTE_PARAMETER_BINDING")
                parameters[name]=task[field]
            inputs={}
            for item in recipe["inputs"]:
                name=item["artifact_id"]
                path=self.input_files.get(name)
                if not path and name=="input":path=task.get("input_artifact")
                if not path:raise RemoteError("REMOTE_INPUTS")
                source=Path(path)
                extension=source.suffix
                relative=name+extension
                copy_bounded(source,safe_path(scratch,relative,exists=False))
                inputs[name]=relative
            definition={"parent":task["remote_parent"],"parameters":parameters,"inputs":inputs,
                "expected_media_contract":task.get("remote_expected_media_contract"),
                "video_condition":task.get("video_condition"),"consistency_requirements":task.get("remote_consistency_requirements")}
            prepared=prepare_bundle(definition,recipe,inputs_root=scratch,wheel=self.wheel,profile=profile,output_root=root)
            existing=(root/prepared["dispatch_id"]/"ledger.json").exists()
            submitted=submit_bundle(root/prepared["bundle"],profile=profile,state_root=root)
            self.last_provenance={"dispatch_id":prepared["dispatch_id"],"request_digest":prepared["request_digest"],
                "provider":profile["transport"],"submission_state":submitted["status"]}
            self.last_submitted_delta=0 if existing else submitted["submitted_count"]
            from fsf.cli.remote import wait_dispatch
            if task.get("cooperative_wait"):
                if submitted["status"]=="submission_unknown":
                    submitted=update_dispatch(prepared["dispatch_id"],profile=profile,state_root=root,action="reconcile")
                state=update_dispatch(prepared["dispatch_id"],profile=profile,state_root=root)
                if state["status"] in {"submitted","queued","running"}:
                    return {"status":"pending_external","outputs":{},"resume_identity":self.last_provenance}
                if state["status"]=="remote_completed":
                    update_dispatch(prepared["dispatch_id"],profile=profile,state_root=root,action="fetch")
                elif state["status"]!="verified":
                    raise RemoteError("REMOTE_SUBMISSION_UNKNOWN" if state["status"]=="submission_unknown" else "REMOTE_PROVIDER_FAILED")
            else:
                wait_dispatch(prepared["dispatch_id"],profile=profile,state_root=root)
            manifest,folder=accepted_result(prepared["dispatch_id"],profile=profile,state_root=root)
            selected=[a for a in manifest["artifacts"] if a["artifact_id"]==self.output_id]
            if len(selected)!=1:raise RemoteError("REMOTE_OUTPUT_MAPPING")
            item=selected[0]
            if self.tool_name in {"studio.video.generate","studio.video.remaster"} and item["role"]!="video":
                raise RemoteError("REMOTE_VIDEO_ROLE")
            source=safe_path(folder/"outputs",item["path"])
            target=safe_path(output_dir,task["artifact_filename"],exists=False)
            # Verified bytes and child provenance are published to the parent
            # output path; the parent still runs its own QC/review steps.
            fd,temp=tempfile.mkstemp(prefix=".remote-artifact-",dir=target.parent);os.close(fd);Path(temp).unlink()
            try:
                copy_bounded(source,temp)
                os.replace(temp,target)
            finally:Path(temp).unlink(missing_ok=True)
            self.last_provenance.update(result_digest=manifest["result_digest"],child=manifest["child"],
                                        child_journal_sha256=manifest["child_journal_sha256"])
            self.last_provenance["consistency_acknowledgement"]=manifest["consistency_acknowledgement"]
            return self.build_tool_result(request_id=task["request_id"],shot_id=task["id"],artifact_path=target,
                artifact_sha256=item["sha256"],size_bytes=item["size_bytes"],duration_seconds=task["duration_seconds"])
        except RemoteError as exc:
            return {"status":"failed","outputs":{},"error":exc.code}
        finally:shutil.rmtree(scratch)
