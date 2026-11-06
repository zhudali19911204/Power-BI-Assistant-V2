import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.analysis import parse_plan,readonly_query,ROW_LIMIT
from backend.app import create_app
from backend.powerbi import context_from_dict


class Provider:
    def __init__(self,replies):self.replies=iter(replies);self.calls=[]
    def stream(self,system,messages,**options):
        self.calls.append((system,list(messages)))
        text=next(self.replies)
        yield text[:13];yield text[13:]


def plan(dax='EVALUATE ROW("Value",125)',scope='按用户要求汇总当前模型'):
    return json.dumps({'scope':scope,'queries':[{'title':'总额','dax':dax}]},ensure_ascii=False)


def result(rows=None,*,ok=True):
    rows=[{'Value':125}] if rows is None else rows
    return {'ok':ok,'run_verified':True,'columns':list(rows[0]) if rows else [],'rows':rows if ok else [],
            'row_count':len(rows) if ok else 0,'truncated':False,'warnings':[],
            'error':'' if ok else 'Query syntax error','seconds':0.01}


@pytest.fixture
def setup(tmp_path,monkeypatch):
    app=create_app(tmp_path)
    ctx=context_from_dict(app.state.powerbi.demo()['context'])
    model=app.state.powerbi.save('测试 Desktop','desktop',51413,ctx)
    dictionary=app.state.semantics.ensure(model,ctx)
    dictionary.update(status='ready',coverage=len(dictionary['entries']))
    app.state.semantics._store(model['id'],dictionary)
    # Analysis transport/format tests isolate their earlier prerequisites. Semantic guards have
    # dedicated integration tests with actual code candidates and confirmed definitions.
    monkeypatch.setattr(app.state.semantics,'unresolved_code',lambda *a:None)
    monkeypatch.setattr(app.state.powerbi,'require_live',lambda *a,**k:(model,ctx))
    with TestClient(app) as client:
        conv=client.post('/api/conversations',json={'model_id':model['id'],'mode':'analysis'}).json()
        yield app,client,conv


def send(client,conv,content='分析实际总额'):
    response=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':content})
    assert response.status_code==200,response.text
    return [json.loads(line) for line in response.text.splitlines()]


@pytest.mark.parametrize('query',[
    'EVALUATE ROW("label","DROP; CREATE EVALUATE")',
    '// preface\nDEFINE MEASURE \'销售\'[测试] = 2\nEVALUATE ROW("value",[测试])',
    'EVALUATE SUMMARIZECOLUMNS(\'DROP\'[EVALUATE],"Total",[UPDATE]) ORDER BY [Total] DESC',
])
def test_readonly_guard_accepts_dax_without_inspecting_literals_as_commands(query):
    assert readonly_query(query)==query


@pytest.mark.parametrize('query',[
    'SELECT * FROM Sales',
    '<Alter><Object>model</Object></Alter>',
    'EVALUATE ROW("x",1); DELETE Sales',
    'EVALUATE ROW("x",1) EVALUATE ROW("x",2)',
    'DEFINE MEASURE Sales[X]=1 EVALUATE ROW("x",[X]) EXECUTE command',
    'EVALUATE ROW("x","unterminated)',
])
def test_guard_rejects_mutations_multiple_results_or_malformed_literals(query):
    with pytest.raises(ValueError):readonly_query(query)


def test_plan_is_bounded_and_accepts_fenced_json():
    assert parse_plan('```json\n'+plan()+'\n```').queries[0].title=='总额'
    with pytest.raises(ValueError):parse_plan(json.dumps({'queries':[{'title':'q','dax':'EVALUATE ROW("x",1)'}]*5}))


