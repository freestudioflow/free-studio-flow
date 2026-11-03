"""Adapter contract fixtures, explicitly not actual speech recognition evidence."""
import hashlib
import json
from pathlib import Path
import sys
import wave

import pytest

from fsf.production.contracts import ProductionError, seal
from fsf.production.speech import build_expected_dialogue
from fsf.production.speech_command import recognize_with_command


def setup(tmp_path):
    sound = tmp_path/'fixture.wav'
    with wave.open(str(sound),'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b'\0\0'*48000)
    expected = build_expected_dialogue(clock={'num':24,'den':1},timeline_frames=24,events=[{
        'event_id':'line','track_id':'voice','clip_id':'clip','start_frame':0,'end_frame':24,
        'text':'These expected words must never be given to the recognition backend.'}])
    script = tmp_path/'recognizer.py'
    script.write_text('''import json,sys
from pathlib import Path
source,target=map(Path,sys.argv[1:]);call=json.loads(source.read_text());request=call['request']
assert all('text' not in event for event in request['events'])
Path('observed-request.json').write_text(json.dumps(request))
event=request['events'][0]
result={'status':'completed','events':[dict(event,text='Adapter fixture output') ]}
target.write_text(json.dumps({'schema_version':'fsf_command_adapter_reply_v1','request_digest':request['request_digest'],'operation':'recognize','result':result}))
''')
    config = seal({'schema_version':'fsf_asr_command_v1','authorized':True,'model_lock_digest':'a'*64,
        'settings':{'command':[sys.executable,str(script)],'cwd':str(tmp_path),'timeout_seconds':10,
                    'credential_env_names':[],'files':[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                    for path in (Path(sys.executable),script)]}},'recognizer_digest')
    work=tmp_path/'work'
    work.mkdir()
    return sound,expected,config,work


def test_expected_words_not_leaked_to_recognizer_or_copied_to_transcript(tmp_path):
    sound,expected,config,work=setup(tmp_path)
    transcript=recognize_with_command(sound,expected,config=config,work_root=work)
    assert transcript['events'][0]['text']=='Adapter fixture output'
    assert transcript['sample_count']==48000
    assert transcript['artifact_sha256']==hashlib.sha256(sound.read_bytes()).hexdigest()
    observed=(tmp_path/'observed-request.json').read_text()
    assert expected['events'][0]['text'] not in observed
    assert 'initial_prompt' not in observed


def test_unauthorized_recognizer_never_calls_command(tmp_path):
    sound,expected,config,work=setup(tmp_path)
    config['authorized']=False
    config=seal(config,'recognizer_digest')
    with pytest.raises(ProductionError,match='NOT_AUTHORIZED'):
        recognize_with_command(sound,expected,config=config,work_root=work)
    assert not (tmp_path/'observed-request.json').exists()


def test_word_crossing_window_start_uses_observed_overlap():
    from fsf.tools.faster_whisper_worker import assign_words
    windows = [{'event_id':'line','track_id':'voice','clip_id':'clip',
                'start_sample':308000,'end_sample':500000}]
    words = [{'word':' We','start_sample':293760,'end_sample':318720}]
    result = assign_words(windows, words)
    assert result == [{'event_id':'line','track_id':'voice','clip_id':'clip',
                       'text':'We','start_sample':293760,'end_sample':318720}]


def test_ambiguous_and_gap_speech_are_retained_without_false_identity():
    from fsf.tools.faster_whisper_worker import assign_words
    windows = [{'event_id':name,'track_id':'voice','clip_id':'clip',
                'start_sample':start,'end_sample':end}
               for name,start,end in [('unexpected_speech_0',0,10),('second',10,20)]]
    words = [{'word':' ambiguous','start_sample':5,'end_sample':15},
             {'word':' outside','start_sample':30,'end_sample':40}]
    result = assign_words(windows, words)
    assert result == [{'event_id':'unexpected_speech_1','track_id':'unassigned',
                       'clip_id':'unassigned','text':'ambiguous outside',
                       'start_sample':5,'end_sample':40}]
