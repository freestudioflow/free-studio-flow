"""Explicit model API backend for compiled Studio tool steps."""
import os
from pathlib import Path
import tempfile

from fsf.core.edit_store import read_json
from fsf.executors.base import BaseVideoExecutor
from fsf.remote.bundle import copy_bounded
from fsf.remote.contracts import RemoteError, digest, sha, safe_path
from fsf.remote.model_api import run_api, validate_profile, verify_receipt


class ModelAPIExecutor(BaseVideoExecutor):
    requires_model_contract = True

    def __init__(self, *, executor_id, tool_name, profile, state_root, parameters=None, input_files=None, binding=None):
        self._id=executor_id
        self._tool=tool_name
        self.profile_path=Path(profile)
        self.state_root=Path(state_root)
        self.parameters=dict(parameters or {})
        self.input_files=dict(input_files or {})
        self.binding_path=Path(binding) if binding else None
        self.last_provenance=None
        self.last_submitted_delta=0

    @property
    def executor_id(self):return self._id

    @property
    def tool_name(self):return self._tool

    @property
    def execution_kind(self):return "api"

    @property
    def config_digest(self):
        value={"profile":read_json(self.profile_path),"parameters":self.parameters,
               "inputs":{k:sha(p) for k,p in self.input_files.items()}}
        if self.binding_path:value["binding"]=read_json(self.binding_path)
        return digest(value)

    def is_available(self):
        try:
            profile=validate_profile(read_json(self.profile_path))
            return profile["authorized"] and self.state_root.is_dir()
        except Exception:return False

    def validate_resume(self,record):
        try:
            provenance=record["remote_provenance"]
            receipt=verify_receipt(self.state_root/provenance["bundle"])
            return receipt["receipt_digest"]==provenance["result_digest"]
        except Exception:return False

    def execute_shot(self,task,output_dir):
        self.last_provenance=None
        self.last_submitted_delta=0
        profile=validate_profile(read_json(self.profile_path))
        expected=task.get("remote_expected_media_contract")
        if self.tool_name in {"studio.video.generate","studio.video.remaster"} and not profile["output"]["media_type"].startswith("video/"):
            raise RemoteError("API_TOOL_OUTPUT_TYPE")
        # A generation backend must explicitly implement the required visual
        # conditions. Generic model APIs cannot acknowledge these on its behalf.
        if task.get("remote_consistency_requirements") or task.get("video_condition"):
            raise RemoteError("API_CONDITIONS_REQUIRE_RECIPE")
        inputs={k:str(Path(p).absolute()) for k,p in self.input_files.items()}
        import shutil
        scratch=Path(tempfile.mkdtemp(prefix=".api-input-",dir=self.state_root))
        try:
            relative={}
            for name,path in inputs.items():
                local=name+Path(path).suffix
                copy_bounded(path,safe_path(scratch,local,exists=False));relative[name]=local
            parent=task.get("remote_parent",{})
            request={"schema_version":"fsf_model_api_request_v1",
                "request_id":"tool_"+digest({"parent":parent,"shot":task["id"],"tool":self.tool_name})[:40],
                "prompt":task["prompt"],"parameters":self.parameters,"inputs":relative,"expected_media_contract":expected}
            if self.binding_path:
                request["request_id"]="tool_"+digest({"request_id":request["request_id"],"binding":read_json(self.binding_path)})[:40]
            existing=(self.state_root/("scope_"+digest({"request_id":request["request_id"]})+".json")).exists()
            result=run_api(request,profile,inputs_root=scratch,state_root=self.state_root,
                           binding=read_json(self.binding_path) if self.binding_path else None,
                           recheck=task.get("resume_paused",False),wait=not task.get("cooperative_wait",False))
            self.last_submitted_delta=0 if existing else 1
            if task.get("cooperative_wait") and result["status"] in {"submitted","queued","running","remote_completed"}:
                return {"status":"pending_external","outputs":{},"resume_identity":{"request_digest":result["request_digest"]}}
            if result["status"]!="completed":raise RemoteError("API_NOT_COMPLETED")
            receipt=verify_receipt(self.state_root/result["bundle"])
            source=safe_path(self.state_root/result["bundle"] / "accepted",receipt["artifact"]["path"])
            target=safe_path(output_dir,task["artifact_filename"],exists=False)
            fd,temporary=tempfile.mkstemp(prefix=".api-publish-",dir=target.parent)
            os.close(fd);Path(temporary).unlink()
            try:
                copy_bounded(source,temporary);os.replace(temporary,target)
            finally:Path(temporary).unlink(missing_ok=True)
            self.last_provenance={"provider":"api","bundle":result["bundle"],"result_digest":receipt["receipt_digest"]}
            return self.build_tool_result(request_id=task["request_id"],shot_id=task["id"],artifact_path=target,
                artifact_sha256=receipt["artifact"]["sha256"],size_bytes=receipt["artifact"]["size_bytes"],duration_seconds=task["duration_seconds"])
        finally:shutil.rmtree(scratch)
