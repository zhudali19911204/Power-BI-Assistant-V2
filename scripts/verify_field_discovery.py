"""Real configured model + Desktop field checks + a clarification; no business aggregations/writes."""
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.engine.context.live_source import find_instances
from backend.schemas import SettingsInput


def main():
    source=create_app();cfg=source.state.settings.raw()
    instances=find_instances()
    if not instances:raise RuntimeError('Open Power BI Desktop first')
    started=time.monotonic()
    with TemporaryDirectory(prefix='field-discovery-') as folder:
        app=create_app(Path(folder))
        app.state.settings.save(SettingsInput(provider=cfg['provider'],model=cfg['model'],base_url=cfg['base_url'],
            api_key=source.state.settings.key(cfg),enable_thinking=cfg['enable_thinking']))
        model=app.state.powerbi.connect(instances[0].port)
        candidates=[(table,c['name']) for table,cols in model['context']['tables'].items() for c in cols
                    if c['dtype']=='string' and any(word in c['name'].lower() for word in ('class','category','费用','分类'))]
        if not candidates:raise RuntimeError('No categorical field for this scenario')
        table=candidates[0][0]
        prompt=(f"我想分析人员费用（personnel）。请先检查 '{table}' 中相关分类字段的实际取值，"
                '再一次只确认一项采用的分类口径。暂时不要查询金额或其他业务指标，也不要创建度量值。')
        with TestClient(app) as client:
            conv=client.post('/api/conversations',json={'model_id':model['id'],'mode':'analysis'}).json()
            with client.stream('POST',f"/api/conversations/{conv['id']}/messages",json={'content':prompt}) as response:
                assert response.status_code==200
                for line in response.iter_lines():
                    event=json.loads(line)
                    if event['type']=='error':raise RuntimeError(event['text'])
            saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
            checks=saved['field_previews']
            assert checks and any(r['ok'] and r['profile']['run_verified'] for r in checks)
            assert saved['clarification'] and 2<=len(saved['clarification']['options'])<=4
            assert not saved['analysis'] and saved['status']=='complete'
            assert not app.state.db.all('SELECT * FROM audit')
            report={'real_field_discovery':'pass','llm':cfg['model'],'field_checks':len(checks),
                    'successful_checks':sum(r['ok'] for r in checks),'clarification_options':len(saved['clarification']['options']),
                    'business_queries':0,'model_writes':0,'actual_values_logged':False,
                    'seconds':round(time.monotonic()-started,2)}
            target=Path(__file__).resolve().parents[1]/'artifacts'
            target.mkdir(exist_ok=True)
            (target/'field-discovery-report.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
