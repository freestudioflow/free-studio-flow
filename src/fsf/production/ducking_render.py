"""Streaming explicit BGM derivation. Import its new SHA using normal audio routing."""
from array import array
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import wave

from fsf.core.asset_library import _root, _open_reader
from fsf.core.edit_store import edit_lock, read_json
from fsf.production.contracts import ProductionError, digest, integer, publish, seal
from fsf.production.control import file_identity
from fsf.production.speech import validate_ducking_envelope


def render_ducking_wave(source, envelope, *, output_root, seconds=300):
    """Apply integer-sample linear-dB ramps without rewriting user gain or master.

    This creates a separate, immutable WAV and lineage receipt. It does not
    silently attach itself to an edit or turn ASR coverage into human review.
    """
    validate_ducking_envelope(envelope)
    integer(seconds,1,5400)
    deadline = time.monotonic()+seconds
    identity = file_identity(source)
    root = _root(output_root)
    binding = {'source_sha256':identity['sha256'],'envelope_digest':envelope['envelope_digest'],
               'algorithm':'linear_db_integer_samples_pcm16_v1'}
    target = root/digest(binding)
    with edit_lock(root/('.ducking-'+target.name)):
        if target.exists():
            _root(target)
            receipt = read_json(target/'receipt.json')
            if (seal(receipt,'receipt_digest')!=receipt or any(receipt.get(name)!=value for name,value in binding.items())
                    or file_identity(target/'audio.wav')['sha256']!=receipt.get('output_sha256')):
                raise ProductionError('PRODUCTION_DUCKING_SOURCE_CHANGED')
            return {**receipt,'reused':True,'bundle':target.name}
        with tempfile.TemporaryDirectory(prefix='.ducking-',dir=root) as temporary:
            scratch = Path(temporary)
            output = scratch/'audio.wav'
            with _open_reader(Path(source).absolute()) as incoming, wave.open(incoming,'rb') as sound:
                if sound.getframerate()!=48000 or sound.getsampwidth()!=2 or sound.getnchannels() not in (1,2) or sound.getcomptype()!='NONE':
                    raise ProductionError('PRODUCTION_DUCKING_FORMAT')
                total, channels = sound.getnframes(),sound.getnchannels()
                if total!=envelope['timeline_samples']:
                    raise ProductionError('PRODUCTION_DUCKING_TIMING')
                pieces = envelope['pieces']
                piece_index = 0
                position = 0
                with output.open('w+b') as stream:
                    with wave.open(stream,'wb') as destination:
                        destination.setparams(sound.getparams())
                        while raw := sound.readframes(8192):
                            if time.monotonic()>=deadline:
                                raise ProductionError('PRODUCTION_DUCKING_DEADLINE')
                            if len(raw)%(channels*2):
                                raise ProductionError('PRODUCTION_DUCKING_FORMAT')
                            samples = array('h')
                            samples.frombytes(raw)
                            if sys.byteorder!='little':samples.byteswap()
                            for offset in range(0,len(samples),channels):
                                while position>=pieces[piece_index]['end_sample']:
                                    piece_index+=1
                                piece=pieces[piece_index]
                                start,end=piece['start_sample'],piece['end_sample']
                                first,last=piece['start_gain_millidb'],piece['end_gain_millidb']
                                gain0,gain1=first['num']/first['den'],last['num']/last['den']
                                gain=gain0+(gain1-gain0)*(position-start)/(end-start)
                                multiplier=10**(gain/20000)
                                for channel in range(channels):
                                    samples[offset+channel]=max(-32768,min(32767,round(samples[offset+channel]*multiplier)))
                                position+=1
                            if sys.byteorder!='little':samples.byteswap()
                            destination.writeframesraw(samples.tobytes())
                        if position!=total:
                            raise ProductionError('PRODUCTION_DUCKING_FORMAT')
                    stream.flush()
                    os.fsync(stream.fileno())
            if file_identity(source)!=identity:
                raise ProductionError('PRODUCTION_DUCKING_SOURCE_CHANGED')
            receipt=seal({'schema_version':'fsf_ducking_render_receipt_v1',**binding,
                          'source_size_bytes':identity['size_bytes'],'output_sha256':file_identity(output)['sha256'],
                          'coverage_digest':envelope['coverage_digest'],'sample_rate':48000,
                          'sample_count':total,'channels':channels,'user_gain_unchanged':True,
                          'listening_review':'not_reviewed','numeric_quality':'not_evaluated'},'receipt_digest')
            publish(scratch/'receipt.json',receipt)
            publish(scratch/'envelope.json',envelope)
            os.rename(scratch,target)
            scratch.mkdir()
            return {**receipt,'reused':False,'bundle':target.name}
