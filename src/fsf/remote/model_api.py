"""Capability-typed model APIs. Configurations are versioned data, not code."""
import base64
import copy
import hashlib
from importlib import resources
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, quote
from urllib.request import Request, build_opener

from jsonschema import Draft202012Validator
from fsf.core.asset_library import _root
from fsf.core.edit_store import read_json, decode_json, edit_lock
from fsf.remote.contracts import RemoteError, digest, sha, seal, check_digest, safe_path, write_json, reject_credential_bytes, reject_inline_credentials
from fsf.remote.transports import NoRedirect
from fsf.remote.credentials import credential_value, credential_scope, bedrock_client, credential_binding_identity


def _shape(value,name):
    version='2' if name=='profile' and value.get('schema_version')=='fsf_model_api_profile_v2' else '1'
    schema=json.loads(resources.files("fsf.schemas").joinpath("fsf_model_api_"+name+"_v"+version+".schema.json").read_text("utf-8"))
    if not Draft202012Validator(schema).is_valid(value):raise RemoteError("API_SCHEMA")
    decode_json(json.dumps(value,allow_nan=False).encode())


def _anonymous(profile):
    """Only an explicit v2 HTTP profile can opt out of credentials."""
    return (profile['schema_version']=='fsf_model_api_profile_v2'
            and profile['protocol']=='json_http_v1' and profile['key_env'] is None
            and isinstance(profile['http'],dict) and profile['http'].get('auth_header')=='none')


def _job_identity(value,previous=None):
    if not isinstance(value,str) or not value or len(value)>1024 or any(ord(c)<32 for c in value):
        raise RemoteError('API_JOB_ID_INVALID')
    if previous is not None and value!=previous:raise RemoteError('API_JOB_ID_CHANGED')
    return value


def validate_profile(value):
    reject_inline_credentials(value)
    value=copy.deepcopy(value);_shape(value,"profile")
    Draft202012Validator.check_schema(value["parameter_schema"])
    if value["protocol"]=="bedrock_converse_v1":
        if value["capability"]!="text" or value["base_url"] is not None or value["key_env"] is not None or not value["region"]:
            raise RemoteError("API_PROTOCOL_CAPABILITY")
    else:
        p=urlsplit(value["base_url"] or "")
        loopback=p.hostname in {"localhost","127.0.0.1","::1"}
        if p.username or p.password or p.query or p.fragment or not p.hostname or ".." in p.path.split("/"):
            raise RemoteError("API_ENDPOINT")
        if p.scheme!="https" and not (p.scheme=="http" and loopback and value["allow_loopback"]):
            raise RemoteError("API_ENDPOINT")
        if not value["key_env"] and not _anonymous(value):raise RemoteError("API_AUTH_CONFIG")
    if value["protocol"] in {"openai_chat_v1","gemini_content_v1"} and value["capability"]!="text":
        raise RemoteError("API_PROTOCOL_CAPABILITY")
    if value["protocol"]=="json_http_v1":
        if value["http"] is None:raise RemoteError("API_HTTP_CONFIG")
        if value['output']['max_bytes']>67108864 and value['http']['response_mode']!='url':
            raise RemoteError('API_STREAMING_URL_REQUIRED')
        for route in value["http"]["routes"].values():
            if route is None:continue
            if not route.startswith("/") or route.startswith("//") or "?" in route or "#" in route or ".." in route.split("/"):
                raise RemoteError("API_ROUTE")
    elif value["http"] is not None:raise RemoteError("API_HTTP_CONFIG")
    expected={"text":"text/","image":"image/","video":"video/","audio":"audio/"}
    prefix=expected.get(value["capability"])
    if prefix and not value["output"]["media_type"].startswith(prefix):raise RemoteError("API_OUTPUT_TYPE")
    if value["capability"]=="upscale" and not value["output"]["media_type"].startswith(("image/","video/")):
        raise RemoteError("API_OUTPUT_TYPE")
    return value


