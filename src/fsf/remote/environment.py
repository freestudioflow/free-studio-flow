"""Pinned tool preparation for approved recipes; not a code sandbox."""
import argparse
import copy
from importlib import resources
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import time
import zipfile

from jsonschema import Draft202012Validator
from fsf.core.asset_library import _no_links, _path_parts, _root, _open_reader
from fsf.core.edit_store import edit_lock, read_json
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.remote.contracts import RemoteError, digest, sha, seal, write_json, safe_path, reject_inline_credentials

INSTALL_NETWORK = {'FSF_REMOTE_INSTALL_HTTP_PROXY':'HTTP_PROXY',
                   'FSF_REMOTE_INSTALL_HTTPS_PROXY':'HTTPS_PROXY',
                   'FSF_REMOTE_INSTALL_NO_PROXY':'NO_PROXY',
                   'FSF_REMOTE_INSTALL_CA':'SSL_CERT_FILE'}


def install_environment(base, authorized_names, *, source=None):
    """Copy only explicitly authorized host installation settings, never evidence."""
    source = os.environ if source is None else source
    result = dict(base)
    for name, target in INSTALL_NETWORK.items():
        result.pop(name, None)
        if name in source:
            if name not in authorized_names:
                raise RemoteError('REMOTE_INSTALL_NETWORK_NOT_AUTHORIZED')
            value = source[name]
            if not value or '\n' in value or '\r' in value or '\0' in value:
                raise RemoteError('REMOTE_INSTALL_NETWORK_INVALID')
            result[target] = value
    return result


def installation_error(stderr):
    text=stderr.lower()
    if 'proxyerror' in text or 'tunnel connection failed' in text:return 'REMOTE_INSTALL_PROXY_FAILED'
    if 'certificate_verify_failed' in text or 'certificate verify failed' in text:return 'REMOTE_INSTALL_CA_FAILED'
    return 'REMOTE_INSTALL_FAILED'


def validate_environment(plan, expected_digest):
    plan = copy.deepcopy(plan)
    schema = json.loads(resources.files('fsf.schemas').joinpath('fsf_environment_plan_v1.schema.json').read_text('utf-8'))
    reject_inline_credentials(plan)
    if not Draft202012Validator(schema).is_valid(plan):
        raise RemoteError('REMOTE_ENVIRONMENT_SCHEMA')
    if seal(plan, 'environment_digest')['environment_digest'] != plan['environment_digest'] or plan['environment_digest'] != expected_digest:
        raise RemoteError('REMOTE_ENVIRONMENT_DIGEST')
    ids = set()
    for tool in plan['tools']:
        if tool['tool_id'] in ids:
            raise RemoteError('REMOTE_DUPLICATE_ID')
        ids.add(tool['tool_id'])
        _path_parts(tool['executable'])
        if tool['mode'] == 'archive':
            if not tool['archive_path'] or not tool['archive_sha256'] or tool['archive_size_bytes'] < 1:
                raise RemoteError('REMOTE_TOOL_ARCHIVE_REQUIRED')
            _path_parts(tool['archive_path'])
        elif tool['archive_path'] is not None or tool['archive_sha256'] is not None or tool['archive_size_bytes'] != 0 or '/' in tool['executable']:
            raise RemoteError('REMOTE_PREINSTALLED_DECLARATION')
        # A literal version is part of the output match, not an unanchored prefix.
        try:
            re.compile(tool['version_pattern'])
        except re.error:
            raise RemoteError('REMOTE_TOOL_VERSION_PATTERN') from None
    return plan


def _remaining(deadline, cancelled):
    if cancelled and cancelled():
        raise RemoteError('REMOTE_CANCELLED')
    value = deadline-time.monotonic()
    if value <= 0:
        raise RemoteError('REMOTE_ENVIRONMENT_TIMEOUT')
    return value


def _extract(source, destination, limit, deadline, cancelled):
    total, count, seen = 0, 0, set()
    def output(name, size, stream, mode):
        nonlocal total, count
        _remaining(deadline, cancelled)
        name = name.rstrip('/')
        parts = _path_parts(name)
        if name in seen or count >= 100000 or size < 0 or total+size > limit:
            raise RemoteError('REMOTE_TOOL_ARCHIVE_LIMIT')
        count += 1; seen.add(name); total += size
        target = destination.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        actual = 0
        with target.open('xb') as out:
            while block := stream.read(min(1024*1024, size-actual+1)):
                _remaining(deadline, cancelled)
                actual += len(block)
                if actual > size:
                    raise RemoteError('REMOTE_TOOL_ARCHIVE_LIMIT')
                out.write(block)
        if actual != size:
            raise RemoteError('REMOTE_TOOL_ARCHIVE_TRUNCATED')
        target.chmod(0o755 if mode & 0o111 else 0o644)
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                _remaining(deadline, cancelled)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or member.flag_bits & 1:
                    raise RemoteError('REMOTE_TOOL_ARCHIVE_LINK')
                _path_parts(member.filename.rstrip('/'))
                if member.is_dir(): continue
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise RemoteError('REMOTE_TOOL_ARCHIVE_LINK')
                with archive.open(member) as stream:
                    output(member.filename, member.file_size, stream, mode)
    else:
        with tarfile.open(source, 'r:*') as archive:
            for member in archive:
                _remaining(deadline, cancelled)
                _path_parts(member.name.rstrip('/'))
                if member.isdir(): continue
                if not member.isfile():
                    raise RemoteError('REMOTE_TOOL_ARCHIVE_LINK')
                with archive.extractfile(member) as stream:
                    output(member.name, member.size, stream, member.mode)


