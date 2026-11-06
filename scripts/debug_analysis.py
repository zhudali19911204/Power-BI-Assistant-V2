"""Real configured LLM + actual Desktop analysis integration; no model writes."""
import json
import re
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import encode,now
from backend.engine.context.live_source import find_instances
from backend.schemas import SettingsInput


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--trend',action='store_true',help='Exercise a time grouping and trend analysis')
    args=parser.parse_args()
    source=create_app()
    cfg=source.state.settings.raw()
    if not source.state.settings.public()['ready']:raise RuntimeError('请先配置大模型服务')
    instances=find_instances()
    if not instances:raise RuntimeError('请先打开本机 Power BI Desktop 模型')
    directory=Path(__file__).resolve().parents[1]/'artifacts'
    directory.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='analysis-',dir=directory) as folder:
        app=create_app(Path(folder))
        app.state.settings.save(SettingsInput(provider=cfg['provider'],model=cfg['model'],base_url=cfg['base_url'],
            api_key=source.state.settings.key(cfg),enable_thinking=cfg['enable_thinking']))
        with TestClient(app) as client:
            connected=client.post('/api/models/connect',json={'port':instances[0].port})
            assert connected.status_code==200,connected.text
            model=connected.json()
            measures=model['context']['measures']
            if not measures:raise RuntimeError('测试需要模型中至少一个已有度量值')
            measure=next((m for m in measures if re.match(r'^\s*SUM\s*\(',m['expression'],re.I)),measures[0])
            prompt=(f"请查询当前模型已有度量值 [{measure['name']}] 在全模型、不添加时间或其他筛选条件下的实际值，"
                    '并用两三句话说明指标口径。直接查询这个已有指标即可，不创建度量值。')
            if args.trend:
                dates=[(table,column['name']) for table,columns in model['context']['tables'].items()
                       for column in columns if re.search(r'year.?month|年月|年度月份',column['name'],re.I)]
                if not dates:raise RuntimeError('当前模型没有可识别的年月字段')
                date_table,date_column=dates[0]
                prompt=(f"请按真实模型字段 '{date_table}'[{date_column}] 分组并排序，查询已有度量 [{measure['name']}] 的实际月度结果，"
                        '覆盖模型内有数据的月份，给出简短的趋势和主要变化分析。可补查总额或数据范围，不创建度量值。')
            conv=client.post('/api/conversations',json={'mode':'analysis','model_id':model['id']}).json()
            events=[]
            with client.stream('POST',f"/api/conversations/{conv['id']}/messages",json={'content':prompt}) as response:
                assert response.status_code==200,response.text
                for line in response.iter_lines():
                    if not line:continue
                    event=json.loads(line);events.append(event)
                    if event['type']=='status':print('STATUS:',event['text'],flush=True)
                    if event['type']=='analysis':
                        print('EVIDENCE:',encode([{'label':q['label'],'ok':q['ok'],'run_verified':q['run_verified'],
                            'row_count':q['row_count'],'truncated':q['truncated'],'error':q['error']} for q in event['queries']]),flush=True)
                    if event['type']=='error':raise RuntimeError(event['text'])
            assert events[-1]['type']=='done'
            saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
            successful=[q for q in saved['analysis'] if q['ok']]
            assert successful and all(q['run_verified'] for q in successful)
            assert saved['content'] and not saved['artifacts']
            assert any('['+q['label']+']' in saved['content'] for q in successful)
            for query in successful:
                assert client.get(f"/api/analysis/{query['id']}/csv").status_code==200
            assert client.get(f"/api/conversations/{conv['id']}/export").status_code==200
            assert not app.state.db.all('SELECT * FROM audit')
            report={'time':now(),'scenario':'trend' if args.trend else 'aggregate','llm':cfg['model'],'queries':len(saved['analysis']),
                    'successful_queries':len(successful),'desktop_run_verified':True,'answer_has_sources':True,
                    'model_writes':0}
            (directory/('analysis-trend-report.json' if args.trend else 'analysis-debug-report.json')).write_text(encode(report),encoding='utf-8')
            print('REAL_ANALYSIS_OK:',encode(report),flush=True)


if __name__=='__main__':main()
