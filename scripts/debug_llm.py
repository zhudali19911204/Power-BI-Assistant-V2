"""Configure and exercise a real model service without writing credentials to source or logs."""
import argparse
import getpass
import json
import sys
import time
from pathlib import Path

from backend.app import create_app
from backend.database import encode, now, uid
from backend.engine.context.live_source import find_instances
from backend.schemas import MessageInput, SettingsInput


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url')
    parser.add_argument('--model')
    parser.add_argument('--key-stdin', action='store_true')
    parser.add_argument('--configure', action='store_true')
    parser.add_argument('--extended', action='store_true', help='Also exercise UI connection test and Power Query generation')
    args = parser.parse_args()
    app = create_app()
    if args.configure:
        if args.key_stdin and not sys.stdin.isatty():
            print('WAITING_FOR_API_KEY', flush=True)
            key = sys.stdin.readline().strip()
        else:
            key = getpass.getpass('API Key (hidden): ')
        if not key:
            raise RuntimeError('No API Key supplied')
        app.state.settings.save(SettingsInput(provider='openai_compat', model=args.model,
                                               base_url=args.base_url, api_key=key))
        del key
        print('CONFIG_SAVED_ENCRYPTED', flush=True)
    settings = app.state.settings.public()
    if not settings['ready']:
        raise RuntimeError('Configure model service first')
    print('MODEL:', settings['model'], flush=True)
    started = time.monotonic()
    from backend.engine.llm.base import user
    provider = app.state.settings.provider()
    reply = provider.complete('Reply with OK only.', [user('Connection test')], max_tokens=256)
    if not reply.strip():
        raise RuntimeError('Model returned empty content on connection test')
    print(f'MODEL_CONNECTION_OK ({time.monotonic()-started:.1f}s)', flush=True)
    if args.extended:
        from fastapi.testclient import TestClient
        with TestClient(app) as client:
            result=client.post('/api/settings/test',json={})
            if result.status_code != 200:
                raise RuntimeError('UI_CONNECTION_TEST_FAILED: '+result.text)
            print('UI_CONNECTION_TEST_OK',flush=True)
    instances = find_instances()
    if not instances:
        print('NO_DESKTOP: service tested, open Desktop to test grounding', flush=True)
        return
    model = app.state.powerbi.connect(instances[0].port)
    numeric = [(t,c['name']) for t,cols in model['context']['tables'].items()
               for c in cols if c['dtype'] in ('int64','double','decimal')]
    if not numeric:
        raise RuntimeError('No numeric column found')
    numeric.sort(key=lambda p: (any(word in p[1].lower() for word in ('id','key','编号')),len(p[1])))
    table, column = numeric[0]
    conv_id, stamp = uid(), now()
    app.state.db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?)', (
        conv_id,'真实大模型联调 · DAX 生成','dax',model['id'],'',stamp,stamp))
    reference = "'"+table.replace("'","''")+"'["+column.replace(']',']]')+"]"
    request = MessageInput(content=(f'连接联调测试：请创建一个名为 V2_连接测试_总计 的度量值，对 {reference} 求和。'
        '保持简单，不添加筛选条件，不写入模型。输出一个 dax 围栏和一句简要说明。'),home_table=table)
    objects = []
    for line in app.state.chat.begin(conv_id,request,app.state.settings):
        event = json.loads(line)
        if event['type'] == 'status':
            print('STATUS:',event['text'],flush=True)
        elif event['type'] == 'artifacts':
            objects = event['artifacts']
            for obj in objects:
                vr = obj['validation']
                print('ARTIFACT:',encode({'kind':obj['kind'],'ok':vr['ok'],'run_verified':vr['run_verified'],
                                          'errors':vr['errors'],'warnings':vr['warnings']}),flush=True)
        elif event['type'] == 'error':
            raise RuntimeError(event['text'])
    if not objects or not all(o['validation']['ok'] and o['validation']['run_verified'] for o in objects):
        raise RuntimeError('Generated DAX did not pass real Desktop validation')
    # Persist only nonsensitive diagnostics in the report; no raw business values or credentials.
    report={'time':now(),'model':settings['model'],'desktop_port':model['port'],
            'table_count':len(model['context']['tables']),'generated_objects':len(objects),
            'run_verified':True,'conversation_id':conv_id,'model_writes':0}
    directory=Path(__file__).resolve().parents[1]/'artifacts'
    directory.mkdir(exist_ok=True)
    (directory/'llm-debug-report.json').write_text(encode(report),encoding='utf-8')
    print('REAL_LLM_AND_DESKTOP_OK',flush=True)
    if args.extended and model['context']['table_queries']:
        query_name=next(iter(model['context']['table_queries']))
        m_id, stamp=uid(),now()
        app.state.db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?)',(
            m_id,'真实大模型联调 · Power Query','mquery',model['id'],query_name,stamp,stamp))
        m_request=MessageInput(content='请保留当前查询的数据源和所有结果，在最后一步后增加一个不改变数据的步骤，例如 Table.SelectRows(上一步, each true)。只输出一个完整 powerquery 代码块和一句说明。',home_table=table)
        m_objects=[]
        for line in app.state.chat.begin(m_id,m_request,app.state.settings):
            event=json.loads(line)
            if event['type']=='status':
                print('POWER_QUERY_STATUS:',event['text'],flush=True)
            elif event['type']=='artifacts':
                m_objects=event['artifacts']
            elif event['type']=='error':
                raise RuntimeError(event['text'])
        if not m_objects or not all(o['validation']['ok'] and not o['validation']['run_verified'] for o in m_objects):
            raise RuntimeError('Power Query generation did not pass static lint')
        print('REAL_LLM_POWER_QUERY_STATIC_OK',flush=True)
        report['power_query_static_ok']=True
        report['power_query_conversation_id']=m_id
        (directory/'llm-debug-report.json').write_text(encode(report),encoding='utf-8')
    print('Conversation saved in local SQLite; no model objects were written.',flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Avoid dumping SDK request headers or credential-containing exceptions.
        detail = getattr(exc,'detail',None)
        print('DEBUG_FAILED:',type(exc).__name__,str(detail or (str(exc) if isinstance(exc,RuntimeError) else 'Check service connectivity and configuration')),flush=True)
        sys.exit(1)
