"""Story/state feasibility tests, never neural visual-consistency certification."""
import copy
import pytest
from fsf.production.memory import create_world,validate_world,state_at,interval_requirements
from fsf.production.contracts import seal,digest,ProductionError


def world(kind='street',frames=4320):
    names={'street':['home','curb','road','stall'],
           'corridor':['outside','hall_a','hall_b','door','table'],
           'gallery':['entrance','art_a','art_b','art_c']}[kind]
    anchors=[{'anchor_id':n,'space_id':'fixed_space','position':[i*3,0,0]} for i,n in enumerate(names)]
    assets=[{'asset_id':a,'version':'1','record_digest':digest(['record',a]),'content_hash':digest(['bytes',a])} for a in ('hero','vendor','guest','prop','environment')]
    def entity(name,asset,anchor):
        a=next(a for a in assets if a['asset_id']==asset)
        return {'entity_id':name,'asset_id':asset,'asset_version':'1','asset_digest':a['content_hash'],
                'initial_anchor':anchor,'identity_digest':digest(['identity',name])}
    entities=[entity('hero','hero',names[0]),entity('vendor','vendor',names[-1]),entity('guest','guest',names[0]),entity('bag','prop',names[-1]),entity('fixed_environment','environment',names[0])]
    facts={
        'hero_clothes':{'entity_id':'hero','attribute':'costume','mutable':False,'value':'blue_jacket'},
        'bag_owner':{'entity_id':'bag','attribute':'owner','mutable':True,'value':'vendor'},
        'bag_attached':{'entity_id':'bag','attribute':'attachment','mutable':True,'value':None},
        'guest_visible':{'entity_id':'guest','attribute':'visible','mutable':True,'value':False},
        'hero_pose':{'entity_id':'hero','attribute':'posture','mutable':True,'value':'standing'}}
    motions=[{'motion_id':'outbound','entity_id':'hero','route_id':'same_route','direction':'forward','start_frame':0,'end_frame':1200},
             {'motion_id':'return','entity_id':'hero','route_id':'same_route','direction':'reverse','start_frame':2800,'end_frame':4320}]
    events=[]
    def event(name,frame,actors,changes):
        return {'event_id':name,'frame':frame,'actor_ids':actors,'changes':[
            {'fact_id':f,'before_digest':digest(before),'value':after} for f,before,after in changes]}
    if kind=='street':
        events=[event('purchase',1600,['hero','vendor'],[('bag_owner','vendor','hero'),('bag_attached',None,'hero')])]
    if kind=='corridor':
        for i in range(6):entities.append(entity('npc_'+str(i),'vendor',names[1+i%2]))
        events=[event('sit',1400,['hero'],[('hero_pose','standing','seated')]),
                event('leave',2700,['hero'],[('hero_pose','seated','standing')])]
    if kind=='gallery':
        facts['guest_formation']={'entity_id':'guest','attribute':'path_offset','mutable':False,'value':[0,0.75,0]}
        for name in ('art_a','art_b','art_c'):
            asset_id='painting_'+name
            assets.append({'asset_id':asset_id,'version':'1','record_digest':digest(['record',asset_id]),'content_hash':digest(['painting_bytes',asset_id])})
            entities.append(entity(asset_id,asset_id,name))
            facts[asset_id+'_wall_position']={'entity_id':asset_id,'attribute':'path_offset','mutable':False,'value':[0,2,1.5]}
        # First inspection returns to the doorway; guest then follows exactly
        # the same route under its original ID, with no fresh scene instance.
        motions[0]['end_frame']=900
        motions[1].update(start_frame=900,end_frame=1800)
        motions.extend([{'motion_id':'guided_hero','entity_id':'hero','route_id':'same_route','direction':'forward','start_frame':2100,'end_frame':4200},
                        {'motion_id':'guided_guest','entity_id':'guest','route_id':'same_route','direction':'forward','start_frame':2100,'end_frame':4200}])
        events=[event('guest_arrives',1900,['guest','hero'],[('guest_visible',False,True)])]
    chapters=[{'chapter_id':'chapter_'+str(i),'start_frame':start,'end_frame':min(frames,start+4320)} for i,start in enumerate(range(0,frames,4320))]
    return create_world({'schema_version':'fsf_production_world_v1','production_id':'theory_film',
        'clock':{'num':24,'den':1},'frame_count':frames,'snapshot':seal({'schema_version':'fsf_production_assets_v1','assets':assets},'snapshot_digest'),
        'anchors':anchors,'edges':[[a,b] for a,b in zip(names,names[1:])],
        'routes':[{'route_id':'same_route','anchor_ids':names}],'entities':entities,'facts':facts,
        'events':events,'motions':motions,'chapters':chapters})


