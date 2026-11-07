"""Additive typed locks and deterministic optional inputs for recipe v4."""
import copy
import re
import json

from fsf.remote.contracts import RemoteError, digest, seal, safe_path, write_json


def is_v4(recipe):
    return recipe.get('schema_version') == 'fsf_remote_recipe_v4'


def project_recipe(recipe, present=None):
    """Only a validation/compiler projection; never reseal an operator document."""
    base = copy.deepcopy(recipe)
    base.pop('execution_requirements', None)
    base.pop('input_mode', None)
    base['schema_version'] = 'fsf_remote_recipe_v1'
    if is_v4(recipe):
        base['inputs'] = [{k: v for k, v in item.items() if k not in {'required', 'consumer'}}
                          for item in base['inputs'] if present is None or item['artifact_id'] in present]
    return seal(base, 'recipe_digest')


def validate_extensions(recipe):
    validate_limits(recipe)
    model = recipe['execution_requirements']['model']
    if digest(model['lock']) != model['lock_digest']:
        raise RemoteError('REMOTE_MODEL_INTEGRITY')
    lock = model['lock']
    if lock['source']['immutable_revision'].lower() in {'latest', 'main', 'master'}:
        raise RemoteError('REMOTE_UNPINNED_MODEL')
    ids = [c['id'] for c in lock['components']]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', i) for i in ids):
        raise RemoteError('REMOTE_DUPLICATE_ID')
    # Qualification is an evidence reference, never an assertion of authority.
    steps = {s['step_id'] for s in recipe['steps']}
    from fsf.core.asset_library import _path_parts
    _path_parts(model['receipt_path'])
    for item in recipe['inputs']:
        consumer = item['consumer']
        if not set(consumer['step_ids']) <= steps:
            raise RemoteError('REMOTE_INPUTS')
        if any('{env.' in arg for arg in consumer.get('args', [])):
            raise RemoteError('REMOTE_SECRET_ARGUMENT')
        if not item['required'] and any('{input.' + item['artifact_id'] + '}' in a
                                       for step in recipe['steps'] for a in step['args']):
            raise RemoteError('REMOTE_INPUTS')


def validate_limits(value, depth=0):
    if depth>64:
        raise RemoteError('REMOTE_CONTRACT_LIMIT')
    if depth==0 and len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8'))>8*1024*1024:
        raise RemoteError('REMOTE_CONTRACT_LIMIT')
    if isinstance(value,str) and len(value.encode('utf-8'))>4096:
        raise RemoteError('REMOTE_CONTRACT_LIMIT')
    if isinstance(value,dict):
        for key,item in value.items():
            validate_limits(key,depth+1);validate_limits(item,depth+1)
    elif isinstance(value,list):
        for item in value:validate_limits(item,depth+1)


def validate_presence(recipe, present):
    specs = {i['artifact_id']: i for i in recipe['inputs']}
    required = {i for i, s in specs.items() if s.get('required', True)}
    if not required <= set(present) <= set(specs):
        raise RemoteError('REMOTE_INPUTS')


def write_bindings(recipe, request, directory):
    present = {i['artifact_id']: i for i in request['inputs']}
    validate_presence(recipe, present)
    value = {'schema_version': 'rc11_inputs_v1', 'mode': recipe['input_mode'], 'bindings': []}
    for spec in recipe['inputs']:
        item = present.get(spec['artifact_id'])
        binding = {'id': spec['artifact_id'], 'role': spec['role'], 'media_type': spec['media_type'],
                   'required': spec['required'], 'consumer': spec['consumer'],
                   'presence': 'present' if item else 'absent'}
        if item:
            binding.update(artifact_id=item['artifact_id'], path=item['path'],
                           sha256=item['reference']['sha256'], size_bytes=item['reference']['size_bytes'])
        value['bindings'].append(binding)
    write_json(directory / 'input-bindings.json', value)
    return value


def step_arguments(recipe, step, request):
    present = {i['artifact_id'] for i in request['inputs']}
    args = list(step['args'])
    stdin_manifest = False
    for spec in recipe['inputs']:
        consumer = spec['consumer']
        if step['step_id'] not in consumer['step_ids']:
            continue
        if consumer['kind'] == 'argv_group' and spec['artifact_id'] in present:
            args.extend(consumer['args'])
        if consumer['kind'] == 'stdin_manifest':
            stdin_manifest = True
    return args, stdin_manifest


def verify_capability(recipe, observed_digest):
    if observed_digest != recipe['execution_requirements']['model']['lock']['capability_digest']:
        raise RemoteError('REMOTE_CAPABILITY_CONFLICT')
