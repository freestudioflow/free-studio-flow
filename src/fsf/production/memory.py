"""Global story/world state. These constraints do not prove rendered pixels.

Spatial routes and declared events survive chunk/chapter boundaries. Model
adapters must consume the resulting requirements; visual evidence stays separate.
"""
import copy
import json
import math
import re
from fractions import Fraction

from fsf.core.edit_store import decode_json
from fsf.production.contracts import ProductionError,digest,seal,key,integer


def _bad():raise ProductionError('PRODUCTION_MEMORY_INVALID')


def _hex(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):_bad()


def _unique(items,field):
    if not isinstance(items,list):_bad()
    result={}
    for item in items:
        if not isinstance(item,dict) or field not in item:_bad()
        identifier=key(item[field])
        if identifier in result:_bad()
        result[identifier]=item
    return result


def _replay(world,through):
    facts=copy.deepcopy(world['facts'])
    known={e['entity_id'] for e in world['entities']}
    _attachments(facts,known)
    for event in world['events']:
        if event['frame']>through:break
        for change in event['changes']:
            fact=facts[change['fact_id']]
            if not fact['mutable'] or digest(fact['value'])!=change['before_digest']:
                raise ProductionError('PRODUCTION_MEMORY_STATE_CONFLICT')
            fact['value']=copy.deepcopy(change['value'])
        _attachments(facts,known)
    return facts


def _attachments(facts,known):
    links={}
    for fact in facts.values():
        if fact['attribute'] in {'owner','attachment'}:
            if fact['value'] is not None and fact['value'] not in known:_bad()
            if fact['attribute']=='attachment' and fact['value'] is not None:links[fact['entity_id']]=fact['value']
        if fact['attribute']=='visible' and type(fact['value']) is not bool:_bad()
        if fact['attribute'] in {'attachment_offset','path_offset'}:
            offset=fact['value']
            if not isinstance(offset,list) or len(offset)!=3 or any(type(x) not in (int,float) or not math.isfinite(x) for x in offset):_bad()
            if fact['attribute']=='path_offset' and fact['mutable']:_bad()
    for entity in links:
        seen=set();node=entity
        while node in links:
            if node in seen:raise ProductionError('PRODUCTION_MEMORY_ATTACHMENT_CYCLE')
            seen.add(node);node=links[node]
    return links


