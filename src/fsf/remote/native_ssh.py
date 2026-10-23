"""Native SSH transfer over an existing authorized worker disk."""
import base64
from importlib import resources
import json
from pathlib import Path
from fsf.core.edit_store import read_json
from fsf.remote.contracts import RemoteError,sha
from fsf.remote.native_store import NativeStore
from fsf.remote.kaggle_native import prepare,import_outputs

class NativeSsh:
    def __init__(self,adapter,config):self.adapter=adapter;self.config=config
    def remote_root(self,request):return self.adapter.settings['remote_root'].rstrip('/')+'/native-'+request['request_digest']
    def prepare(self,request,directory):
        # Native disk transport has no notebook payload-size constraint.
        store=NativeStore(read_json(self.config))
        try:prepare(request,store,directory)
        except RemoteError as exc:
            if exc.code!='REMOTE_KAGGLE_NATIVE_INPUT_TOO_LARGE':raise
        self.adapter.prepare(None,None,directory)
    def submit(self,request,directory):
        from fsf.remote.dispatch_hold import refuse_held_submit
        refuse_held_submit(directory)
        remote=self.remote_root(request);a=self.adapter;s=a.settings
        a._ssh([s['python'],'-c','import pathlib,sys;p=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);p.mkdir()',remote])
        a._scp(directory/'native-input.zip',s['host_alias']+':'+remote+'/input.zip')
        source=resources.files('fsf.tools').joinpath('remote_bootstrap_native.py').read_bytes()
        scope={'BUNDLE':remote+'/input.zip','BUNDLE_SHA':sha(directory/'native-input.zip'),'DEADLINE':request['policy']['deadline_seconds'],
            'WORK_ROOT':remote+'/runtime','RESULT_OUT':remote+'/result.zip','REQUEST_DIGEST':request['request_digest']}
        wrapper=directory/'native-launch.py'
        wrapper.write_text('import base64\nscope='+repr(scope)+'\nexec(compile(base64.b64decode('+repr(base64.b64encode(source).decode())+'),"native_bootstrap","exec"),scope)\n')
        a._scp(wrapper,s['host_alias']+':'+remote+'/launch.py')
        a._ssh([s['python'],'-c','import subprocess,sys;subprocess.Popen(sys.argv[1:],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True);print("launched")',s['python'],remote+'/launch.py'])
        return {'request_digest':request['request_digest'],'data_plane':'ssh_native_v1'}
    def status(self,request):
        path=self.remote_root(request)+'/runtime/native-status.json';s=self.adapter.settings
        text=self.adapter._ssh([s['python'],'-c','import pathlib,json,sys;p=pathlib.Path(sys.argv[1]);print(p.read_text() if p.is_file() else json.dumps({"status":"queued","request_digest":sys.argv[2]}))',path,request['request_digest']])
        value=json.loads(text)
        if value.get('request_digest')!=request['request_digest']:raise RemoteError('REMOTE_WORKER_IDENTITY')
        return value['status']
    def reconcile(self,request):
        text=self.adapter._ssh([self.adapter.settings['python'],'-c','import pathlib,sys;print(pathlib.Path(sys.argv[1]).is_dir())',self.remote_root(request)])
        return {'request_digest':request['request_digest'],'data_plane':'ssh_native_v1'} if text.strip()=='True' else None
    def fetch(self,request,directory):
        target=directory/'native-result.zip';target.unlink(missing_ok=True)
        self.adapter._scp(self.adapter.settings['host_alias']+':'+self.remote_root(request)+'/result.zip',target)
        return import_outputs(request,NativeStore(read_json(self.config)),target)
    def cancel(self,request):
        path=self.remote_root(request)+'/runtime/work/'+request['request_digest']+'/cancel'
        self.adapter._ssh([self.adapter.settings['python'],'-c','import pathlib,sys;p=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);p.touch();print("requested")',path])
        return 'requested'
