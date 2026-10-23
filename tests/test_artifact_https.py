"""Actual SDK SigV4 over loopback TLS, including separate CLI/worker processes."""
import base64
import datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import ipaddress
import json
import multiprocessing
import ssl
import select
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qsl,urlsplit,quote

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from fsf.remote.artifacts import ArtifactStore,store_public_identity
from fsf.remote.contracts import seal,write_json
from test_remote_artifacts import profile as store_profile
from test_remote_dispatch import remote_wheel,template

ACCESS='SYNTHETIC-FSF-ACCESS'
SECRET='fixture-'+'signing-material'


@pytest.fixture
def service(tmp_path_factory,monkeypatch):
    root=tmp_path_factory.mktemp('tls')
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1))
          .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
          .sign(key,hashes.SHA256()))
    (root/'ca.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (root/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    objects={};observed=[]
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*a):pass
        def reply(self,status,body=b'',headers=None):
            self.send_response(status)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Connection','close')
            for k,v in (headers or {}).items():self.send_header(k,str(v))
            self.end_headers()
            if self.command!='HEAD':self.wfile.write(body)
        def signed(self):
            try:
                auth=self.headers['Authorization'];scheme,rest=auth.split(' ',1)
                parts=dict(item.strip().split('=',1) for item in rest.split(','))
                access,date,region,service,end=parts['Credential'].split('/')
                assert scheme=='AWS4-HMAC-SHA256' and access==ACCESS and service=='s3' and end=='aws4_request'
                parsed=urlsplit(self.path)
                query='&'.join(quote(k,safe='-_.~')+'='+quote(v,safe='-_.~') for k,v in sorted(parse_qsl(parsed.query,keep_blank_values=True)))
                headers=''.join(name+':'+ ' '.join(self.headers[name].split())+'\n' for name in parts['SignedHeaders'].split(';'))
                canonical='\n'.join([self.command,quote(parsed.path,safe='/-_.~'),query,headers,parts['SignedHeaders'],self.headers['X-Amz-Content-SHA256']])
                scope='/'.join([date,region,service,end])
                string='\n'.join([scheme,self.headers['X-Amz-Date'],scope,hashlib.sha256(canonical.encode()).hexdigest()])
                signing=('AWS4'+SECRET).encode()
                for value in (date,region,service,end):signing=hmac.new(signing,value.encode(),hashlib.sha256).digest()
                assert hmac.compare_digest(hmac.new(signing,string.encode(),hashlib.sha256).hexdigest(),parts['Signature'])
                observed.append(self.command);return True
            except Exception:
                self.reply(403,b'<Error><Code>AccessDenied</Code></Error>',{'Content-Type':'application/xml'});return False
        def do_GET(self):
            if urlsplit(self.path).path=='/installation-smoke':return self.reply(200,b'installation-network-ok')
            if not self.signed():return
            parsed=urlsplit(self.path)
            if parsed.query=='versioning':return self.reply(200,b'<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Status>Enabled</Status></VersioningConfiguration>')
            if parsed.path not in objects:return self.reply(404,b'<Error><Code>NoSuchKey</Code></Error>')
            data,metadata=objects[parsed.path]
            headers={'x-amz-version-id':'v1'}
            if self.headers.get('Range'):
                start,end=map(int,self.headers['Range'][6:].split('-'))
                headers['Content-Range']=f'bytes {start}-{end}/{len(data)}'
                return self.reply(206,data[start:end+1],headers)
            self.reply(200,data,headers)
        def do_HEAD(self):
            if not self.signed():return
            path=urlsplit(self.path).path
            if path not in objects:return self.reply(404)
            data,metadata=objects[path]
            self.send_response(200)
            for k,v in {'Content-Length':len(data),'x-amz-version-id':'v1','x-amz-meta-sha256':metadata,'Connection':'close'}.items():self.send_header(k,str(v))
            self.end_headers()
        def do_PUT(self):
            if not self.signed():return
            path=urlsplit(self.path).path
            if path in objects:return self.reply(412,b'<Error><Code>PreconditionFailed</Code></Error>')
            data=self.rfile.read(int(self.headers['Content-Length']))
            checksum=base64.b64encode(hashlib.sha256(data).digest()).decode()
            if self.headers.get('x-amz-checksum-sha256')!=checksum:return self.reply(400,b'<Error><Code>BadDigest</Code></Error>')
            objects[path]=(data,self.headers['x-amz-meta-sha256'])
            self.reply(200,headers={'x-amz-version-id':'v1','ETag':'"fixture-etag"','x-amz-checksum-sha256':checksum})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(root/'ca.pem',root/'key.pem')
    server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('AWS_ACCESS_KEY_ID',ACCESS);monkeypatch.setenv('AWS_SECRET_ACCESS_KEY',SECRET)
    monkeypatch.delenv('AWS_SESSION_TOKEN',raising=False);monkeypatch.setenv('AWS_CA_BUNDLE',str(root/'ca.pem'))
    monkeypatch.setenv('AWS_EC2_METADATA_DISABLED','true')
    profile=store_profile();profile['endpoint']=f'https://127.0.0.1:{server.server_port}'
    write_json(root/'store.json',profile)
    try:yield root,profile,observed
    finally:server.shutdown();server.server_close();thread.join(timeout=5)