def pointer(value,path):
    if path=="":return value
    if not isinstance(path,str) or not path.startswith("/"):raise RemoteError("API_RESPONSE_MAPPING")
    for token in path[1:].split("/"):
        token=token.replace("~1","/").replace("~0","~")
        try:
            if isinstance(value,list):
                if not re.fullmatch(r"0|[1-9][0-9]*",token):raise ValueError()
                value=value[int(token)]
            elif isinstance(value,dict):value=value[token]
            else:raise ValueError()
        except (ValueError,KeyError,IndexError):raise RemoteError("API_RESPONSE_MAPPING") from None
    return value


def _http(profile,method,route,body=None,*,deadline,artifact_url=None):
    base=profile["base_url"].rstrip("/")
    url=artifact_url or base+route
    p=urlsplit(url);base_parts=urlsplit(base)
    if artifact_url:
        if p.hostname not in profile["artifact_hosts"] or p.username or p.password:
            raise RemoteError("API_ARTIFACT_HOST")
        if p.scheme!="https" and not (profile["allow_loopback"] and p.scheme=="http" and p.hostname in {"127.0.0.1","localhost","::1"}):
            raise RemoteError("API_ARTIFACT_HOST")
    remaining=deadline-time.monotonic()
    if remaining<=0:raise RemoteError("API_DEADLINE_EXCEEDED")
    key=None if _anonymous(profile) else credential_value(profile)
    if not key and not _anonymous(profile):raise RemoteError("API_AUTH_MISSING")
    headers={"Content-Type":"application/json"}
    header="x-goog-api-key" if profile["protocol"]=="gemini_content_v1" else (profile["http"]["auth_header"] if profile["http"] else "Authorization")
    if header!="none" and (not artifact_url or (p.scheme,p.netloc)==(base_parts.scheme,base_parts.netloc)):
        headers[header]=("Bearer " if header=="Authorization" else "")+key
    data=json.dumps(body,allow_nan=False).encode() if body is not None else None
    if data is not None and len(data)>profile.get('request_max_bytes',12*1024*1024):raise RemoteError("API_INPUT_LIMIT")
    downloaded=None
    try:
        with build_opener(NoRedirect).open(Request(url,data=data,method=method,headers=headers),timeout=min(remaining,60)) as response:
            if artifact_url and profile['schema_version']=='fsf_model_api_profile_v2':
                fd,name=tempfile.mkstemp(prefix='fsf-api-download-');downloaded=Path(name)
                total=0
                with os.fdopen(fd,'wb') as sink:
                    while True:
                        if time.monotonic()>=deadline:raise RemoteError('API_DEADLINE_EXCEEDED')
                        block=response.read1(65536)
                        if not block:break
                        total+=len(block)
                        if total>profile['output']['max_bytes']:raise RemoteError('API_OUTPUT_LIMIT')
                        if shutil.disk_usage(downloaded.parent).free<len(block):raise RemoteError('API_DISK_LIMIT')
                        sink.write(block)
                    sink.flush();os.fsync(sink.fileno())
                result=downloaded;downloaded=None
                return result
            chunks=[];total=0
            while True:
                if time.monotonic()>=deadline:raise RemoteError("API_DEADLINE_EXCEEDED")
                chunk=response.read1(65536)
                if not chunk:break
                total+=len(chunk)
                if total>min(profile["output"]["max_bytes"],67108864)+1024*1024:raise RemoteError("API_RESPONSE_LIMIT")
                chunks.append(chunk)
            return b"".join(chunks)
    except HTTPError as exc:
        if exc.code in (401,403):code="API_AUTH_FAILED"
        elif exc.code==429:code="API_RATE_LIMITED"
        elif 400<=exc.code<500:code="API_REQUEST_REJECTED"
        else:code="API_SUBMISSION_UNKNOWN"
        raise RemoteError(code) from None
    except RemoteError:raise
    except Exception:raise RemoteError("API_CONNECTION_UNKNOWN") from None
    finally:
        if downloaded is not None:downloaded.unlink(missing_ok=True)


