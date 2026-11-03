"""Actual PCM attenuation and source retention; no ASR/listening qualification."""
from array import array
import math
import wave

import pytest

from fsf.production.contracts import ProductionError
from fsf.production.ducking_render import render_ducking_wave
from fsf.production.speech import build_ducking_envelope
from test_production_speech import report


def source(tmp_path, samples=144000):
    path=tmp_path/'bgm.wav'
    values=array('h',(round(10000*math.sin(2*math.pi*440*index/48000)) for index in range(samples)))
    with wave.open(str(path),'wb') as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(48000)
        sound.writeframes(values.tobytes())
    return path,values


def envelope():
    coverage=report([{'event_id':'voice','track_id':'VO','clip_id':'C1','start_sample':48000,'end_sample':96000}],144000)
    return build_ducking_envelope(coverage,target_track_id='BGM',attenuation_millidb=12000,attack_samples=240,release_samples=240)


def test_render_reduces_dialogue_region_and_preserves_rest_and_original(tmp_path):
    path,before=source(tmp_path)
    original=path.read_bytes()
    output=tmp_path/'render'
    output.mkdir()
    result=render_ducking_wave(path,envelope(),output_root=output)
    with wave.open(str(output/result['bundle']/'audio.wav'),'rb') as sound:
        assert sound.getnframes()==len(before)
        after=array('h')
        after.frombytes(sound.readframes(sound.getnframes()))
    def rms(values):return math.sqrt(sum(value*value for value in values)/len(values))
    delta=20*math.log10(rms(after[50000:90000])/rms(before[50000:90000]))
    assert abs(delta+12)<0.01
    assert after[:47000]==before[:47000]
    assert after[97000:]==before[97000:]
    assert path.read_bytes()==original
    assert result['user_gain_unchanged'] is True
    assert result['listening_review']=='not_reviewed'
    assert render_ducking_wave(path,envelope(),output_root=output)['reused'] is True
    (output/result['bundle']/'audio.wav').write_bytes(b'changed')
    with pytest.raises(ProductionError,match='SOURCE_CHANGED'):
        render_ducking_wave(path,envelope(),output_root=output)


def test_short_bgm_is_not_looped_or_padded_silently(tmp_path):
    path,_=source(tmp_path,48000)
    output=tmp_path/'render'
    output.mkdir()
    with pytest.raises(ProductionError,match='TIMING'):
        render_ducking_wave(path,envelope(),output_root=output)
    assert not list(output.glob('*/audio.wav'))
