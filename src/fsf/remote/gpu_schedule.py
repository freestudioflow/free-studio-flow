"""Worker-side ready queue, adapted from the earlier dynamic schedule experiment.

Generate -> verify -> accept, bounded attempts and dependency gates are retained.
An idle device immediately takes another ready item; jobs never share devices.
Command completion is not proof of GPU rendering or human motion approval.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import os
from pathlib import Path
import time
import sys
import threading

from fsf.core.edit_store import edit_lock, read_json
from fsf.remote.contracts import RemoteError, digest, sha, write_json, safe_path, seal
from fsf.remote.gpu import cuda_inventory, select_devices, gpu_runtime_environment, memory_inventory
from fsf.executors.subprocess_safety import run_safe_subprocess


def validate_schedule(plan):
    required={'schema_version', 'authorized_devices', 'deadline_seconds', 'tasks'}
    if not required<=set(plan) or set(plan)-required-{'sharing'} or plan['schema_version'] != 'fsf_gpu_schedule_v1':
        raise RemoteError('REMOTE_GPU_SCHEDULE')
    n = plan['authorized_devices']
    if type(n) is not int or not 1 <= n <= 16 or type(plan['deadline_seconds']) is not int or not 1 <= plan['deadline_seconds'] <= 5400:
        raise RemoteError('REMOTE_GPU_NOT_AUTHORIZED')
    tasks = plan['tasks']
    sharing=plan.get('sharing')
    if sharing is not None:
        if not isinstance(sharing,dict) or set(sharing)!={'max_tasks_per_device','memory_budget_mb','headroom_mb'}:
            raise RemoteError('REMOTE_GPU_MEMORY_POLICY')
        if (type(sharing['max_tasks_per_device']) is not int or not 1<=sharing['max_tasks_per_device']<=8 or
                type(sharing['memory_budget_mb']) is not int or sharing['memory_budget_mb']<1 or
                type(sharing['headroom_mb']) is not int or sharing['headroom_mb']<256):
            raise RemoteError('REMOTE_GPU_MEMORY_POLICY')
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 256:
        raise RemoteError('REMOTE_GPU_SCHEDULE')
    ids = set()
    import re
    for task in tasks:
        fields={'task_id', 'deps', 'device_count', 'args', 'outputs', 'max_attempts'}
        if set(task) != fields | ({'vram_mb_per_device'} if sharing is not None else set()):
            raise RemoteError('REMOTE_GPU_SCHEDULE')
        if sharing is not None and (type(task['vram_mb_per_device']) is not int or not 1<=task['vram_mb_per_device']<=sharing['memory_budget_mb']):
            raise RemoteError('REMOTE_GPU_MEMORY_POLICY')
        key = task['task_id']
        if not isinstance(key, str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', key) or key in ids:
            raise RemoteError('REMOTE_GPU_SCHEDULE')
        ids.add(key)
        if type(task['device_count']) is not int or not 1 <= task['device_count'] <= n or type(task['max_attempts']) is not int or not 1 <= task['max_attempts'] <= 3:
            raise RemoteError('REMOTE_GPU_NOT_AUTHORIZED')
        if not isinstance(task['args'], list) or not task['args'] or any(not isinstance(a, str) or not a or '\x00' in a for a in task['args']):
            raise RemoteError('REMOTE_GPU_SCHEDULE')
        for field in ('deps', 'outputs'):
            values = task[field]
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values) or len(values) != len(set(values)):
                raise RemoteError('REMOTE_GPU_SCHEDULE')
        if not task['outputs']:
            raise RemoteError('REMOTE_OUTPUTS')
        for name in task['outputs']:
            if '\\' in name or Path(name).is_absolute() or any(p in ('', '.', '..') for p in name.split('/')) or ':' in name:
                raise RemoteError('REMOTE_OUTPUTS')
    pending = {t['task_id']: set(t['deps']) for t in tasks}
    if any(not deps <= ids or key in deps for key, deps in pending.items()):
        raise RemoteError('REMOTE_GPU_SCHEDULE')
    done = set()
    while pending:
        ready = {key for key, deps in pending.items() if deps <= done}
        if not ready:
            raise RemoteError('REMOTE_GPU_SCHEDULE')
        done |= ready
        for key in ready:
            del pending[key]
    return plan


def run_schedule(plan, *, output_root, visible=None, runner=run_safe_subprocess, cancelled=lambda: False,
                 paused=lambda: False, memory_probe=memory_inventory):
    """Run only on the already authorized worker; never submits a cloud job."""
    validate_schedule(plan)
    visible = cuda_inventory() if visible is None else visible
    selected = select_devices(visible, authorized=plan['authorized_devices'], requested=plan['authorized_devices'])
    if len({d['uuid'] for d in selected}) != len(selected) or len({d['index'] for d in selected}) != len(selected):
        raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
    sharing=plan.get('sharing')
    if sharing and any(sharing['memory_budget_mb']+sharing['headroom_mb']>d['memory_mb'] for d in selected):
        raise RemoteError('REMOTE_GPU_MEMORY_POLICY')
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    with edit_lock(root / '.schedule'):
        return _run(plan, root, selected, runner, cancelled, paused, memory_probe)


def _run(plan, root, selected, runner, cancelled, paused, memory_probe):
    path = root / 'schedule-receipt.json'
    identity = digest(plan)
    state = {'schema_version': 'fsf_gpu_schedule_receipt_v1', 'plan_digest': identity,
             'authorized_devices': plan['authorized_devices'], 'selected_devices': selected,
             'tasks': {}, 'status': 'running', 'motion_review': 'not_reviewed',
             'gpu_execution_verified': False}
    if path.exists():
        state = read_json(path)
        if seal(state, 'receipt_digest')['receipt_digest'] != state.get('receipt_digest') or state['plan_digest'] != identity or state['selected_devices'] != selected:
            raise RemoteError('REMOTE_GPU_SCHEDULE_IDENTITY')
        for key, entry in state['tasks'].items():
            if entry['status'] == 'running':
                raise RemoteError('REMOTE_EXECUTION_UNKNOWN')
            if entry['status'] == 'accepted':
                for item in entry['outputs']:
                    if sha(safe_path(root, item['path'])) != item['sha256']:
                        raise RemoteError('REMOTE_OUTPUT_CHANGED')
        state['status'] = 'running'
    deadline = time.monotonic() + plan['deadline_seconds']
    sharing=plan.get('sharing')
    slots=sharing['max_tasks_per_device'] if sharing else 1
    memory_stop=threading.Event()
    memory_error=None
    last_probe=0.0
    if sharing:state.setdefault('memory_samples',{})
    active = {}
    tasks = {t['task_id']: t for t in plan['tasks']}

    def save():
        write_json(path, seal(state, 'receipt_digest'))

    def execute(task, devices, attempt, work):
        env = {k: os.environ[k] for k in ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'HOME') if k in os.environ}
        env.update(gpu_runtime_environment())
        env.update(CUDA_VISIBLE_DEVICES=','.join(d['uuid'] for d in devices), FSF_REMOTE_DEPTH='1')
        start = time.monotonic()
        args = [a.replace('{python}', sys.executable).replace('{root}', str(root.resolve())).replace('{workspace}', str(root.resolve().parent)) for a in task['args']]
        result = runner(args, cwd=work, env=env, inherit_env=False,
                        timeout_seconds=max(0.01, deadline-start), cancel_check=lambda:cancelled() or memory_stop.is_set(),
                        output_limit_bytes=1024*1024)
        if result.timed_out or result.returncode:
            import re
            codes=re.findall(r'\b(?:BLENDER|REMOTE|AUDIO|PROCESS)_[A-Z_]{2,64}\b', getattr(result,'stderr',''))
            if codes:raise RemoteError(codes[-1])
            raise RemoteError('REMOTE_COMMAND_FAILED')
        outputs = []
        for name in task['outputs']:
            target = safe_path(work, name)
            outputs.append({'path': target.relative_to(root).as_posix(), 'sha256': sha(target), 'size_bytes': target.stat().st_size})
        return {'status': 'accepted', 'attempt': attempt, 'devices': devices, 'outputs': outputs,
                'started': start, 'finished': time.monotonic()}

    with ThreadPoolExecutor(max_workers=len(selected)*slots) as pool:
        while True:
            if sharing and time.monotonic()-last_probe>=0.25:
                last_probe=time.monotonic()
                try:
                    sample=memory_probe()
                    for device in selected:
                        key=device['uuid'];observed=sample[key]
                        prior=state['memory_samples'].setdefault(key,{'sampled_peak_mb':0,'max_compute_processes':0,'samples':0})
                        prior['sampled_peak_mb']=max(prior['sampled_peak_mb'],observed['used_mb'])
                        prior['max_compute_processes']=max(prior['max_compute_processes'],observed['compute_processes'])
                        prior['samples']+=1
                        if observed['used_mb']>sharing['memory_budget_mb']:
                            memory_error='REMOTE_GPU_MEMORY_LIMIT';memory_stop.set()
                except Exception:
                    memory_error='REMOTE_GPU_MEMORY_UNAVAILABLE';memory_stop.set()
            stop = cancelled() or paused() or memory_stop.is_set() or time.monotonic() >= deadline
            accepted = {k for k, v in state['tasks'].items() if v['status'] == 'accepted'}
            for key, task in tasks.items():
                prior = state['tasks'].get(key, {})
                if stop or key in active.values() or prior.get('status') in {'accepted', 'failed', 'blocked'}:
                    continue
                if any(state['tasks'].get(dep, {}).get('status') in {'failed', 'blocked'} for dep in task['deps']):
                    state['tasks'][key] = {'status': 'blocked', 'attempt': 0}
                    continue
                counts={d['uuid']:0 for d in selected};reserved={d['uuid']:0 for d in selected}
                for running in active.values():
                    for d in state['tasks'][running]['devices']:
                        counts[d['uuid']]+=1
                        reserved[d['uuid']]+=tasks[running].get('vram_mb_per_device',0)
                available=[d for d in selected if counts[d['uuid']]<slots and
                    (not sharing or reserved[d['uuid']]+task['vram_mb_per_device']<=sharing['memory_budget_mb'])]
                if not set(task['deps']) <= accepted or len(available) < task['device_count']:
                    continue
                devices=available[:task['device_count']]
                attempt = prior.get('attempt', 0) + 1
                work = root / key / str(attempt)
                work.mkdir(parents=True, exist_ok=False)
                state['tasks'][key] = {'status': 'running', 'attempt': attempt, 'devices': devices}
                save()  # Durable claim before starting the process.
                active[pool.submit(execute, task, devices, attempt, work)] = key
            save()
            if not active:
                break
            completed, _ = wait(active, timeout=0.05, return_when=FIRST_COMPLETED)
            for future in completed:
                key = active.pop(future)
                prior = state['tasks'][key]
                try:
                    state['tasks'][key] = future.result()
                except (Exception, KeyboardInterrupt) as exc:
                    state['tasks'][key] = {**prior, 'status': 'failed' if prior['attempt'] >= tasks[key]['max_attempts'] else 'redo',
                                           'error_code': exc.code if isinstance(exc,RemoteError) else 'REMOTE_COMMAND_FAILED'}
                save()
        if memory_error:state['error_code']=memory_error
        state['status'] = ('failed' if memory_error else 'paused' if cancelled() or paused() else 'failed' if time.monotonic() >= deadline or
                           any(v['status'] != 'accepted' for v in state['tasks'].values()) else 'completed')
        save()
    return read_json(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('plan')
    parser.add_argument('--out', required=True)
    parser.add_argument('--archive')
    args = parser.parse_args()
    result = run_schedule(read_json(args.plan), output_root=args.out,
                          paused=lambda:(Path(args.out)/'pause').exists(),
                          cancelled=lambda:(Path(args.out)/'cancel').exists())
    if args.archive and result['status'] == 'completed':
        from fsf.remote.bundle import pack
        root = Path(args.out)
        names = ['schedule-receipt.json'] + [o['path'] for entry in result['tasks'].values() for o in entry['outputs']]
        pack(root, names, args.archive)
    return 0 if result['status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
