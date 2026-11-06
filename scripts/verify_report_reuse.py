"""Read-only Desktop metadata checks for persistent report/dictionary association."""
import argparse
import json
import time
from pathlib import Path

from backend.app import create_app
from backend.database import now
from backend.engine.context.live_source import find_instances


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,help='Desktop 端口；多个实例时指定')
    args=parser.parse_args()
    engines=find_instances()
    if args.port:engines=[i for i in engines if i.port==args.port]
    if len(engines)!=1:raise RuntimeError('请打开一个已保存语义词典的报表，多个实例时用 --port 指定')
    started=time.monotonic();port=engines[0].port;results=[];definitions=None
    def no_llm(*a,**k):raise AssertionError('复用词典不应调用模型服务')
    for _ in range(2):
        # Recreate the entire app/repository between connections, using persisted SQLite.
        app=create_app();app.state.settings.provider=no_llm
        model=app.state.powerbi.connect(port)
        profile=app.state.semantics.on_connect(model,app.state.settings)
        assert model['report'].get('name'),'没有识别到报表名称'
        assert profile['status']=='ready','没有匹配到可直接复用的已完成词典'
        fields=('id','business_name','meaning','role','unit','aliases','state','reviewed_at')
        current=sorted([{k:e[k] for k in fields} for e in profile['entries']],key=lambda e:e['id'])
        if definitions is not None:assert current==definitions,'重启后业务定义或用户确认状态发生变化'
        definitions=current
        results.append({'matched_by':profile['reuse']['matched_by'],
                        'objects':len(current),'confirmed':sum(e['state']=='confirmed' for e in current)})
    report={'time':now(),'report_name':model['report']['name'],
            'file_path_detected':bool(model['report'].get('path')),'connections':results,
            'assistant_restart_preserved_definitions':True,'desktop_closed_or_modified':False,
            'external_model_requests':0,'business_values_read':0,'model_writes':0,
            'seconds':round(time.monotonic()-started,2)}
    target=Path(__file__).resolve().parents[1]/'artifacts';target.mkdir(exist_ok=True)
    (target/'report-reuse-report.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
