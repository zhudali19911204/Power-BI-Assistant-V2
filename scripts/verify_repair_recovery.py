"""Read-only Desktop integration: restore a historical snapshot, repair, retain text, revalidate."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.engine.context.live_source import find_instances


ORIGINAL='这是第一版模型评价，修复时需要完整保留。\n\n```dax\n__V2_RepairProbe = THIS_FUNCTION_DOES_NOT_EXIST()\n```'
REPAIRED='下面是修订的测试度量。\n\n```dax\n__V2_RepairProbe = 1 + 1\n```'


class Provider:
    def __init__(self):self.calls=0
    def stream(self,*args,**kwargs):
        self.calls+=1
        text=ORIGINAL if self.calls==1 else REPAIRED
        for offset in range(0,len(text),9):yield text[offset:offset+9]


def main():
    instances=find_instances()
    if not instances:raise RuntimeError('请先在本机 Desktop 打开模型')
    directory=Path(__file__).resolve().parents[1]/'artifacts'
    directory.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='repair-',dir=directory) as folder:
        app=create_app(Path(folder))
        provider=Provider()
        app.state.settings.provider=lambda:provider
        with TestClient(app) as client:
            original=client.post('/api/models/connect',json={'port':instances[0].port})
            assert original.status_code==200,original.text
            model=original.json()
            app.state.powerbi.live_ids.clear()
            # A new connection must restore matching historical IDs, not just the latest one.
            latest=client.post('/api/models/connect',json={'port':instances[0].port})
            assert latest.status_code==200,latest.text
            assert app.state.powerbi.get(model['id'])['live']
            app.state.powerbi.live_ids.clear()
            conv=client.post('/api/conversations',json={'model_id':model['id']}).json()
            response=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'生成一个测试度量，计算1+1'})
            assert response.status_code==200,response.text
            events=[json.loads(line) for line in response.text.splitlines()]
            assert events[-1]['type']=='done',events[-1]
            assert not any(event['type']=='reset' for event in events)
            text=''.join(event['text'] for event in events if event['type']=='delta')
            assert text.startswith(ORIGINAL)
            assert REPAIRED in text and '验证修订 · 第 1 轮' in text
            assert provider.calls==2,provider.calls
            obj=next(event for event in events if event['type']=='artifacts')['artifacts'][0]
            assert obj['validation']['ok'] and obj['validation']['run_verified'],obj['validation']
            assert obj['validation']['sample']==2
            saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
            assert saved['content']==text
            assert ORIGINAL in client.get(f"/api/conversations/{conv['id']}/export").text
            revalidated=client.post(f"/api/messages/{saved['id']}/validate",json={})
            assert revalidated.status_code==200,revalidated.text
            assert revalidated.json()['artifacts'][0]['validation']['run_verified']
            assert not app.state.db.all('SELECT * FROM audit')
            print('PASS: historical connection restored; actual Desktop error triggered repair; original text retained; '
                  'repaired DAX evaluated to 2; history/export retained all versions; manual revalidation passed; no model writes')


if __name__=='__main__':main()
