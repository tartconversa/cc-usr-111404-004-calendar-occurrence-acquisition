"""将来源字节身份与当前响应概览一起交给调用者；另提供按 UTC 半开窗的实例采集入口。"""
import json
import sys
from pathlib import Path
from .source import from_packet,fetch
from .baseline import analyze
from .collect import StateStore,collect_response

def inspect_response(response):
    return {'source_id':response.source_id,'response_sha256':response.sha256,'summary':analyze(response)}

def inspect(packet):return inspect_response(from_packet(packet))

def collect_one(url,source_id,**kwargs):return inspect_response(fetch(url,source_id,**kwargs))

def collect_packet(packet,**kwargs):return collect_response(from_packet(packet),**kwargs)

def collect_window(url,source_id,*,state_path=None,**kwargs):
    fetch_keys={k:kwargs.pop(k) for k in ('method','headers','body','max_bytes') if k in kwargs}
    state=StateStore(state_path) if state_path else StateStore()
    return collect_response(fetch(url,source_id,**fetch_keys),state=state,**kwargs)

def main():
    if len(sys.argv)!=2:raise SystemExit('请指定来源响应JSON路径')
    print(json.dumps(inspect(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