def test_real_data_precedes_summary_and_is_saved_exportable_and_cascades_on_delete(setup,monkeypatch):
    app,client,conv=setup
    provider=Provider([plan(),'### 结论\n\n实际总额为 125。[Q1]'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *args:result())
    events=send(client,conv)
    assert events[-1]['type']=='done'
    evidence=next(e for e in events if e['type']=='analysis')['queries']
    assert evidence[0]['rows']==[{'Value':125}]
    assert len(provider.calls)==2
    assert '125' in provider.calls[-1][1][-1].content
    assert 'truncated' in provider.calls[-1][1][-1].content
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['analysis'][0]['rows']==[{'Value':125}]
    assert '125' in saved['content'] and not saved['artifacts']
    query_id=saved['analysis'][0]['id']
    csv=client.get(f'/api/analysis/{query_id}/csv')
    assert csv.status_code==200 and '125' in csv.text
    exported=client.get(f"/api/conversations/{conv['id']}/export").text
    assert 'EVALUATE ROW' in exported and '实际结果' in exported
    assert not app.state.chat.locks[conv['id']].locked()
    assert not app.state.db.all('SELECT * FROM audit')
    client.delete(f"/api/conversations/{conv['id']}")
    assert not app.state.db.all('SELECT * FROM analysis_queries')


def test_query_repair_preserves_each_attempt_and_does_not_reset_reply(setup,monkeypatch):
    app,client,conv=setup
    provider=Provider([plan('EVALUATE Missing()'),plan(),'实际结果是125。[Q1]'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda model,dax:result(ok='Missing' not in dax))
    events=send(client,conv)
    assert not any(event['type']=='reset' for event in events)
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert [q['ok'] for q in saved['analysis']]==[False,True]
    assert [q['attempt'] for q in saved['analysis']]==[1,2]
    assert saved['content'].startswith('### 分析口径')
    assert '实际结果是125' in saved['content']


def test_total_query_failure_never_requests_a_fabricated_numeric_summary(setup,monkeypatch):
    app,client,conv=setup
    provider=Provider([plan(),plan()])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *args:result(ok=False))
    events=send(client,conv)
    assert events[-1]['type']=='done'
    assert len(provider.calls)==2
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert '未取得可用于分析的数据' in saved['content']
    assert all(not q['ok'] for q in saved['analysis'])


def test_unclear_request_is_clarified_without_executing_queries(setup,monkeypatch):
    app,client,conv=setup
    provider=Provider([json.dumps({'clarification':'希望分析哪个指标和时间范围？','queries':[]},ensure_ascii=False)])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    def unexpected(*args):raise AssertionError('Clarification must not execute a query')
    monkeypatch.setattr(app.state.chat.analysis,'execute',unexpected)
    send(client,conv,'帮我分析一下')
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['clarification']['question']=='这次分析希望采用哪个指标口径？'
    assert len(saved['clarification']['options'])>=2
    assert '时间范围' not in saved['clarification']['question']
    assert not saved['analysis'] and len(provider.calls)==1


def test_demo_cannot_be_used_for_data_conclusions(tmp_path):
    app=create_app(tmp_path)
    with TestClient(app) as client:
        demo=client.post('/api/models/demo',json={}).json()
        assert client.post('/api/conversations',json={'mode':'analysis','model_id':demo['id']}).status_code==409


def test_follow_up_requeries_current_data_instead_of_reusing_previous_figures(setup,monkeypatch):
    app,client,conv=setup
    provider=Provider([plan(),'总额125。[Q1]',plan(),'最新总额150。[Q1]'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    answers=iter([result(),result([{'Value':150}])])
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *args:next(answers))
    send(client,conv)
    send(client,conv,'请更新这个指标')
    assert len(provider.calls[2][1])==3
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['analysis'][0]['rows']==[{'Value':150}]
    assert '150' in saved['content']


def test_engine_results_are_bounded_and_truncation_is_explicit(setup,monkeypatch):
    app,client,conv=setup
    monkeypatch.setattr('backend.analysis.find_adomd_dll',lambda *a:'fake')
    monkeypatch.setattr('backend.analysis.find_instances',lambda:[])
    seen=[]
    class Session:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def query(self,dax,max_rows):
            seen.append(max_rows)
            return [{'Index':n} for n in range(max_rows)]
    monkeypatch.setattr('backend.analysis._LiveSession',Session)
    model=app.state.powerbi.get(conv['model_id'])
    executed=app.state.chat.analysis.execute(model,'EVALUATE GENERATESERIES(1,1000)')
    assert seen==[ROW_LIMIT+1]
    assert executed['ok'] and executed['run_verified']
    assert executed['row_count']==ROW_LIMIT and executed['truncated']


def test_csv_treats_model_strings_as_data_not_formulas(setup,monkeypatch):
    app,client,conv=setup
    provider=Provider([plan(),'结果见[Q1]。'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:result([{'Label':'=2+3','Amount':-2}]))
    events=send(client,conv)
    query=next(e for e in events if e['type']=='analysis')['queries'][0]
    text=client.get(f"/api/analysis/{query['id']}/csv").text
    assert "'=2+3" in text and ',-2' in text
