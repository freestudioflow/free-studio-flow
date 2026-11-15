"""Bound story coverage, chapter continuity and exact feature clocks."""
from fractions import Fraction

from fsf.production.contracts import ProductionError, digest, key, seal
from fsf.production.timeline import interval

CLOCKS = ({'num': 24, 'den': 1}, {'num': 24000, 'den': 1001})
FRAMES = 172800


def require(condition, code='FEATURE_PLAN_INVALID'):
    if not condition:
        raise ProductionError(code)


def validate_feature(value, plan):
    require(isinstance(value, dict) and set(value) == {
        'schema_version', 'clock', 'frame_count', 'initial_state', 'chapters', 'shots', 'feature_digest'
    } and value['schema_version'] == 'fsf_feature_plan_v1' and seal(value, 'feature_digest') == value)
    clock = value['clock']
    require(clock in CLOCKS and all(type(n) is int for n in clock.values()))
    require(type(value['frame_count']) is int and value['frame_count'] == FRAMES)
    shots = value['shots']
    tasks = {task['task_id']: task for task in plan['tasks']}
    require(isinstance(shots, list) and len(shots) == len(tasks) and bool(shots), 'FEATURE_COVERAGE')
    seen = set()
    beat_keys = set()
    semantic_keys = set()
    state = value['initial_state']
    require(isinstance(state, dict) and bool(state))
    end = 0
    shot_index = {}
    for shot in shots:
        require(isinstance(shot, dict) and set(shot) == {
            'step_id', 'chapter', 'range', 'beat_id', 'purpose', 'location', 'asset_ids',
            'dialogue', 'state_before', 'state_after'
        })
        step, beat = key(shot['step_id']), key(shot['beat_id'])
        require(step in tasks and step not in seen and beat not in beat_keys, 'FEATURE_COVERAGE')
        start, stop = interval(shot['range'], FRAMES)
        task = tasks[step]
        require(start == end and task['generated_frames'] == stop - start
                and set(task['depends_on']) <= seen, 'FEATURE_COVERAGE')
        if seen:
            require(shots[len(seen)-1]['step_id'] in task['depends_on'], 'FEATURE_STATE_UNBOUND')
        require(shot['state_before'] == state and isinstance(shot['state_after'], dict)
                and state.keys() == shot['state_after'].keys(), 'FEATURE_STATE_UNBOUND')
        require(isinstance(shot['asset_ids'], list) and bool(shot['asset_ids'])
                and len(set(shot['asset_ids'])) == len(shot['asset_ids']))
        for asset in shot['asset_ids']:
            key(asset)
        key(shot['location'])
        purpose = shot['purpose']
        require(isinstance(purpose, str) and bool(purpose.strip()) and len(purpose) <= 4096)
        identity = (purpose.strip().casefold(), shot['location'],
                    digest(shot['state_before']), digest(shot['state_after']))
        require(identity not in semantic_keys, 'FEATURE_TEMPLATE_REPEAT')
        dialogue = shot['dialogue']
        require(isinstance(dialogue, dict) and dialogue.get('mode') in {'speech', 'no_dialogue'})
        if dialogue['mode'] == 'speech':
            require(set(dialogue) == {'mode', 'lines'} and isinstance(dialogue['lines'], list)
                    and bool(dialogue['lines']), 'FEATURE_DIALOGUE_UNBOUND')
            position = start
            for line in dialogue['lines']:
                line_start, line_end = interval(line['range'], FRAMES)
                require(set(line) == {'speaker', 'text', 'range'} and key(line['speaker'])
                        and isinstance(line['text'], str) and line['text'].strip()
                        and position <= line_start < line_end <= stop, 'FEATURE_DIALOGUE_UNBOUND')
                position = line_end
        else:
            require(set(dialogue) == {'mode', 'reason'} and isinstance(dialogue['reason'], str)
                    and bool(dialogue['reason'].strip()), 'FEATURE_DIALOGUE_UNBOUND')
        state, end = shot['state_after'], stop
        seen.add(step)
        beat_keys.add(beat)
        semantic_keys.add(identity)
        shot_index[step] = shot
    require(end == FRAMES and seen == tasks.keys(), 'FEATURE_COVERAGE')
    chapter_ids = set()
    shot_position = chapter_end = 0
    for chapter in value['chapters']:
        require(set(chapter) == {'chapter_id', 'range', 'steps'})
        identifier = key(chapter['chapter_id'])
        start, stop = interval(chapter['range'], FRAMES)
        require(identifier not in chapter_ids and start == chapter_end and bool(chapter['steps']), 'FEATURE_COVERAGE')
        cursor = start
        for step in chapter['steps']:
            require(shot_position < len(shots) and shots[shot_position]['step_id'] == step, 'FEATURE_COVERAGE')
            bound = shot_index[step]
            require(bound['chapter'] == identifier and bound['range'][0] == cursor, 'FEATURE_COVERAGE')
            cursor = bound['range'][1]
            shot_position += 1
        require(cursor == stop, 'FEATURE_COVERAGE')
        chapter_end = stop
        chapter_ids.add(identifier)
    require(chapter_end == FRAMES and shot_position == len(shots), 'FEATURE_COVERAGE')
    duration = Fraction(FRAMES * clock['den'], clock['num'])
    return {'feature_digest': value['feature_digest'], 'chapters': len(chapter_ids), 'shots': len(shots),
            'clock': clock, 'frames': FRAMES, 'seconds': [duration.numerator, duration.denominator],
            'scope': 'structural_plan'}
