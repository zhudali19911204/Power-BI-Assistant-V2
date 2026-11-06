"""Real server/engine checks using only constant values after a saved semantic build."""
import json
import re
import time
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import now
import backend.chat as chat_module


def main():
    app=create_app()
    row=app.state.db.one("SELECT model_id,payload FROM model_semantics ORDER BY updated_at DESC LIMIT 1")
    if not row or json.loads(row['payload'])['status']!='ready':
        raise RuntimeError('请先完成当前模型的语义梳理')
    model=app.state.powerbi.get(row['model_id'])
    app.state.powerbi.require_live(model['id'],unchanged=True)
    provider_factory=app.state.settings.provider
    requests=[]

    class ObservedProvider:
        def __init__(self,provider):self.provider=provider;self._client=provider._client
        def stream_events(self,system,messages,**opts):
            requests.append({'dictionary_in_prompt':'模型语义词典' in system,
                             'confirmed_intent_rule':'历史助手说过的口径不是用户确认' in system})
            yield from self.provider.stream_events(system,messages,**opts)

    app.state.settings.provider=lambda *a,**k:ObservedProvider(provider_factory(*a,**k))
    original_execute=app.state.chat.analysis.execute

    def constant_query_only(model,dax):
        # This integration check must not read measures, fields or business values.
        if '[' in dax or "'" in dax:
            raise HTTPException(422,'本次联调只接受常量 ROW 查询，不允许引用模型字段或度量值')
        return original_execute(model,dax)

    app.state.chat.analysis.execute=constant_query_only
    original_validate=chat_module.validate

    def constant_measures_only(ctx,objects,home_table,evaluator=None,points=None):
        if any(obj['kind']!='dax_measure' or not re.fullmatch(r'\s*(?:1\s*\+\s*1|2)\s*',obj['expression']) for obj in objects):
            raise HTTPException(422,'本次联调只接受 1+1 的常量度量值，不执行模型数据聚合')
        return original_validate(ctx,objects,home_table,evaluator,points)

    chat_module.validate=constant_measures_only
    started=time.monotonic();report={};created=[]
    with TestClient(app) as client:
        try:
            for mode,prompt in (
                ('dax','这是语义词典接入联调。只输出一个 dax 代码块：V2_Semantic_Probe = 1 + 1。'
                       '只使用常量，不读取业务字段、不添加筛选。应用会做临时验证，不写入模型。不需要询问业务口径。'),
                ('analysis','这是语义词典接入联调。请只执行一条查询 EVALUATE ROW("SemanticProbe", 1 + 1)，'
                            '只用常量，不引用字段或已有度量值，不读取任何业务数值。直接执行这个常量查询并简短说明结果，'
                            '不需要口径确认，不创建度量值。'),
            ):
                conv=client.post('/api/conversations',json={'model_id':model['id'],'mode':mode,'title':'语义接入常量联调'}).json()
                created.append(conv['id']);begin=len(requests)
                print('STATUS:',mode,'真实请求已开始',flush=True)
                with client.stream('POST',f"/api/conversations/{conv['id']}/messages",json={'content':prompt}) as response:
                    assert response.status_code==200,response.text
                    for line in response.iter_lines():
                        event=json.loads(line)
                        if event['type']=='error':raise RuntimeError(event['text'])
                saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
                assert saved['status']=='complete' and requests[begin]['dictionary_in_prompt']
                assert requests[begin]['confirmed_intent_rule']
                if mode=='dax':
                    assert saved['artifacts'] and all(a['validation']['ok'] and a['validation']['run_verified']
                                                     and a['validation']['sample']==2 for a in saved['artifacts'])
                else:
                    assert saved['analysis'] and any(q['ok'] and q['run_verified'] and
                        any(2 in record.values() for record in q['rows']) for q in saved['analysis'])
                    assert not saved['field_previews']
                report[mode]={'real_llm':True,'dictionary_in_prompt':True,'desktop_constant_query_verified':True}
                print('STATUS:',mode,'常量验证通过',flush=True)
        finally:
            chat_module.validate=original_validate
            for cid in created:
                result=client.delete(f'/api/conversations/{cid}')
                if result.status_code!=200:print('TEST_CONVERSATION_CLEANUP_PENDING',cid,flush=True)
    report.update(time=now(),model_writes=0,business_values_read=0,requests=len(requests),
                  seconds=round(time.monotonic()-started,2))
    target=Path(__file__).resolve().parents[1]/'artifacts'
    target.mkdir(exist_ok=True)
    (target/'semantic-generation-report.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
