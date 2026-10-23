"""Parent and worker verification of each scheduled Blender result."""
from pathlib import Path
import tempfile

from fsf.core.edit_store import read_json
from fsf.remote.bundle import unpack
from fsf.remote.contracts import RemoteError, seal, sha, digest, safe_path
from fsf.remote.gpu_schedule import validate_schedule
from fsf.runtime.blender import verify_bundle


def verify_gpu_batch(archive, request, plan, scratch_root):
    validate_schedule(plan)
    requirements = request['recipe']['execution_requirements']
    if digest(plan) != requirements.get('schedule_digest') or plan['authorized_devices'] != requirements['render']['device_count']:
        raise RemoteError('REMOTE_GPU_SCHEDULE_IDENTITY')
    with tempfile.TemporaryDirectory(dir=scratch_root) as temp:
        root = Path(temp)
        names = unpack(archive, root)
        state = read_json(root / 'schedule-receipt.json')
        if (seal(state, 'receipt_digest')['receipt_digest'] != state.get('receipt_digest') or
                state['plan_digest'] != digest(plan) or state['status'] != 'completed' or
                state['authorized_devices'] != plan['authorized_devices'] or
                set(state['tasks']) != {t['task_id'] for t in plan['tasks']}):
            raise RemoteError('REMOTE_GPU_SCHEDULE_IDENTITY')
        selected = state['selected_devices']
        uuids = {d['uuid'] for d in selected}
        if len(selected) != plan['authorized_devices'] or len(uuids) != len(selected):
            raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
        expected = {'schedule-receipt.json'}
        frames = []
        intervals = []
        for task in plan['tasks']:
            entry = state['tasks'][task['task_id']]
            if entry['status'] != 'accepted' or not 1 <= entry['attempt'] <= task['max_attempts']:
                raise RemoteError('REMOTE_GPU_SCHEDULE_IDENTITY')
            devices = entry['devices']
            if len(devices) != task['device_count'] or len({d['uuid'] for d in devices}) != len(devices) or not {d['uuid'] for d in devices} <= uuids:
                raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
            if entry['finished'] < entry['started']:
                raise RemoteError('REMOTE_GPU_SCHEDULE_IDENTITY')
            intervals.append({**entry,'vram_mb_per_device':task.get('vram_mb_per_device',0)})
            declared = {task['task_id']+'/'+str(entry['attempt'])+'/'+p for p in task['outputs']}
            if {o['path'] for o in entry['outputs']} != declared:
                raise RemoteError('REMOTE_OUTPUTS')
            for item in entry['outputs']:
                path = safe_path(root, item['path'])
                if sha(path) != item['sha256'] or path.stat().st_size != item['size_bytes']:
                    raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
                expected.add(item['path'])
                with tempfile.TemporaryDirectory(dir=scratch_root) as inner:
                    unpack(path, inner)
                    manifest = verify_bundle(inner)
                    render = {**requirements['render'], 'device_count': task['device_count']}
                    evidence = manifest['execution_evidence']
                    selection = evidence['selection']
                    selection = selection if isinstance(selection, list) else [selection]
                    if (manifest['schema_version'] != 'fsf_blender_manifest_v2' or manifest['render_profile'] != render or
                            manifest['encode_profile'] != requirements['encode'] or manifest['clock'] != request['segment']['clock'] or
                            evidence['device'] != 'GPU' or selection != devices):
                        raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
                    frames.extend(f['scene_frame'] for f in manifest['frame_mapping'])
        if names != expected or sorted(frames) != list(range(request['segment']['start_frame'], request['segment']['end_frame'])):
            raise RemoteError('REMOTE_FRAME_SEQUENCE')
        sharing=plan.get('sharing')
        for device in selected:
            uuid=device['uuid'];events=[]
            for entry in intervals:
                if uuid in {d['uuid'] for d in entry['devices']}:
                    events.extend([(entry['started'],1,entry['vram_mb_per_device']),
                                   (entry['finished'],-1,-entry['vram_mb_per_device'])])
            count=reserved=0
            for _,delta,memory in sorted(events):
                count+=delta;reserved+=memory
                if count>(sharing['max_tasks_per_device'] if sharing else 1) or (sharing and reserved>sharing['memory_budget_mb']):
                    raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
            if sharing:
                observed=state.get('memory_samples',{}).get(uuid)
                if (sharing['memory_budget_mb']+sharing['headroom_mb']>device['memory_mb'] or not observed or
                        observed['samples']<1 or not 0<=observed['sampled_peak_mb']<=sharing['memory_budget_mb']):
                    raise RemoteError('REMOTE_GPU_MEMORY_LIMIT')
        return {'verified_tasks': len(intervals), 'authorized_devices': plan['authorized_devices'],
                'frame_count': len(frames), 'motion_review': 'not_reviewed',
                'sharing':sharing,'memory_samples':state.get('memory_samples')}
