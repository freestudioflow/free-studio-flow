"""Optional neural GPU smoke recipe; no film-quality or human-review claims."""
from pathlib import Path
import argparse
import time


def run(request_path,output,receipt):
    import torch
    from PIL import Image,ImageDraw
    from huggingface_hub import snapshot_download
    from transformers import AutoProcessor,Qwen2_5_VLForConditionalGeneration
    from fsf.core.edit_store import read_json
    from fsf.remote.contracts import sha,seal,write_json
    from fsf.remote.gpu import cuda_inventory
    from fsf.remote.model_evidence import verify_model_receipt
    request=read_json(request_path)
    spec=request['recipe']['execution_requirements']['model']
    lock=spec['lock']
    if lock['model_id']!='Qwen/Qwen2.5-VL-3B-Instruct' or lock['precision']!='float16':
        raise ValueError('MODEL_PROBE_CONFIG')
    cache=Path(output).parent/'model-cache'
    folder=Path(snapshot_download(repo_id=lock['model_id'],revision=lock['revision'],
                                 allow_patterns=list(lock['files']),cache_dir=cache,token=False))
    observed={name:sha(folder/name) for name in lock['files']}
    if observed!=lock['files']:raise ValueError('MODEL_PROBE_WEIGHTS')
    if not torch.cuda.is_available():raise ValueError('MODEL_PROBE_CUDA')
    torch.set_num_threads(2)
    model=Qwen2_5_VLForConditionalGeneration.from_pretrained(folder,torch_dtype=torch.float16,
              device_map={'':0},attn_implementation='sdpa',local_files_only=True,trust_remote_code=False)
    processor=AutoProcessor.from_pretrained(folder,local_files_only=True,trust_remote_code=False,
                                           min_pixels=56*56,max_pixels=112*112)
    pictures=[]
    for x in (12,36):
        image=Image.new('RGB',(112,112),'white')
        ImageDraw.Draw(image).rectangle((x,40,x+24,64),fill='red')
        pictures.append(image)
    messages=[{'role':'user','content':[{'type':'image'},{'type':'image'},
        {'type':'text','text':'These are two ordered frames. Describe the object color and direction of movement in one short sentence.'}]}]
    prompt=processor.apply_chat_template(messages,tokenize=False,add_generation_prompt=True)
    inputs=processor(text=[prompt],images=pictures,padding=True,return_tensors='pt').to('cuda:0')
    torch.cuda.reset_peak_memory_stats()
    start,end=torch.cuda.Event(enable_timing=True),torch.cuda.Event(enable_timing=True)
    started=time.monotonic_ns();start.record()
    with torch.inference_mode():tokens=model.generate(**inputs,max_new_tokens=64,do_sample=False)
    end.record();torch.cuda.synchronize();finished=time.monotonic_ns()
    answer=processor.batch_decode(tokens[:,inputs['input_ids'].shape[1]:],skip_special_tokens=True)[0]
    output=Path(output);receipt=Path(receipt)
    write_json(output,{'scope':'synthetic_two_frame_model_smoke','model':lock['model_id'],
        'revision':lock['revision'],'answer':answer,'input_image_count':2,'generated_tokens':int(tokens.shape[1]-inputs['input_ids'].shape[1]),
        'motion_review':'not_reviewed','listening_review':'not_reviewed','calibration_passed':False})
    devices=cuda_inventory()
    physical=torch.cuda.get_device_properties(0)
    # No visibility remapping is introduced by this single-card smoke recipe.
    selected=[d['uuid'] for d in devices if d['index']==0]
    if len(selected)!=1:raise ValueError('MODEL_PROBE_DEVICE')
    value=seal({'schema_version':'fsf_model_execution_receipt_v1','request_digest':request['request_digest'],
        'lock_digest':spec['lock_digest'],'observed_files':observed,'devices':selected,'precision':'float16',
        'model_calls':1,'cuda_event_ms':float(start.elapsed_time(end)),'started_ns':started,'finished_ns':finished,
        'peak_allocated_bytes':int(torch.cuda.max_memory_allocated()),'outputs':{output.name:sha(output)}},'receipt_digest')
    write_json(receipt,value)
    verify_model_receipt(output.parent,request,devices)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--request',required=True)
    parser.add_argument('--out',required=True)
    parser.add_argument('--receipt',required=True)
    args=parser.parse_args()
    run(args.request,args.out,args.receipt)