def _validate_world(value):
    fields={'schema_version','production_id','clock','frame_count','snapshot','anchors','edges','routes',
            'entities','facts','events','motions','chapters','world_digest'}
    if not isinstance(value,dict) or set(value)!=fields or value['schema_version']!='fsf_production_world_v1':_bad()
    try:decode_json(json.dumps(value,allow_nan=False).encode())
    except (ValueError,TypeError,RecursionError):_bad()
    if seal(value,'world_digest')!=value:_bad()
    key(value['production_id']);integer(value['frame_count'],1,172800)
    if not isinstance(value['clock'],dict) or set(value['clock'])!={'num','den'} or any(type(n) is not int for n in value['clock'].values()) or value['clock'] not in ({'num':24,'den':1},{'num':24000,'den':1001}):_bad()
    snap=value['snapshot']
    if not isinstance(snap,dict) or set(snap)!={'schema_version','assets','snapshot_digest'} or snap['schema_version']!='fsf_production_assets_v1' or seal(snap,'snapshot_digest')!=snap:_bad()
    assets=_unique(snap['assets'],'asset_id')
    for asset in assets.values():
        if set(asset)!={'asset_id','version','content_hash','record_digest'}:_bad()
        key(asset['version']);_hex(asset['content_hash']);_hex(asset['record_digest'])
    anchors=_unique(value['anchors'],'anchor_id');routes=_unique(value['routes'],'route_id')
    entities=_unique(value['entities'],'entity_id');_unique(value['events'],'event_id');_unique(value['motions'],'motion_id')
    if not anchors or not entities:_bad()
    for anchor in anchors.values():
        if set(anchor)!={'anchor_id','space_id','position'}:_bad()
        key(anchor['space_id'])
        if not isinstance(anchor['position'],list) or len(anchor['position'])!=3:_bad()
        try:
            if any(type(n) not in (int,float) or not math.isfinite(n) for n in anchor['position']):_bad()
        except OverflowError:_bad()
    edges=set()
    if not isinstance(value['edges'],list):_bad()
    for pair in value['edges']:
        if not isinstance(pair,list) or len(pair)!=2 or pair[0] not in anchors or pair[1] not in anchors or pair[0]==pair[1]:_bad()
        edge=tuple(sorted(pair))
        if edge in edges:_bad()
        edges.add(edge)
    for route in routes.values():
        if set(route)!={'route_id','anchor_ids'} or not isinstance(route['anchor_ids'],list) or len(route['anchor_ids'])<2:_bad()
        ids=route['anchor_ids']
        if any(a not in anchors for a in ids) or any(tuple(sorted((a,b))) not in edges for a,b in zip(ids,ids[1:])):_bad()
    for entity in entities.values():
        if set(entity)!={'entity_id','asset_id','asset_version','asset_digest','initial_anchor','identity_digest'}:_bad()
        asset=assets.get(entity['asset_id'])
        if not asset or entity['asset_version']!=asset['version'] or entity['asset_digest']!=asset['content_hash'] or entity['initial_anchor'] not in anchors:_bad()
        _hex(entity['identity_digest'])
    if not isinstance(value['facts'],dict) or len(value['facts'])>10000:_bad()
    fact_keys=set()
    for identifier,fact in value['facts'].items():
        key(identifier)
        if not isinstance(fact,dict) or set(fact)!={'entity_id','attribute','mutable','value'} or fact['entity_id'] not in entities or type(fact['mutable']) is not bool:_bad()
        key(fact['attribute'])
        pair=(fact['entity_id'],fact['attribute'])
        if pair in fact_keys:_bad()
        fact_keys.add(pair)
        if fact['attribute'] in {'position','anchor'}:_bad()
        if fact['attribute']=='owner' and fact['value'] is not None and fact['value'] not in entities:_bad()
    previous=-1
    for event in value['events']:
        if set(event)!={'event_id','frame','actor_ids','changes'}:_bad()
        integer(event['frame'],0,value['frame_count']-1)
        if event['frame']<previous or not isinstance(event['actor_ids'],list) or not event['actor_ids'] or len(event['actor_ids'])!=len(set(event['actor_ids'])) or not set(event['actor_ids'])<=set(entities):_bad()
        previous=event['frame']
        if not isinstance(event['changes'],list) or not event['changes']:_bad()
        changed=set()
        for change in event['changes']:
            if not isinstance(change,dict) or set(change)!={'fact_id','before_digest','value'} or change['fact_id'] not in value['facts'] or change['fact_id'] in changed:_bad()
            changed.add(change['fact_id']);_hex(change['before_digest'])
            fact=value['facts'][change['fact_id']]
            if fact['attribute']=='owner' and change['value'] is not None and change['value'] not in entities:_bad()
    _replay(value,value['frame_count']-1)
    for entity in entities.values():
        position=entity['initial_anchor'];end=0
        motions=sorted((m for m in value['motions'] if m['entity_id']==entity['entity_id']),key=lambda m:m['start_frame'])
        for motion in motions:
            if set(motion)!={'motion_id','entity_id','route_id','direction','start_frame','end_frame'} or motion['route_id'] not in routes or motion['direction'] not in {'forward','reverse'}:_bad()
            integer(motion['start_frame'],0,value['frame_count']-1);integer(motion['end_frame'],1,value['frame_count'])
            path=routes[motion['route_id']]['anchor_ids'][::1 if motion['direction']=='forward' else -1]
            if motion['start_frame']<end or motion['end_frame']-motion['start_frame']<len(path) or path[0]!=position:
                raise ProductionError('PRODUCTION_MEMORY_SPATIAL_CONFLICT')
            position=path[-1];end=motion['end_frame']
    if any(m['entity_id'] not in entities for m in value['motions']):_bad()
    _unique(value['chapters'],'chapter_id');end=0
    for chapter in value['chapters']:
        if set(chapter)!={'chapter_id','start_frame','end_frame'} or chapter['start_frame']!=end:_bad()
        integer(chapter['start_frame'],0,value['frame_count']-1);integer(chapter['end_frame'],chapter['start_frame']+1,value['frame_count'])
        end=chapter['end_frame']
    if end!=value['frame_count']:_bad()
    return copy.deepcopy(value)


def validate_world(value):
    try:return _validate_world(value)
    except ProductionError:raise
    except (ValueError,TypeError,KeyError,IndexError,OverflowError,RecursionError):_bad()


def create_world(definition):
    if not isinstance(definition,dict) or 'world_digest' in definition:_bad()
    return validate_world(seal(definition,'world_digest'))