def test_actual_sdk_sigv4_tls_and_cli_roundtrip(service):
    root,profile,observed=service
    state=root/'state';state.mkdir();source=root/'input.bin';source.write_bytes(b'Signed HTTPS bytes')
    ref=root/'reference.json'
    put=subprocess.run([sys.executable,'-m','fsf.cli','remote','artifact-put',str(source),'--store',str(root/'store.json'),
        '--state',str(state),'--artifact-id','asset','--key','jobs/asset.bin','--role','data','--media-type','application/octet-stream','--out',str(ref)],capture_output=True,text=True,timeout=30)
    assert put.returncode==0,put.stdout
    get=subprocess.run([sys.executable,'-m','fsf.cli','remote','artifact-get',str(ref),'--store',str(root/'store.json'),
        '--state',str(state),'--out',str(root/'output.bin')],capture_output=True,text=True,timeout=30)
    assert get.returncode==0,get.stdout
    assert (root/'output.bin').read_bytes()==source.read_bytes()
    assert {'GET','PUT','HEAD'}<=set(observed)
    assert SECRET not in put.stdout+put.stderr+get.stdout+get.stderr


def test_installation_proxy_and_ca_are_real_and_scoped(service):
    root,profile,_=service
    connections=[]
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_CONNECT(self):
            expected='Basic '+base64.b64encode(b'fixture-user:fixture-password').decode()
            if self.headers.get('Proxy-Authorization')!=expected:
                self.send_response(407);self.end_headers();return
            host,port=self.path.rsplit(':',1)
            if host!='127.0.0.1' or int(port)!=urlsplit(profile['endpoint']).port:
                self.send_response(403);self.end_headers();return
            connections.append(True)
            remote=socket.create_connection((host,int(port)),timeout=5)
            self.send_response(200);self.end_headers()
            try:
                while True:
                    readable,_,_=select.select([self.connection,remote],[],[],5)
                    if not readable:return
                    for source in readable:
                        data=source.recv(65536)
                        if not data:return
                        (remote if source is self.connection else self.connection).sendall(data)
            finally:remote.close()
    proxy=ThreadingHTTPServer(('127.0.0.1',0),Proxy);proxy.daemon_threads=True
    thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start()
    from fsf.remote.environment import install_environment,installation_error
    from fsf.executors.subprocess_safety import run_safe_subprocess
    import os
    base={k:os.environ[k] for k in ('PATH','SystemRoot','WINDIR','TEMP','TMP') if k in os.environ}
    prefix={'FSF_REMOTE_INSTALL_HTTPS_PROXY':f'http://fixture-user:fixture-password@127.0.0.1:{proxy.server_port}',
            'FSF_REMOTE_INSTALL_CA':str(root/'ca.pem')}
    code='import requests,os; r=requests.get('+repr(profile['endpoint']+'/installation-smoke')+',proxies={"https":os.environ["HTTPS_PROXY"]},verify=os.environ["SSL_CERT_FILE"],timeout=5);r.raise_for_status();print(r.text)'
    try:
        env=install_environment(base,list(prefix),source=prefix)
        result=run_safe_subprocess([sys.executable,'-c',code],env=env,inherit_env=False,timeout_seconds=10,output_limit_bytes=16384)
        assert result.returncode==0 and 'installation-network-ok' in result.stdout and connections
        assert 'HTTPS_PROXY' not in base and 'fixture-password' not in result.stdout+result.stderr
        broken=dict(env);broken['HTTPS_PROXY']=broken['HTTPS_PROXY'].replace('fixture-password','wrong-password')
        failure=run_safe_subprocess([sys.executable,'-c',code],env=broken,inherit_env=False,timeout_seconds=10,output_limit_bytes=16384)
        assert failure.returncode!=0 and installation_error(failure.stderr)=='REMOTE_INSTALL_PROXY_FAILED'
    finally:proxy.shutdown();proxy.server_close();thread.join(timeout=5)


def _cli_race(command,barrier,queue):
    barrier.wait(timeout=30)
    completed=subprocess.run(command,capture_output=True,text=True,timeout=60)
    queue.put({'returncode':completed.returncode,'stdout':completed.stdout})


