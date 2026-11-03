"""Optional offline ASR adapter example; install its dependencies in a separate recipe.

No model is downloaded, no expected words are used, and no human review is made.
The caller pins a local CTranslate2 model and this worker in its authorized config.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import time
import wave


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def unique_pairs(items):
    result = {}
    for name,value in items:
        if name in result:
            raise ValueError('duplicate key')
        result[name] = value
    return result


def read(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(8*1024**2+1)
    if len(raw)>8*1024**2:
        raise ValueError('input limit')
    return json.loads(raw,object_pairs_hook=unique_pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('finite numbers required')))


def file_hash(path, deadline):
    total, result = 0, hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024*1024):
            total += len(block)
            if total>2*1024**3 or time.monotonic()>=deadline:
                raise ValueError('resource limit')
            result.update(block)
    return result.hexdigest()


def assign_words(windows, words):
    """Window identity is operator metadata, not inferred speaker diarization."""
    import heapq
    ordered=sorted(windows,key=lambda item:(item['start_sample'],item['end_sample'],item['event_id']))
    active, ends, grouped = {},[],{}
    declared_ids={item['event_id'] for item in windows}
    suffix=0
    unexpected='unexpected_speech_0'
    while unexpected in declared_ids:
        suffix+=1
        unexpected='unexpected_speech_'+str(suffix)
    pointer=0
    for word in sorted(words,key=lambda item:(item['start_sample'],item['end_sample'])):
        left,right=word['start_sample'],max(word['start_sample']+1,word['end_sample'])
        while pointer<len(ordered) and ordered[pointer]['start_sample']<right:
            event=ordered[pointer]
            active[pointer]=event
            heapq.heappush(ends,(event['end_sample'],pointer))
            pointer+=1
        while ends and ends[0][0]<=left:
            _,index=heapq.heappop(ends)
            active.pop(index,None)
        best,score,tied=None,0,False
        for candidate in active.values():
            overlap=max(0,min(right,candidate['end_sample'])-max(left,candidate['start_sample']))
            if overlap>score:best,score,tied=candidate,overlap,False
            elif overlap and overlap==score:tied=True
            if score==right-left and tied:break
        event=best if best is not None and not tied else {
            'event_id':unexpected,'track_id':'unassigned','clip_id':'unassigned'}
        group=grouped.setdefault(event['event_id'],{name:event[name] for name in ('event_id','track_id','clip_id')} | {
            'text':'','start_sample':word['start_sample'],'end_sample':word['end_sample']})
        group['text']+=word['word']
        group['start_sample']=min(group['start_sample'],word['start_sample'])
        group['end_sample']=max(group['end_sample'],word['end_sample'])
    return [{**event,'text':event['text'].strip()} for event in grouped.values()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model',required=True)
    parser.add_argument('--lock',required=True)
    parser.add_argument('--runtime-lock',required=True)
    parser.add_argument('--seconds',type=int,default=300)
    parser.add_argument('--max-window-seconds',type=int,default=60)
    parser.add_argument('--threads',type=int,default=2)
    parser.add_argument('--language',default='en')
    parser.add_argument('source')
    parser.add_argument('target')
    args = parser.parse_args()
    if not 1<=args.seconds<=5400 or args.max_window_seconds<1 or args.threads<1:
        raise ValueError('invalid limits')
    deadline = time.monotonic()+args.seconds
    call = read(args.source)
    if call['operation'] != 'recognize':
        raise ValueError('unsupported operation')
    request = call['request']
    if set(request) != {'audio_sha256','sample_rate','events','model_lock_digest','recognizer_digest','request_digest'}:
        raise ValueError('unexpected recognizer input')
    audio = Path(call['locator']['audio'])
    if file_hash(audio,deadline) != request['audio_sha256'] or request['sample_rate'] != 48000:
        raise ValueError('source changed')
    lock = read(args.lock)
    if (set(lock) != {'model_id','revision','files','lock_digest'}
            or digest({name:value for name,value in lock.items() if name!='lock_digest'}) != lock['lock_digest']
            or lock['lock_digest'] != request['model_lock_digest']):
        raise ValueError('model binding')
    model_root = Path(args.model).resolve()
    if not lock['files'] or len(lock['files'])>128:
        raise ValueError('model file limit')
    for name,sha in lock['files'].items():
        file = model_root/name
        if not file.resolve().is_relative_to(model_root) or file.is_symlink() or file_hash(file,deadline) != sha:
            raise ValueError('model changed')
    from importlib.metadata import version
    runtime=read(args.runtime_lock)
    for package in runtime:
        if version(package['name'])!=package['version']:
            raise ValueError('runtime version changed')
    from faster_whisper import WhisperModel
    model = WhisperModel(str(model_root),device='cpu',compute_type='int8',cpu_threads=args.threads,num_workers=1,local_files_only=True)
    observed, all_words = [], []
    with wave.open(str(audio),'rb') as sound:
        if sound.getframerate()!=48000 or sound.getnchannels() not in (1,2) or sound.getsampwidth()!=2:
            raise ValueError('PCM format')
        parameters = sound.getparams()
        for event in request['events']:
            if set(event) != {'event_id','track_id','clip_id','start_sample','end_sample'}:
                raise ValueError('no expected text is allowed')
            start,end = event['start_sample'],event['end_sample']
            if type(start) is not int or type(end) is not int or not 0<=start<end<=sound.getnframes():
                raise ValueError('window limit')
        # Recognize the whole timeline, including gaps and padding. Declared
        # event windows are used only AFTER recognition to assign identities.
        for start in range(0,sound.getnframes(),args.max_window_seconds*48000):
            end=min(sound.getnframes(),start+args.max_window_seconds*48000)
            if time.monotonic()>=deadline:
                raise ValueError('deadline')
            sound.setpos(start)
            buffer = io.BytesIO()
            with wave.open(buffer,'wb') as sliced:
                sliced.setparams(parameters)
                sliced.writeframes(sound.readframes(end-start))
            buffer.seek(0)
            segments, information = model.transcribe(buffer,language=args.language,beam_size=5,
                word_timestamps=True,condition_on_previous_text=False,vad_filter=False)
            words, texts = [], []
            for segment in segments:
                if time.monotonic()>=deadline:
                    raise ValueError('deadline')
                texts.append(segment.text)
                native=[{'word':word.word,'start_sample':start+int(word.start*48000+0.5),
                         'end_sample':start+int(word.end*48000+0.5),'probability':float(word.probability)} for word in segment.words or []]
                if segment.text.strip() and not native:
                    native=[{'word':segment.text,'start_sample':start+int(segment.start*48000+0.5),
                             'end_sample':start+int(segment.end*48000+0.5),'probability':None}]
                words.extend(native)
            all_words.extend(words)
            if len(all_words)>200000:
                raise ValueError('word limit')
            observed.append({'start_sample':start,'end_sample':end,'words':words,'text':''.join(texts),
                             'language':information.language})
    events=assign_words(request['events'],all_words)
    if file_hash(audio,deadline) != request['audio_sha256']:
        raise ValueError('source changed')
    raw = {'audio_sha256':request['audio_sha256'],'model_lock_digest':lock['lock_digest'],
           'faster_whisper_version':version('faster-whisper'),'ctranslate2_version':version('ctranslate2'),
           'runtime_lock_sha256':file_hash(args.runtime_lock,deadline),
           'chunks':observed,'expected_text_used':False,'whole_timeline_observed':True,
           'identity_mode':'operator_interval_assignment_not_diarization','listening_review':'not_reviewed'}
    Path(args.target).with_name('asr-observed.json').write_text(json.dumps(raw,allow_nan=False),encoding='utf-8')
    reply = {'schema_version':'fsf_command_adapter_reply_v1','request_digest':request['request_digest'],
             'operation':'recognize','result':{'status':'completed','events':events}}
    Path(args.target).write_text(json.dumps(reply,allow_nan=False),encoding='utf-8')


if __name__=='__main__':
    main()
