"""Explicit destination resolution and positive-evidence first-slot policy."""
from fsf.remote.contracts import RemoteError


def resolve(profile):
    settings = profile['settings']
    native, worker = settings.get('native_notebook'), settings.get('worker_notebook')
    if native and worker and native != worker:
        raise RemoteError('REMOTE_DESTINATION_CONFLICT')
    slug = native or worker
    policy = profile.get('destination', {'intent': 'existing', 'model_sources': [], 'secret_aliases': []})
    if profile.get('destination') and profile['transport'] != 'kaggle':
        if policy['intent'] != 'existing' or policy['model_sources'] or policy['secret_aliases']:
            raise RemoteError('REMOTE_DESTINATION_CONFLICT')
    if profile.get('destination') and profile['transport'] == 'kaggle' and not slug:
        raise RemoteError('REMOTE_DESTINATION_CONFLICT')
    if policy['intent'] == 'create_private' and (not slug or settings.get('activation_mode', 'api_push') != 'api_push'):
        raise RemoteError('REMOTE_DESTINATION_CONFLICT')
    return {'slug': slug, **policy}


def require_secret_capability(profile):
    # No proven SDK binding interface is configured. Do not manufacture metadata.
    if resolve(profile)['secret_aliases']:
        raise RemoteError('REMOTE_SECRET_CAPABILITY_UNAVAILABLE')


def secret_capabilities(profile):
    destination = resolve(profile)
    transport = profile['transport']
    mode = profile['settings'].get('activation_mode', 'api_push') if transport == 'kaggle' else 'worker'
    local = transport == 'local'
    return {'transport': transport, 'submission_mode': mode,
            'operator_environment': {'status': 'available' if local else 'unverified',
                                     'verification': 'recipe_and_policy' if local else 'worker_delivery_unverified'},
            'provider_attachment': {'status': 'unavailable', 'verification': 'unverified',
                                    'aliases': list(destination['secret_aliases'])},
            'model_sources': list(destination['model_sources'])}


def existing_slot_proof(metadata, *, ref, version=None):
    actual = getattr(metadata, 'current_version_number', None)
    if (getattr(metadata, 'ref', None) != ref or getattr(metadata, 'is_private', None) is not True
            or type(actual) is not int or actual < 1
            or (version is not None and actual != version)
            or type(getattr(metadata, 'id', None)) is not int or metadata.id < 1):
        raise RemoteError('REMOTE_DESTINATION_UNVERIFIED')
    return actual


def new_slot_proof(proof, *, owner, slug):
    expected = {'owner': owner, 'slug': slug, 'state': 'absent', 'can_create_private': True}
    if not isinstance(proof,dict) or proof.get('can_create_private') is not True or proof != expected:
        raise RemoteError('REMOTE_DESTINATION_UNVERIFIED')
    return expected


def authorize_create(profile, *, owner, proof=None):
    destination = resolve(profile)
    if destination['intent'] != 'create_private':
        return None
    # A transport must supply a documented positive capability. The current
    # Kaggle SDK gateway has none; HTTP 403/404 is deliberately not a proof.
    return new_slot_proof(proof, owner=owner, slug=destination['slug'])