@pytest.mark.parametrize('parallel',[False,True])
def test_actual_local_cli_v2_submission_and_independent_worker_fetch(service,remote_wheel,parallel):
    root,config,observed=service
    for name in ('state','worker','prepared','transfer','budget'):(root/name).mkdir()
    store=ArtifactStore(config)
    recipe=template('cpu.recipe.json');recipe.update(schema_version='fsf_remote_recipe_v2',execution_requirements={
        'render':{'engine':'CYCLES','device':'cpu','backend':'none','device_count':0,'min_vram_mb':0,'samples':4,'seed':0,'threads':2},
        'encode':{'profile':'rc8_control_x264_v1','device':'cpu','encoder':'libx264','ffmpeg_version':'8.0.1','ffmpeg_sha256':'a'*64,'ffmpeg_build_digest':'b'*64,'libx264_build':'core 165 r3222 b35605a'}})
    recipe['environment']['python']=f'3.{sys.version_info.minor}';recipe=seal(recipe,'recipe_digest')
    profile=template('local.profile.json');profile.update(schema_version='fsf_remote_profile_v2',authorized=True,approved_recipes=[recipe['recipe_digest']],
        settings={'python':sys.executable,'worker_root':str(root/'worker')},gpu_limits={'allowed':False,'backends':[],'max_devices':0,'max_vram_mb':0},
        store_binding={'store_id':config['store_id'],'config_digest':store_public_identity(config),'input_prefix':'jobs/inputs/','output_prefix':'jobs/outputs/'})
    profile.update(group_limits={'max_budget_seconds':3600,'max_total_attempts':16,'max_attempts_per_segment':3,'max_frames':96},
        allocation={'allocation_id':'test_allocation','state_root':str(root/'budget'),'max_submissions':16,'max_execution_seconds':10000})
    wheel=store.put(remote_wheel,artifact_id='worker_wheel',key='jobs/inputs/'+remote_wheel.name,role='worker_wheel',media_type='application/zip',state_root=root/'transfer')
    definition={'parent':{'job_id':'parent','run_id':'run','step_id':'producer','attempt_id':1,'plan_digest':'a'*64,'inputs_digest':'b'*64,'expected_contract_digest':None,'requirements_digest':None},
        'parameters':{'value':9},'inputs':{},'video_condition':None,'consistency_requirements':None,'expected_media_contract':None}
    segment={'group_id':'group','segment_id':'segment','generation_digest':'c'*64,'start_frame':0,'end_frame':24,'clock':{'num':24,'den':1}}
    for name,value in [('definition',definition),('recipe',recipe),('profile',profile),('inputs',{}),('wheel',wheel),('segment',segment)]:write_json(root/(name+'.json'),value)
    prepare=subprocess.run([sys.executable,'-m','fsf.cli','remote','prepare-v2',str(root/'definition.json'),
        *[part for name in ('recipe','profile','inputs','wheel','segment') for part in ('--'+name,str(root/(name+'.json')))],
        '--store',str(root/'store.json'),'--out',str(root/'prepared')],capture_output=True,text=True,timeout=30)
    assert prepare.returncode==0,prepare.stdout
    key=json.loads(prepare.stdout)['result']['request_digest']
    command=[sys.executable,'-m','fsf.cli','remote','dispatch-v2',str(root/'prepared'/(key+'.json')),'--profile',str(root/'profile.json'),
             '--store',str(root/'store.json'),'--state',str(root/'state'),'--wait']
    if parallel:
        context=multiprocessing.get_context('spawn');barrier=context.Barrier(3);queue=context.Queue()
        workers=[context.Process(target=_cli_race,args=(command,barrier,queue)) for _ in range(2)]
        for worker in workers:worker.start()
        barrier.wait(timeout=30)
        results=[queue.get(timeout=75) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            if worker.is_alive():worker.terminate();worker.join(timeout=5)
            assert worker.exitcode==0
        assert all(r['returncode']==0 for r in results),results
        assert all(json.loads(r['stdout'])['result']['status']=='verified' for r in results)
        assert all(json.loads(r['stdout'])['result']['submitted_count']==1 for r in results)
    else:
        completed=subprocess.run(command,capture_output=True,text=True,timeout=60)
        assert completed.returncode==0,completed.stdout
        assert json.loads(completed.stdout)['result']['status']=='verified'
    assert json.loads((root/'state'/key/'accepted/outputs/result.json').read_text())['square']==81
    repeated=subprocess.run(command,capture_output=True,text=True,timeout=30)
    assert repeated.returncode==0 and json.loads(repeated.stdout)['result']['submitted_count']==1