def _body(template,values):
    if isinstance(template,dict):return {k:_body(v,values) for k,v in template.items()}
    if isinstance(template,list):return [_body(v,values) for v in template]
    if isinstance(template,str) and re.fullmatch(r"\{[a-zA-Z0-9_.-]+\}",template):
        key=template[1:-1]
        if key not in values:raise RemoteError("API_TEMPLATE_PARAMETER")
        return values[key]
    if isinstance(template,str) and re.search(r"\{(?:prompt|model|params\.|inputs\.)",template):
        raise RemoteError("API_TEMPLATE_PARAMETER")
    return template


def _route(template,ledger):
    if template is None:raise RemoteError("API_METHOD_UNSUPPORTED")
    result=template
    for name in ("job_id","request_id"):
        if "{"+name+"}" in result:
            value=ledger.get(name)
            if not isinstance(value,str) or not value or len(value)>512:raise RemoteError("API_JOB_ID")
            if any(t in (".","..","") for t in value.split("/")) or any(c in value for c in "?\\#\r\n"):
                raise RemoteError("API_JOB_ID")
            result=result.replace("{"+name+"}",quote(value,safe="/"))
    if "{" in result:raise RemoteError("API_ROUTE")
    return result


def _native(profile,request,deadline):
    params=request["parameters"]
    tokens=params.get("max_tokens",profile["max_tokens"])
    temperature=params.get("temperature",0)
    if profile["protocol"]=="openai_chat_v1":
        raw=_http(profile,"POST","/chat/completions",{"model":profile["model"],"messages":[{"role":"user","content":request["prompt"]}],
             "max_tokens":tokens,"temperature":temperature},deadline=deadline)
        response=decode_json(raw)
        if pointer(response,"/choices/0").get("finish_reason") not in (None,"stop"):
            raise RemoteError("API_INCOMPLETE_OUTPUT")
        text=pointer(response,"/choices/0/message/content")
        return text.encode("utf-8"),raw,response.get("model")
    if profile["protocol"]=="gemini_content_v1":
        if not re.fullmatch(r"[A-Za-z0-9_.-]+",profile["model"]):raise RemoteError("API_MODEL_ID")
        raw=_http(profile,"POST","/models/"+profile["model"]+":generateContent",{
            "contents":[{"role":"user","parts":[{"text":request["prompt"]}]}],
            "generationConfig":{"maxOutputTokens":tokens,"temperature":temperature}},deadline=deadline)
        response=decode_json(raw)
        if pointer(response,"/candidates/0").get("finishReason") not in (None,"STOP"):
            raise RemoteError("API_INCOMPLETE_OUTPUT")
        text=pointer(response,"/candidates/0/content/parts/0/text")
        return text.encode("utf-8"),raw,response.get("modelVersion")
    try:
        import boto3
        from botocore.config import Config
    except ImportError:raise RemoteError("API_BEDROCK_DEPENDENCY_MISSING") from None
    try:
        client=bedrock_client(profile,Config(connect_timeout=10,
            read_timeout=max(1,int(deadline-time.monotonic())),retries={"total_max_attempts":1}))
        response=client.converse(modelId=profile["model"],messages=[{"role":"user","content":[{"text":request["prompt"]}]}],
            inferenceConfig={"maxTokens":tokens,"temperature":temperature})
    except Exception as exc:
        provider_code=getattr(exc,"response",{}).get("Error",{}).get("Code","")
        code={"AccessDeniedException":"API_BEDROCK_ACCESS_DENIED", "ExpiredTokenException":"API_BEDROCK_EXPIRED",
              "ThrottlingException":"API_RATE_LIMITED", "ValidationException":"API_BEDROCK_MODEL_UNAVAILABLE"}.get(provider_code,"API_BEDROCK_REQUEST_FAILED")
        raise RemoteError(code) from None
    text=pointer(response,"/output/message/content/0/text")
    if response.get("stopReason") not in (None,"end_turn","stop_sequence"):
        raise RemoteError("API_INCOMPLETE_OUTPUT")
    return text.encode("utf-8"),json.dumps(response,default=str).encode(),profile["model"]


