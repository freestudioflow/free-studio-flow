"""Regenerate standalone v4 recipe/request schemas from shared definitions.

No legacy schemas are changed. The inline copies support offline validation.
"""
from pathlib import Path
import copy
import json


def main():
    D = Path(__file__).resolve().parents[1] / 'src/fsf/schemas'
    def obj(props,required=None):return {'type':'object','properties':props,'required':list(props) if required is None else required,'additionalProperties':False}
    s={'type':'string','minLength':1,'maxLength':4096};h={'type':'string','pattern':'^[0-9a-f]{64}$'}
    state=obj({'status':{'enum':['known','unknown','not_applicable']},'value':s},['status']);state['allOf']=[{'if':{'properties':{'status':{'const':'known'}}},'then':{'required':['value']},'else':{'not':{'required':['value']}}}]
    lock=obj({'schema_version':{'const':'rc11_lock_v1'},'source':obj({'kind':{'enum':['hf_snapshot','binary_release','local_snapshot','custom']},'locator':s,'immutable_revision':s,'content_sha256':h}),'components':{'type':'array','minItems':1,'maxItems':64,'items':obj({'id':s,**{x:copy.deepcopy(state) for x in ['compute_dtype','weight_format','quantization','offload']}})},'runtime':obj({'backend_id':s,'version':s,'environment_digest':h}),'capability_digest':h,'qualification':obj({'declared':h,'observed':{'type':['string','null'],'pattern':'^[0-9a-f]{64}$'},'qualified':{'type':['string','null'],'pattern':'^[0-9a-f]{64}$'}})})
    recipe=json.loads((D/'fsf_remote_recipe_v3.schema.json').read_text());recipe['properties']['schema_version']={'const':'fsf_remote_recipe_v4'}
    recipe['properties']['input_mode']=s;recipe['required'].append('input_mode')
    consumer=obj({'kind':{'enum':['argv_group','stdin_manifest','manifest','controlled_directory']},'step_ids':{'type':'array','minItems':1,'uniqueItems':True,'items':s},'args':{'type':'array','minItems':1,'maxItems':64,'items':s}},['kind','step_ids'])
    consumer['allOf']=[{'if':{'properties':{'kind':{'const':'argv_group'}}},'then':{'required':['args']},'else':{'not':{'required':['args']}}}]
    item=recipe['properties']['inputs']['items'];item['properties'].update(required={'type':'boolean'},consumer=consumer);item['required']+=['required','consumer']
    render=obj({'engine':{'enum':['MODEL','TOOL']},'device':{'enum':['gpu','cpu']},'backend':{'enum':['cuda','none']},'device_count':{'type':'integer','minimum':0,'maximum':16},'min_vram_mb':{'type':'integer','minimum':0,'maximum':1048576}})
    model=obj({'lock':lock,'lock_digest':h,'receipt_path':s,'verifier_id':s,'verifier_digest':h})
    recipe['properties']['execution_requirements']=obj({'render':render,'model':model})
    request=json.loads((D/'fsf_remote_request_v3.schema.json').read_text());request['properties']['schema_version']={'const':'fsf_remote_request_v4'};request['properties']['protocol_version']={'const':'4'};request['properties']['recipe']=copy.deepcopy(recipe)
    for name,value in [('fsf_remote_recipe_v4',recipe),('fsf_remote_request_v4',request),('rc11_lock_v1',{'$schema':'https://json-schema.org/draft/2020-12/schema',**lock})]:
     (D/(name+'.schema.json')).write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')


if __name__ == "__main__":
    main()
