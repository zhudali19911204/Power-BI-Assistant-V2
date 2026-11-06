"""Inspect event timing over the actual desktop HTTP transport, without printing model data."""
import argparse
import json
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-url', required=True)
    parser.add_argument('--prompt', default='请只用一句话说明当前模型有多少张表，不要生成代码。')
    args = parser.parse_args()
    with httpx.Client(base_url=args.app_url, timeout=180, trust_env=False) as client:
        models=client.get('/api/models').raise_for_status().json()
        if not models:
            raise RuntimeError('No model connected')
        conv=client.post('/api/conversations',json={'model_id':models[0]['id'],'title':'流式传输诊断'}).raise_for_status().json()
        print('REQUEST_STARTED',flush=True)
        start=time.monotonic()
        with client.stream('POST',f"/api/conversations/{conv['id']}/messages",json={
            'content':args.prompt}) as response:
            print('HEADERS',response.status_code,response.headers.get('content-type'),flush=True)
            response.raise_for_status()
            count=0
            for line in response.iter_lines():
                if not line: continue
                event=json.loads(line)
                count+=1
                if event['type']!='delta' or count<6:
                    print(f"{time.monotonic()-start:.2f}s EVENT {event['type']}"
                          +(' '+event.get('text','') if event['type'] in ('status','error') else ''),flush=True)
            print('STREAM_ENDED',count,flush=True)


if __name__ == '__main__':
    main()