def _extract(profile,raw,deadline):
    http=profile["http"]
    mode=http["response_mode"]
    if mode=="binary":return raw
    value=pointer(decode_json(raw),http["result_pointer"])
    if not isinstance(value,str) or not value:raise RemoteError("API_RESPONSE_MAPPING")
    if mode=="text":return value.encode("utf-8")
    if mode=="base64":
        try:return base64.b64decode(value,validate=True)
        except Exception:raise RemoteError("API_RESPONSE_MAPPING") from None
    return _http(profile,"GET","",deadline=deadline,artifact_url=value)


def validate_artifact(path,profile,request,inputs,deadline):
    if not 0<Path(path).stat().st_size<=profile["output"]["max_bytes"]:raise RemoteError("API_OUTPUT_LIMIT")
    capability=profile["capability"];media=profile["output"]["media_type"]
    if capability=="text":
        text=Path(path).read_text("utf-8")
        if not text.strip():raise RemoteError("API_EMPTY_OUTPUT")
        return "text_response"
    if media.startswith("image/"):
        from PIL import Image
        with Image.open(path) as image:
            w,h=image.size
            if min(w,h)<1 or max(w,h)>profile["output"]["max_dimension"]:raise RemoteError("API_DIMENSION_LIMIT")
            expected={"image/png":"PNG","image/jpeg":"JPEG","image/webp":"WEBP"}.get(media)
            if image.format!=expected:raise RemoteError("API_MEDIA_TYPE")
            image.load()
        if capability=="upscale":
            if len(inputs)!=1:raise RemoteError("API_UPSCALE_INPUT")
            with Image.open(next(iter(inputs.values()))) as original:
                scale=request["parameters"].get("scale",2)
                if type(scale) not in (int,float) or not 1<scale<=8 or w!=round(original.width*scale) or h!=round(original.height*scale):
                    raise RemoteError("API_UPSCALE_DIMENSIONS")
        return "decoded"
    from fsf.conformance.media import ensure_media_tools_on_path,SafeFfprobeRunner,evaluate_media_file
    from fsf.executors.subprocess_safety import run_safe_subprocess
    ffmpeg,ffprobe=ensure_media_tools_on_path()
    if not ffmpeg or not ffprobe:raise RemoteError("API_MEDIA_TOOLS")
    info=SafeFfprobeRunner(ffprobe).probe(path)
    streams=info.get("streams",[])
    videos=[s for s in streams if s.get("codec_type")=="video"]
    if media.startswith("video/") and (len(videos)!=1 or max(videos[0]["width"],videos[0]["height"])>profile["output"]["max_dimension"]):
        raise RemoteError("API_DIMENSION_LIMIT")
    duration=float(info.get("format",{}).get("duration",0))
    if not 0<duration<=profile["output"]["max_duration_seconds"]:raise RemoteError("API_DURATION_LIMIT")
    if media.startswith("video/"):
        if not request["expected_media_contract"]:raise RemoteError("API_VIDEO_CONTRACT_REQUIRED")
        report,_action=evaluate_media_file(path,expected_contract=request["expected_media_contract"])
        if report.get("evaluation_status")!="conforms":raise RemoteError("API_MEDIA_QC_FAILED")
        if capability=="upscale":
            if len(inputs)!=1:raise RemoteError("API_UPSCALE_INPUT")
            old=SafeFfprobeRunner(ffprobe).probe(next(iter(inputs.values())))
            a=next(s for s in old["streams"] if s["codec_type"]=="video")
            b=next(s for s in streams if s["codec_type"]=="video")
            scale=request["parameters"].get("scale",2)
            if b["width"]!=round(a["width"]*scale) or b["height"]!=round(a["height"]*scale):raise RemoteError("API_UPSCALE_DIMENSIONS")
        return "conforms"
    if not any(s["codec_type"]=="audio" for s in streams) or any(s["codec_type"]=="video" for s in streams):
        raise RemoteError("API_MEDIA_TYPE")
    remaining=deadline-time.monotonic()
    if remaining<=0:raise RemoteError("API_DEADLINE_EXCEEDED")
    res=run_safe_subprocess([ffmpeg,"-nostdin","-v","error","-xerror","-i",str(path),"-f","null","-"],
                            timeout_seconds=remaining,output_limit_bytes=65536)
    if res.returncode or res.timed_out:raise RemoteError("API_AUDIO_DECODE_FAILED")
    return "decoded"


