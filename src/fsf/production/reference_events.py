"""Local evidence extractors for reference analysis."""
import array
import hashlib
import json
from pathlib import Path
import sys
import wave

from PIL import Image, ImageChops, ImageDraw, ImageFont

from fsf.production.contracts import ProductionError
from fsf.production.control import file_identity


def _runs(values):
    start, previous = 0, None
    for index, value in enumerate(values):
        if index and value != previous:
            if previous is not None:
                yield start, index, previous
            start = index
        previous = value
    if values and previous is not None:
        yield start, len(values), previous


def _read_bitmap_text(image):
    """Exact bitmap matching for the Pillow default uppercase/digit font."""
    mask = image.convert("L").point(lambda value: 255 if value >= 160 else 0)
    box = mask.getbbox()
    if box is None:
        return None
    font = ImageFont.load_default()
    alphabet = []
    for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        bitmap = font.getmask(char)
        left, top, _, _ = font.getbbox(char)
        points = {(left+x,top+y) for y in range(bitmap.size[1]) for x in range(bitmap.size[0])
                  if bitmap[y*bitmap.size[0]+x]>=160}
        alphabet.append((char, int(font.getlength(char)), points))
    target={(x,y) for y in range(box[1],box[3]) for x in range(box[0],box[2]) if mask.getpixel((x,y))}
    matches=set()
    budget=4096
    def search(x,y,text,remaining):
        nonlocal budget
        budget-=1
        if budget<0 or len(matches)>1:
            return
        if not remaining:
            check=Image.new("L",mask.size)
            ImageDraw.Draw(check).text((x-int(font.getlength(text)),y),text,font=font,fill=255)
            if check.point(lambda value:255 if value>=160 else 0).tobytes()==mask.tobytes():
                matches.add(text)
            return
        if x>box[2] or len(text)>=24:
            return
        for char,advance,points in alphabet:
            placed={(px+x,py+y) for px,py in points}
            if placed<=target and placed & remaining:
                search(x+advance,y,text+char,remaining-placed)
    for _,_,points in alphabet:
        # Anchor the first glyph to the leftmost observed column.
        start=box[0]-min(x for x,_ in points)
        for y in range(box[1]-10,box[1]+1):
            search(start,y,"",target)
    return next(iter(matches)) if len(matches)==1 and budget>=0 else None


def layers(path, width, height, frame_count):
    """Find rectangular inserts against a uniform static corner background."""
    identity = []
    inserts = {}
    with Path(path).open("rb") as stream:
        for _ in range(frame_count):
            image=Image.frombytes("RGB",(width,height),stream.read(width*height*3))
            corners = [image.getpixel(point) for point in ((0,0),(width-1,0),(0,height-1),(width-1,height-1))]
            if len(set(corners))!=1:
                identity.append(None)
                continue
            bbox=ImageChops.difference(image,Image.new("RGB",image.size,corners[0])).getbbox()
            if not bbox or (bbox[2]-bbox[0])*(bbox[3]-bbox[1])>.8*width*height:
                identity.append(None)
                continue
            crop=image.crop(bbox)
            if any(crop.getpixel(p)!=crop.getpixel((0,0)) for p in
                   ((crop.width-1,0),(0,crop.height-1),(crop.width-1,crop.height-1))):
                identity.append(None)
                continue
            key = (bbox, hashlib.sha256(crop.tobytes()).digest())
            if key not in inserts:
                inserts[key] = _read_bitmap_text(crop)
            identity.append(key)
    events=[]
    for start,end,key in _runs(identity):
        if end-start<2:
            continue
        bbox,_ = key
        text=inserts[key]
        events.append({"range":[start,end],"bbox":list(bbox),"kind":"observation",
                       "type":"rectangular_insert","text":text,
                       "text_status":"observed" if text else "not_available"})
    return events


def audio_events(path, identity=None, *, maximum_samples=4800000):
    evidence = identity if identity is not None else file_identity(path)
    events = []
    with wave.open(str(path),"rb") as stream:
        rate, count = stream.getframerate(), stream.getnframes()
        if stream.getnchannels()!=1 or stream.getsampwidth()!=2 or count>maximum_samples:
            raise ProductionError("REFERENCE_AUDIO_UNSUPPORTED")
        offset=0
        start=None
        for _ in range((count+239)//240):
            pcm=array.array("h",stream.readframes(240))
            if sys.byteorder!="little":
                pcm.byteswap()
            if len(pcm)!=min(240,count-offset):
                raise ProductionError("REFERENCE_AUDIO_TRUNCATED")
            active=any(abs(value)>=512 for value in pcm)
            if active and start is None:
                start=offset
            elif not active and start is not None:
                events.append([start,offset])
                start=None
            offset+=len(pcm)
        if start is not None:
            events.append([start,offset])
    if file_identity(path)!=evidence:
        raise ProductionError("REFERENCE_SOURCE_CHANGED")
    return {"status":"observed","source":evidence,"sample_rate":rate,"sample_count":count,
            "events":[{"type":"audio_activity","kind":"observation","range_samples":event} for event in events],
            "music_origin":{"status":"unknown","reason":"origin_not_observable"}}


def classification(raw, source):
    value=json.loads(raw)
    if (not isinstance(value,dict) or set(value)!={"source_sha256","label","confidence"}
            or value["source_sha256"]!=source["sha256"]
            or value["label"] not in ("live_action","ai","3d","programmatic_animation","mixed","unknown")
            or (value["confidence"] is not None and
                (type(value["confidence"]) not in (int,float) or not 0<=value["confidence"]<=1))
            or (value["label"]=="unknown") != (value["confidence"] is None)):
        raise ProductionError("REFERENCE_ANALYZER_RESULT")
    return {"kind":"inference","status":"unknown" if value["label"]=="unknown" else "hypothesis",
            "label":value["label"],"confidence":value["confidence"],"evidence":source}


def transitions(raw, source, count):
    value=json.loads(raw)
    if (not isinstance(value,dict) or set(value)!={"source_sha256","cuts","fades"}
            or value["source_sha256"]!=source["sha256"]):
        raise ProductionError("REFERENCE_ANALYZER_RESULT")
    cuts,fades=value["cuts"],value["fades"]
    if (not isinstance(cuts,list) or any(type(n) is not int or not 0<n<count for n in cuts)
            or cuts!=sorted(set(cuts)) or not isinstance(fades,list)
            or any(not isinstance(span,list) or len(span)!=2
                   or any(type(n) is not int for n in span)
                   or not 0<=span[0]<span[1]<=count for span in fades)
            or any(a[1]>b[0] for a,b in zip(fades,fades[1:]))):
        raise ProductionError("REFERENCE_ANALYZER_RESULT")
    return cuts,fades
