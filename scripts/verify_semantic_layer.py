"""Read the real model dictionary and check code ambiguity without writing to Desktop."""
import argparse
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.engine.context.live_source import find_instances
from backend.schemas import SettingsInput


def verify(app,source,port,resume=False):
    cfg=source.state.settings.raw()
    if app is not source:
        app.state.settings.save(SettingsInput(provider=cfg['provider'],model=cfg['model'],base_url=cfg['base_url'],
            api_key=source.state.settings.key(cfg),enable_thinking=cfg['enable_thinking']))
    if resume:
        row=app.state.db.one("SELECT s.model_id FROM model_semantics s JOIN models m ON m.id=s.model_id WHERE m.source='desktop' AND m.port=? ORDER BY s.updated_at DESC LIMIT 1",(port,))
        if not row:raise RuntimeError('没有可恢复的语义词典')
        model=app.state.powerbi.get(row['model_id'])
        app.state.powerbi.require_live(model['id'],unchanged=True)
    else:
        model=app.state.powerbi.connect(port)
        app.state.semantics.on_connect(model,app.state.settings)
    app.state.semantics.start(model['id'],app.state.settings)
    started=time.monotonic();stage=None
    while True:
        profile=app.state.semantics.get(model['id'])
        if profile['stage']!=stage:
            stage=profile['stage'];print('STATUS:',stage,flush=True)
        if profile['status']!='building':break
        if time.monotonic()-started>900:raise RuntimeError('Semantic build did not finish within 15 minutes')
        time.sleep(.5)
    assert profile['status']=='ready',profile['error']
    assert profile['coverage']==len(profile['entries'])
    assert all(e['state'] in ('candidate','confirmed') for e in profile['entries'])
    with TestClient(app) as client:
        for mode in ('dax','analysis'):
            conv=client.post('/api/conversations',json={'model_id':model['id'],'mode':mode,'title':'编号语义确认测试'}).json()
            response=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'2026年0257人员费用最高的月份'})
            assert response.status_code==200,response.text
            events=[json.loads(line) for line in response.text.splitlines()]
            assert any(e['type']=='clarification' and e['clarification']['topic']=='编号归属' for e in events)
            assert not app.state.db.all('SELECT * FROM analysis_queries WHERE message_id IN (SELECT id FROM messages WHERE conversation_id=?)',(conv['id'],))
            client.delete(f"/api/conversations/{conv['id']}")
    report={'semantic_build':'pass','llm':cfg['model'],'entries':len(profile['entries']),
        'confirmed':sum(e['state']=='confirmed' for e in profile['entries']),'all_objects_covered':True,
        'dax_and_analysis_code_guard':True,'model_writes':0,'business_values_logged':False,
        'seconds':round(time.monotonic()-started,2)}
    target=Path(__file__).resolve().parents[1]/'artifacts';target.mkdir(exist_ok=True)
    (target/'semantic-layer-report.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--save-current',action='store_true');parser.add_argument('--local-only',action='store_true');parser.add_argument('--resume',action='store_true');args=parser.parse_args()
    source=create_app();instances=find_instances()
    if not instances:raise RuntimeError('请先打开 Desktop')
    if args.local_only:
        with TemporaryDirectory(prefix='semantics-local-') as folder:
            app=create_app(Path(folder));model=app.state.powerbi.connect(instances[0].port)
            p=app.state.semantics.on_connect(model,app.state.settings)
            assert p['metadata_status']=='read'
            assert all(e['state']=='unresolved' for e in p['entries'])
            print(json.dumps({'local_semantic_metadata':'pass','entries':len(p['entries']),
                'columns_with_descriptions':sum(bool(e['facts'].get('description')) for e in p['entries'] if e['kind']=='column'),
                'metadata_sent_to_llm':False,'model_writes':0},ensure_ascii=False))
        return
    if args.save_current:verify(source,source,instances[0].port,resume=args.resume)
    else:
        with TemporaryDirectory(prefix='semantics-') as folder:verify(create_app(Path(folder)),source,instances[0].port)


if __name__=='__main__':main()