def _files(root):
    result = {}
    for path in root.rglob('*'):
        _no_links(path)
        if path.is_file(): result[path.relative_to(root).as_posix()] = sha(path)
    return result


def prepare_environment(plan, *, expected_digest, input_root, output_root, cancelled=None, remaining_seconds=None):
    plan = validate_environment(plan, expected_digest)
    source, target = _root(input_root), _root(output_root)
    budget = plan['deadline_seconds']
    if remaining_seconds is not None:
        if type(remaining_seconds) not in (int, float) or remaining_seconds <= 0:
            raise RemoteError('REMOTE_ENVIRONMENT_TIMEOUT')
        budget = min(budget, remaining_seconds)
    deadline = time.monotonic()+budget
    if not platform.python_version().startswith(plan['python']):
        raise RemoteError('REMOTE_PYTHON_VERSION')
    key = plan['environment_digest']
    final = target/key
    env = {k:os.environ[k] for k in ('PATH','SystemRoot','WINDIR','TEMP','TMP') if k in os.environ}
    def run(executable, args):
        res = run_safe_subprocess([str(executable), *args], cwd=source, env=env, inherit_env=False,
              timeout_seconds=_remaining(deadline,cancelled), cancel_check=cancelled, output_limit_bytes=16384)
        if res.timed_out: raise RemoteError('REMOTE_ENVIRONMENT_TIMEOUT')
        if res.returncode: raise RemoteError('REMOTE_TOOL_PROBE_FAILED')
        return res.stdout
    with edit_lock(target/'.tool-prepare-target', timeout=min(10, _remaining(deadline,cancelled))):
        _remaining(deadline,cancelled)
        prior = None
        if final.exists():
            _no_links(final)
            prior = read_json(final/'receipt.json')
            if seal(prior,'receipt_digest')['receipt_digest'] != prior.get('receipt_digest') or prior.get('environment_digest') != key:
                raise RemoteError('REMOTE_ENVIRONMENT_CHANGED')
            actual = _files(final/'tools')
            if actual != prior['installed_files']:
                raise RemoteError('REMOTE_ENVIRONMENT_CHANGED')
        scratch = Path(tempfile.mkdtemp(prefix='.environment-',dir=target))
        try:
            tools = scratch/'tools'; tools.mkdir()
            observed = []
            for tool in plan['tools']:
                _remaining(deadline,cancelled)
                if tool['os'] != platform.system().lower() or tool['architecture'].lower() != platform.machine().lower():
                    raise RemoteError('REMOTE_TOOL_PLATFORM')
                if tool['mode'] == 'archive':
                    archive = safe_path(source,tool['archive_path'])
                    with _open_reader(archive) as stream:
                        before = os.fstat(stream.fileno())
                        if before.st_size != tool['archive_size_bytes'] or sha(archive) != tool['archive_sha256']:
                            raise RemoteError('REMOTE_TOOL_INTEGRITY')
                    destination = tools/tool['tool_id']; destination.mkdir()
                    _extract(archive,destination,tool['expanded_limit_bytes'],deadline,cancelled)
                    if sha(archive) != tool['archive_sha256']:
                        raise RemoteError('REMOTE_TOOL_INTEGRITY')
                    executable = safe_path(destination,tool['executable'])
                else:
                    executable = shutil.which(tool['executable'])
                    if not executable: raise RemoteError('REMOTE_TOOL_MISSING')
                    executable = Path(executable)
                output = run(executable,tool['version_args'])
                if not re.search(tool['version_pattern'],output) or not re.search(r'(?<![\w.])'+re.escape(tool['version'])+r'(?![\w.])',output):
                    raise RemoteError('REMOTE_TOOL_VERSION')
                binary_sha = sha(executable)
                if tool['smoke_args']: run(executable,tool['smoke_args'])
                if sha(executable) != binary_sha: raise RemoteError('REMOTE_ENVIRONMENT_CHANGED')
                observed.append({'tool_id':tool['tool_id'],'version':tool['version'],'binary_sha256':binary_sha,
                                 'mode':tool['mode'],'os':tool['os'],'architecture':tool['architecture']})
            receipt = seal({'schema_version':'fsf_environment_receipt_v1','environment_digest':key,
                            'python':platform.python_version(),'tools':observed,'installed_files':_files(tools)},'receipt_digest')
            if prior:
                if prior != receipt: raise RemoteError('REMOTE_ENVIRONMENT_CHANGED')
            else:
                write_json(scratch/'receipt.json',receipt)
                os.replace(scratch,final)
            return receipt
        finally:
            if scratch.exists(): shutil.rmtree(scratch)


