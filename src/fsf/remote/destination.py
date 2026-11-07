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
    if policy['intent'] == 'create_private' and (not slug or settings.get('activation_mode', 'api_push') != 'api_push'):
        raise RemoteError('REMOTE_DESTINATION_CONFLICT')
    return {'slug': slug, **policy}


def require_secret_capability(profile):
    # No proven SDK binding interface is configured. Do not manufacture metadata.
    if resolve(profile)['secret_aliases']:
        raise RemoteError('REMOTE_SECRET_CAPABILITY_UNAVAILABLE')


def new_slot_proof(proof, *, owner, slug):
    expected = {'owner': owner, 'slug': slug, 'state': 'absent', 'can_create_private': True}
    if proof != expected:
        raise RemoteError('REMOTE_DESTINATION_UNVERIFIED')
    return expected


def authorize_create(profile, *, owner, proof=None):
    destination = resolve(profile)
    if destination['intent'] != 'create_private':
        return None
    # A transport must supply a documented positive capability. The current
    # Kaggle SDK gateway has none; HTTP 403/404 is deliberately not a proof.
    return new_slot_proof(proof, owner=owner, slug=destination['slug'])