def run_api(request,profile,*,inputs_root,state_root,action="run",wait=True,binding=None,recheck=False):
    profile=validate_profile(profile)
    from fsf.remote.onboarding import connection_attempt
    with credential_scope(profile,binding):
        if action == "run":
            with connection_attempt(profile,binding,_root(state_root),recheck=recheck) as state:
                result=_run_api(request,profile,inputs_root=inputs_root,state_root=state_root,action=action,wait=wait)
                state["verified_inference"] = result.get("status")=="completed"
                if result.get("status")=="submission_unknown":
                    state.update(status="paused_unknown",last_error="API_SUBMISSION_UNKNOWN")
                return result
        return _run_api(request,profile,inputs_root=inputs_root,state_root=state_root,action=action,wait=wait)


def _run_api(request,profile,*,inputs_root,state_root,action="run",wait=True):
    profile=validate_profile(profile);_shape(request,"request")
    if not Draft202012Validator(profile["parameter_schema"]).is_valid(request["parameters"]):raise RemoteError("API_PARAMETERS")
    tokens=request["parameters"].get("max_tokens",profile["max_tokens"])
    if type(tokens) is not int or not 0<tokens<=profile["max_tokens"]:raise RemoteError("API_TOKEN_LIMIT")
    source=_root(inputs_root);root=_root(state_root)
    inputs={k:safe_path(source,p) for k,p in request["inputs"].items()}
    if any(p.stat().st_size>profile.get('input_max_bytes',8*1024*1024) for p in inputs.values()):raise RemoteError("API_INPUT_LIMIT")
    input_shas={k:sha(p) for k,p in inputs.items()}
    identity={"request":request,"profile":digest(profile),"inputs":input_shas}
    if credential_binding_identity(profile) is not None:
        identity["credential_binding_digest"]=credential_binding_identity(profile)
    key=digest(identity)
    directory=root/("api_"+key)
    # A caller-supplied request ID is immutable even if its POST response was
    # lost. Changing the prompt/profile cannot bypass an unresolved intent.
    scope=digest({"request_id":request["request_id"]})
    with edit_lock(root/("scope_"+scope)), edit_lock(root/("api_"+key+".lock-target")):
        if not profile["authorized"]:raise RemoteError("API_NOT_AUTHORIZED")
        identity_path=root/("scope_"+scope+".json")
        if identity_path.exists():
            identity=read_json(identity_path);check_digest(identity,"identity_digest")
            if identity.get("request_digest")!=key:raise RemoteError("API_REQUEST_ID_CONFLICT")
        else:
            write_json(identity_path,seal({"request_id":request["request_id"],"request_digest":key},"identity_digest"))
        if directory.exists():
            ledger=read_json(directory/"ledger.json")
            check_digest(ledger,"ledger_digest")
            if ledger.get("request_digest")!=key or ledger.get("profile_digest")!=digest(profile):raise RemoteError("API_INTENT_CONFLICT")
        else:
            if action!="run":raise RemoteError("API_INTENT_MISSING")
            directory.mkdir()
            ledger={"request_digest":key,"profile_digest":digest(profile),"request_id":request["request_id"],"status":"prepared","job_id":None,
                    "created_at":time.time(),"result_digest":None,"last_error":None,"submission_started":False}
            write_json(directory/"ledger.json",seal(ledger,"ledger_digest"))
        # Publication is atomic before the ledger update. Recover that crash
        # window from the complete bound receipt, without contacting a provider.
        if (directory/'accepted').exists() and ledger['status']!='completed':
            receipt=verify_receipt(directory)
            if receipt['request_digest']!=key or receipt['profile_digest']!=digest(profile):
                raise RemoteError('API_INTENT_CONFLICT')
            ledger.update(status='completed',result_digest=receipt['receipt_digest'],last_error=None)
            write_json(directory/'ledger.json',seal(ledger,'ledger_digest'))
        if ledger["status"]=="completed":
            receipt=verify_receipt(directory)
            if receipt["request_digest"]!=key or receipt["profile_digest"]!=digest(profile) or receipt["receipt_digest"]!=ledger["result_digest"]:
                raise RemoteError("API_INTENT_CONFLICT")
            return {"status":"completed","request_digest":key,"bundle":directory.name,"receipt_digest":receipt["receipt_digest"]}
        extended=profile['schema_version']=='fsf_model_api_profile_v2'
        # The original submission budget is never renewed. Observing an existing
        # provider intent is a separate bounded operation and cannot create a job.
        observation=extended and action in {'status','fetch','reconcile','cancel'}
        remaining=(profile.get('observation_timeout_seconds',60) if observation else
                   max(0,profile["timeout_seconds"]-(time.time()-ledger["created_at"])))
        deadline=time.monotonic()+remaining
        def save():write_json(directory/"ledger.json",seal(ledger,"ledger_digest"))
        http=profile["http"]
        if extended and ledger['status'] in {'failed','cancelled'}:
            return {'status':ledger['status'],'request_digest':key,'error_code':ledger['last_error']}
        if action=="cancel":
            if not http or not http["routes"]["cancel"]:return {"status":ledger["status"],"cancel":"unsupported","request_digest":key}
            _http(profile,"POST",_route(http["routes"]["cancel"],ledger),{},deadline=deadline)
            ledger["status"]="cancel_requested";save()
            return {"status":"cancel_requested","request_digest":key}
        if action=="reconcile":
            if not http or not http["routes"]["reconcile"]:return {"status":ledger["status"],"reconcile":"unsupported","request_digest":key}
            response=decode_json(_http(profile,"GET",_route(http["routes"]["reconcile"],ledger),deadline=deadline))
            ledger["job_id"]=_job_identity(pointer(response,http["job_id_pointer"]),ledger['job_id']);ledger["status"]="submitted";save()
        if ledger["status"]=="submission_unknown":return {"status":"submission_unknown","request_digest":key}
        if ledger["status"] in {"failed","cancelled"}:return {"status":ledger["status"],"request_digest":key,"error_code":ledger["last_error"]}
        raw=None;data=None;reported_model=None
        try:
            if ledger["status"]=="prepared":
                if action!="run":return {"status":"prepared","request_digest":key}
                if extended and time.monotonic()>=deadline:raise RemoteError('API_DEADLINE_EXCEEDED')
                if profile["protocol"]!="bedrock_converse_v1" and not _anonymous(profile) and not credential_value(profile):raise RemoteError("API_AUTH_MISSING")
                ledger.update(status="submission_unknown",submission_started=True);save()
                if profile["protocol"]!="json_http_v1":data,raw,reported_model=_native(profile,request,deadline)
                else:
                    values={"prompt":request["prompt"],"model":profile["model"],"request_id":request["request_id"]}
                    values.update({"params."+k:v for k,v in request["parameters"].items()})
                    for name,path in inputs.items():
                        encoded=base64.b64encode(path.read_bytes()).decode()
                        values["inputs."+name+".base64"]=encoded
                        values["inputs."+name+".data_uri"]="data:"+(mimetypes.guess_type(path.name)[0] or "application/octet-stream")+";base64,"+encoded
                    raw=_http(profile,"POST",http["routes"]["submit"],_body(http["body"],values),deadline=deadline)
                    if http["job_id_pointer"]:
                        ledger["job_id"]=_job_identity(pointer(decode_json(raw),http["job_id_pointer"]))
                        ledger["status"]="submitted";save()
                    else:data=_extract(profile,raw,deadline)
            if data is None:
                if not http or not http["routes"]["status"]:raise RemoteError("API_RESPONSE_MAPPING")
                while True:
                    raw=_http(profile,"GET",_route(http["routes"]["status"],ledger),deadline=deadline)
                    status=pointer(decode_json(raw),http["status_pointer"])
                    normalized=http["status_map"].get(str(status))
                    if normalized is None:raise RemoteError("API_STATUS_UNKNOWN")
                    ledger["status"]="remote_completed" if normalized=="completed" else normalized;save()
                    if normalized in {"failed","cancelled"}:raise RemoteError("API_PROVIDER_FAILED")
                    if normalized=="completed":break
                    if not wait:return {"status":normalized,"request_digest":key}
                    if time.monotonic()+0.2>=deadline:raise RemoteError("API_DEADLINE_EXCEEDED")
                    time.sleep(0.2)
                if http["routes"]["fetch"]:raw=_http(profile,"GET",_route(http["routes"]["fetch"],ledger),deadline=deadline)
                data=_extract(profile,raw,deadline)
            scratch=Path(tempfile.mkdtemp(prefix=".api-output-",dir=directory))
            try:
                name="result"+profile["output"]["suffix"]
                target=scratch/name
                if isinstance(data,Path):
                    try:shutil.copyfile(data,target)
                    finally:data.unlink(missing_ok=True)
                else:target.write_bytes(data)
                reject_credential_bytes(target,[credential_value(profile) if profile["key_env"] else None])
                quality=validate_artifact(target,profile,request,inputs,deadline)
                if input_shas!={k:sha(p) for k,p in inputs.items()}:raise RemoteError("API_INPUT_CHANGED")
                receipt=seal({"schema_version":"fsf_model_api_receipt_v1","request_digest":key,"profile_digest":digest(profile),
                    "capability":profile["capability"],"model":profile["model"],"reported_model":reported_model,
                    "artifact":{"path":name,"sha256":sha(target),"size_bytes":target.stat().st_size,"media_type":profile["output"]["media_type"]},
                    "quality":quality,"review":"not_reviewed","provider_response_sha256":hashlib.sha256(raw).hexdigest()},"receipt_digest")
                _shape(receipt,"receipt");write_json(scratch/"receipt.json",receipt)
                os.replace(scratch,directory/"accepted")
                ledger.update(status="completed",result_digest=receipt["receipt_digest"]);save()
                return {"status":"completed","request_digest":key,"bundle":directory.name,"receipt_digest":receipt["receipt_digest"]}
            finally:
                if scratch.exists():shutil.rmtree(scratch)
        except KeyboardInterrupt:
            ledger["last_error"]="API_CANCELLED";save();raise RemoteError("API_CANCELLED") from None
        except Exception as exc:
            code=exc.code if isinstance(exc,RemoteError) else "API_RESPONSE_FAILED"
            ledger["last_error"]=code
            credential_error=code in {'API_AUTH_MISSING','API_SECRET_UNAVAILABLE','API_SECRET_EXPIRED','API_SECRET_REVISION','API_KEYRING_UNAVAILABLE'}
            if credential_error and ledger['status']=='prepared' and ledger.get('submission_started') is False:
                save();raise RemoteError(code) from None
            recoverable=ledger['job_id'] is not None and (credential_error or (extended and code in {
                'API_DEADLINE_EXCEEDED','API_CONNECTION_UNKNOWN','API_SUBMISSION_UNKNOWN',
                'API_AUTH_FAILED','API_RATE_LIMITED','API_STATUS_UNKNOWN','API_AUTH_MISSING',
                'API_SECRET_UNAVAILABLE','API_SECRET_EXPIRED','API_SECRET_REVISION','API_KEYRING_UNAVAILABLE'}))
            if ledger["status"]!="submission_unknown" and not recoverable:
                if not (extended and ledger['status']=='cancelled'):ledger["status"]="failed"
            save();raise RemoteError(code) from None


def verify_receipt(directory):
    directory=Path(directory)
    receipt=read_json(directory/"accepted/receipt.json");_shape(receipt,"receipt");check_digest(receipt,"receipt_digest")
    path=safe_path(directory/"accepted",receipt["artifact"]["path"])
    if sha(path)!=receipt["artifact"]["sha256"] or path.stat().st_size!=receipt["artifact"]["size_bytes"]:
        raise RemoteError("API_ARTIFACT_INTEGRITY")
    return receipt