def recipe_diff(before, after):
    from fsf.remote.contracts import validate_recipe
    before, after = validate_recipe(before), validate_recipe(after)
    changes = []
    def walk(left,right,path):
        if type(left) is dict and type(right) is dict:
            for key in sorted(left.keys() | right.keys()):
                if key == 'recipe_digest': continue
                if key not in left or key not in right: changes.append(path+'/'+key)
                else: walk(left[key],right[key],path+'/'+key)
        elif left != right: changes.append(path)
    walk(before,after,'')
    return {'before_digest':before['recipe_digest'],'after_digest':after['recipe_digest'],
            'changed_paths':changes,'approval_required':before['recipe_digest']!=after['recipe_digest'],
            'automatic_approval':False}


def bind_environment(recipe, plan):
    """Produce a new, unapproved recipe; input package bytes are pinned by plan."""
    from fsf.remote.contracts import validate_recipe
    if recipe.get('schema_version')=='fsf_remote_recipe_v2':
        from fsf.remote.v2 import validate_recipe as validate_v2
        recipe=validate_v2(recipe)
        base={k:v for k,v in recipe.items() if k!='execution_requirements'}
        base['schema_version']='fsf_remote_recipe_v1'
        result=bind_environment(seal(base,'recipe_digest'),plan)
        result.update(schema_version='fsf_remote_recipe_v2',execution_requirements=recipe['execution_requirements'])
        return validate_v2(seal(result,'recipe_digest'))
    recipe = validate_recipe(recipe)
    plan = validate_environment(plan, plan.get('environment_digest'))
    result = copy.deepcopy(recipe)
    occupied = {item['artifact_id'] for item in recipe['inputs']}
    if 'environment_plan' in occupied:
        raise RemoteError('REMOTE_DUPLICATE_ID')
    result['inputs'].append({'artifact_id':'environment_plan','role':'data','media_type':'application/json'})
    replacements = {}
    for tool in plan['tools']:
        if tool['mode'] == 'archive':
            suffix = Path(tool['archive_path']).suffix
            if tool['tool_id'] in occupied or not re.fullmatch(r'\.[a-z0-9]{1,8}',suffix) or tool['archive_path'] != 'inputs/'+tool['tool_id']+suffix:
                raise RemoteError('REMOTE_TOOL_INPUT_MAPPING')
            occupied.add(tool['tool_id'])
            result['inputs'].append({'artifact_id':tool['tool_id'],'role':'data','media_type':'application/octet-stream'})
            replacements['{'+tool['tool_id']+'}'] = '.prepared-tools/'+plan['environment_digest']+'/tools/'+tool['tool_id']+'/'+tool['executable']
        else:
            if tool['tool_id'] != 'python':
                replacements['{'+tool['tool_id']+'}'] = tool['executable']
    env = result['environment']
    env['python'] = plan['python']
    if plan['tools']:
        systems = {item['os'] for item in plan['tools']}
        if len(systems) != 1: raise RemoteError('REMOTE_TOOL_PLATFORM')
        env['os'] = next(iter(systems))
    env['setup'].insert(0,{'source':'fsf:bundled-tools','args':['{python}','-m','fsf.remote.environment',
        '--plan','{input.environment_plan}','--digest',plan['environment_digest'],'--inputs','.','--out','.prepared-tools']})
    # Parent creates this controlled directory through worker preparation below.
    for probe in env['probes']:
        probe['program'] = replacements.get(probe['program'],probe['program'])
    for step in result['steps']:
        step['args'] = [replacements.get(arg,arg) for arg in step['args']]
        if step['tool'] == 'fsf.recipe.blender' and '{blender}' in replacements:
            step['args'].extend(['--executable',replacements['{blender}']])
        if step['tool'] == 'fsf.recipe.blender' and all('{'+name+'}' in replacements for name in ('ffmpeg','ffprobe')):
            for name in ('ffmpeg','ffprobe'):step['args'].extend(['--'+name,replacements['{'+name+'}']])
    return validate_recipe(seal(result,'recipe_digest'))


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--plan',required=True)
    parser.add_argument('--digest',required=True)
    parser.add_argument('--inputs',required=True)
    parser.add_argument('--out',required=True)
    try:
        args = parser.parse_args(argv)
        target = Path(args.out).absolute()
        target.mkdir(parents=True,exist_ok=True)
        _no_links(target)
        result = prepare_environment(read_json(args.plan),expected_digest=args.digest,input_root=args.inputs,output_root=target)
        print(json.dumps({'ok':True,'environment_digest':result['environment_digest'],'receipt_digest':result['receipt_digest']}))
        return 0
    except KeyboardInterrupt:
        code = 'REMOTE_CANCELLED'
    except RemoteError as exc:
        code = exc.code
    except Exception:
        code = 'REMOTE_ENVIRONMENT_FAILED'
    print(json.dumps({'ok':False,'error':{'code':code}}))
    return 130 if code == 'REMOTE_CANCELLED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