def state_at(world,frame):
    world=validate_world(world);integer(frame,0,world['frame_count']-1)
    anchors={a['anchor_id']:a for a in world['anchors']};routes={r['route_id']:r['anchor_ids'] for r in world['routes']}
    positions={e['entity_id']:list(anchors[e['initial_anchor']]['position']) for e in world['entities']}
    for m in sorted(world['motions'],key=lambda m:m['start_frame']):
        if m['start_frame']>frame:continue
        path=routes[m['route_id']][::1 if m['direction']=='forward' else -1]
        if frame>=m['end_frame']-1:positions[m['entity_id']]=list(anchors[path[-1]]['position']);continue
        progress=Fraction(frame-m['start_frame'],m['end_frame']-m['start_frame']-1)*(len(path)-1)
        edge=min(int(progress),len(path)-2);alpha=progress-edge
        a,b=anchors[path[edge]]['position'],anchors[path[edge+1]]['position']
        positions[m['entity_id']]=[float(Fraction(str(x))*(1-alpha)+Fraction(str(y))*alpha) for x,y in zip(a,b)]
    facts=_replay(world,frame);links=_attachments(facts,set(positions))
    for fact in facts.values():
        if fact['attribute']=='path_offset':
            entity=fact['entity_id']
            try:positions[entity]=[float(Fraction(str(x))+Fraction(str(y))) for x,y in zip(positions[entity],fact['value'])]
            except OverflowError:_bad()
    offsets={f['entity_id']:f['value'] for f in facts.values() if f['attribute']=='attachment_offset'}
    done=set()
    def attach(entity):
        if entity in done:return positions[entity]
        if entity in links:
            if any(m['entity_id']==entity and m['start_frame']<=frame<m['end_frame'] for m in world['motions']):
                raise ProductionError('PRODUCTION_MEMORY_SPATIAL_CONFLICT')
            parent=attach(links[entity]);offset=offsets.get(entity,[0,0,0])
            try:positions[entity]=[float(Fraction(str(x))+Fraction(str(y))) for x,y in zip(parent,offset)]
            except OverflowError:_bad()
        done.add(entity);return positions[entity]
    for entity in positions:attach(entity)
    return seal({'schema_version':'fsf_production_world_state_v1','world_digest':world['world_digest'],'frame':frame,
                 'facts':facts,'positions':positions},'state_digest')


def interval_requirements(world,start_frame,end_frame):
    world=validate_world(world);integer(start_frame,0,world['frame_count']-1);integer(end_frame,start_frame+1,world['frame_count'])
    return seal({'schema_version':'fsf_world_requirements_v1','world_digest':world['world_digest'],
        'snapshot_digest':world['snapshot']['snapshot_digest'],'interval':[start_frame,end_frame],
        'clock':world['clock'],'start_state':state_at(world,start_frame),'end_state':state_at(world,end_frame-1),
        'entities':world['entities'],'anchors':world['anchors'],'routes':world['routes'],
        'events':[e for e in world['events'] if start_frame<=e['frame']<end_frame],
        'motions':[m for m in world['motions'] if m['start_frame']<end_frame and m['end_frame']>start_frame],
        'chapters':[c['chapter_id'] for c in world['chapters'] if c['start_frame']<end_frame and c['end_frame']>start_frame],
        'visual_conformance':'not_evaluated','human_review':'not_reviewed'},'requirements_digest')


def _validate_world_ack(requirements,ack,*,allow_gaps_for_probe=False):
    """Confirms the backend received conditions, never that its pixels obeyed."""
    if type(allow_gaps_for_probe) is not bool:_bad()
    if not isinstance(requirements,dict) or requirements.get('schema_version')!='fsf_world_requirements_v1' or seal(requirements,'requirements_digest')!=requirements:_bad()
    fields={'requirements_digest','world_digest','snapshot_digest','interval','entity_ids','route_ids','capability_gaps'}
    if not isinstance(ack,dict) or set(ack)!=fields:_bad()
    for name in ('requirements_digest','world_digest','snapshot_digest','interval'):
        if ack[name]!=requirements[name]:raise ProductionError('PRODUCTION_MEMORY_ACK_MISMATCH')
    for name,source,identifier in (('entity_ids','entities','entity_id'),('route_ids','routes','route_id')):
        if not isinstance(ack[name],list) or any(not isinstance(x,str) for x in ack[name]) or len(ack[name])!=len(set(ack[name])) or set(ack[name])!={x[identifier] for x in requirements[source]}:
            raise ProductionError('PRODUCTION_MEMORY_ACK_MISMATCH')
    gaps=ack['capability_gaps']
    if not isinstance(gaps,list) or any(not isinstance(g,str) or not g or len(g)>256 for g in gaps):_bad()
    if gaps and not allow_gaps_for_probe:raise ProductionError('PRODUCTION_MEMORY_CAPABILITY_GAP')
    return {'conditions_acknowledged':True,'required_conditions_available':not gaps,
            'visual_conformance':'not_evaluated','human_review':'not_reviewed'}


def validate_world_ack(requirements,ack,*,allow_gaps_for_probe=False):
    try:
        return _validate_world_ack(requirements,ack,allow_gaps_for_probe=allow_gaps_for_probe)
    except ProductionError:
        raise
    except (KeyError,TypeError,ValueError,IndexError,RecursionError):
        raise ProductionError('PRODUCTION_MEMORY_INVALID') from None
