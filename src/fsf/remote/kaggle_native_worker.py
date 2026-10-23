"""Run the existing v2 worker against provider-native staged bytes."""
import argparse
from pathlib import Path
from fsf.core.edit_store import read_json
from fsf.remote.contracts import sha,RemoteError
from fsf.remote.native_store import NativeStore
from fsf.remote.v2 import run_worker,validate_request
from fsf.remote.kaggle_native import export_outputs

def main():
    parser=argparse.ArgumentParser();parser.add_argument('root');parser.add_argument('--out',required=True)
    args=parser.parse_args();root=Path(args.root)
    config=read_json(root/'store.json');config['root']=str(root/'objects')
    store=NativeStore(config);request=validate_request(read_json(root/'request.json'),store)
    for ref in [request['worker_wheel'],*(a['reference'] for a in request['inputs'])]:
        source=root/'blobs'/ref['sha256']
        actual=store.put(source,artifact_id=ref['artifact_id'],key=ref['object_key'],role=ref['role'],media_type=ref['media_type'],state_root=root)
        if actual!=ref:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
    (root/'work').mkdir(exist_ok=True)
    result=run_worker(request,store=store,output_root=root/'work',approved_recipe=request['recipe']['recipe_digest'])
    export_outputs(request,store,args.out)
    return 0 if result['status']=='completed' else 2

if __name__=='__main__':raise SystemExit(main())