def test_purchase_and_return_keep_same_space_identity_and_carried_prop():
    w=world();initial=state_at(w,0);final=state_at(w,4319)
    assert initial['positions']['hero']==final['positions']['hero']==[0,0,0]
    assert final['facts']['bag_owner']['value']=='hero'
    assert final['positions']['bag']==final['positions']['hero']
    assert state_at(w,1500)['facts']['bag_owner']['value']=='vendor'
    a,b=interval_requirements(w,0,17),interval_requirements(w,4200,4320)
    assert a['entities']==b['entities'] and a['snapshot_digest']==b['snapshot_digest']
    assert b['visual_conformance']=='not_evaluated'


def test_negotiation_and_return_do_not_reset_bystanders_or_costume():
    w=world('corridor');before=state_at(w,1300);after=state_at(w,4319)
    assert state_at(w,2000)['facts']['hero_pose']['value']=='seated'
    assert after['facts']['hero_pose']['value']=='standing'
    for i in range(6):assert before['positions']['npc_'+str(i)]==after['positions']['npc_'+str(i)]
    assert before['facts']['hero_clothes']==after['facts']['hero_clothes']
    assert after['positions']['hero']==state_at(w,0)['positions']['hero']


def test_gallery_guest_enters_then_same_route_is_reused():
    w=world('gallery')
    assert state_at(w,1800)['facts']['guest_visible']['value'] is False
    assert state_at(w,2000)['facts']['guest_visible']['value'] is True
    assert state_at(w,1799)['positions']['hero']==state_at(w,0)['positions']['hero']
    for frame in (2100,3000,4199):
        state=state_at(w,frame)
        hero,guest=state['positions']['hero'],state['positions']['guest']
        assert guest==[hero[0],hero[1]+0.75,hero[2]]
    for name in ('painting_art_a','painting_art_b','painting_art_c'):
        assert state_at(w,500)['positions'][name]==state_at(w,3000)['positions'][name]
    early=interval_requirements(w,0,900);late=interval_requirements(w,2100,4200)
    assert [e for e in early['entities'] if e['entity_id'].startswith('painting_')]==[e for e in late['entities'] if e['entity_id'].startswith('painting_')]
    # The explicit formation avoids coincident proxies; mesh-level collision
    # and walking pose still require the render adapter and visual checks.


def test_state_persists_over_120_minute_chapter_boundaries_without_generation():
    w=world(frames=172800)
    assert len(w['chapters'])==40
    for frame in (4320,86400,172799):
        state=state_at(w,frame)
        assert state['facts']['bag_owner']['value']=='hero'
        assert state['facts']['hero_clothes']['value']=='blue_jacket'
    req=interval_requirements(w,4310,4330)
    assert req['chapters']==['chapter_0','chapter_1']
    assert req['world_digest']==w['world_digest']


@pytest.mark.parametrize('mutation',[
    lambda w:w['clock'].update(den=True),
    lambda w:w['clock'].update(num=24.0),
    lambda w:w['entities'][0].update(asset_digest='a'*64),
    lambda w:w['events'][0]['changes'][0].update(before_digest='b'*64),
    lambda w:w['events'][0]['changes'][0].update(value='unknown_actor'),
    lambda w:w['motions'][1].update(direction='forward'),
    lambda w:w['motions'][1].update(start_frame=1000),
    lambda w:w['edges'].pop(),
    lambda w:w['events'][0]['actor_ids'].append([]),
    lambda w:w['motions'][0].pop('entity_id'),
    lambda w:w['chapters'][0].update(end_frame=4319),
])
def test_contradictory_or_malformed_memory_is_refused(mutation):
    w=world();mutation(w)
    with pytest.raises(ProductionError):validate_world(seal(w,'world_digest'))


def test_immutable_costume_and_attachment_cycles_are_rejected():
    w=world();w['events'][0]['changes']=[{'fact_id':'hero_clothes','before_digest':digest('blue_jacket'),'value':'red_jacket'}]
    with pytest.raises(ProductionError,match='STATE_CONFLICT'):validate_world(seal(w,'world_digest'))
    w=world();w['facts']['hero_attached']={'entity_id':'hero','attribute':'attachment','mutable':False,'value':'bag'}
    with pytest.raises(ProductionError,match='ATTACHMENT_CYCLE'):validate_world(seal(w,'world_digest'))


def test_condition_ack_is_bound_to_world_interval_and_is_not_visual_approval():
    from fsf.production.memory import validate_world_ack
    req=interval_requirements(world('gallery'),2100,2200)
    ack={k:copy.deepcopy(req[k]) for k in ('requirements_digest','world_digest','snapshot_digest','interval')}
    ack.update(entity_ids=[e['entity_id'] for e in req['entities']],route_ids=[r['route_id'] for r in req['routes']],capability_gaps=[])
    assert validate_world_ack(req,ack)['visual_conformance']=='not_evaluated'
    ack['capability_gaps']=['multi_actor_identity']
    with pytest.raises(ProductionError,match='CAPABILITY_GAP'):validate_world_ack(req,ack)
    assert not validate_world_ack(req,ack,allow_gaps_for_probe=True)['required_conditions_available']
    ack['interval']=[0,100]
    with pytest.raises(ProductionError,match='ACK_MISMATCH'):validate_world_ack(req,ack,allow_gaps_for_probe=True)
