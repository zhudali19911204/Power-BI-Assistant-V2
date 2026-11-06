import json

from backend.analysis import ROW_LIMIT
from tests.test_analysis import setup,Provider,plan,result,send


def test_summary_interruption_retains_actual_evidence_and_partial_response(setup,monkeypatch):
    app,client,conv=setup
    class BrokenProvider:
        calls=0
        def stream(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1:
                yield plan();return
            yield '已经查询到数据，正在分析。'
            raise OSError('test transport failure')
    monkeypatch.setattr(app.state.settings,'provider',lambda:BrokenProvider())
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:result())
    events=send(client,conv)
    assert events[-1]['type']=='error'
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['status']=='error' and '已经查询到数据' in saved['content']
    assert saved['analysis'][0]['rows']==[{'Value':125}]
    assert not app.state.chat.locks[conv['id']].locked()


def test_truncated_data_has_app_generated_caveat_even_if_model_omits_it(setup,monkeypatch):
    app,client,conv=setup
    provider=Provider([plan(),'本次可查看部分结果。'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:{**result(),'truncated':True})
    send(client,conv)
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert '本轮部分结果已截断' in saved['content']
    assert '[Q1]' in saved['content']


def test_guard_failure_does_not_open_an_engine_connection(setup,monkeypatch):
    app,client,conv=setup
    def unexpected(*args,**kwargs):raise AssertionError('Mutation must be rejected before opening a connection')
    monkeypatch.setattr('backend.analysis._LiveSession',unexpected)
    query=app.state.chat.analysis.execute(app.state.powerbi.get(conv['model_id']),'DELETE FROM Model')
    assert not query['ok'] and not query['run_verified']


def test_no_matching_rows_are_passed_as_empty_data_not_zero(setup,monkeypatch):
    app,client,conv=setup
    provider=Provider([plan(),'没有符合条件的数据。'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:result([]))
    send(client,conv)
    evidence=json.loads(provider.calls[-1][1][-1].content.split('\n',1)[1])['evidence'][0]
    assert evidence['rows']==[] and evidence['row_count']==0


def test_large_results_are_limited_by_payload_size_too(setup,monkeypatch):
    app,client,conv=setup
    monkeypatch.setattr('backend.analysis.find_adomd_dll',lambda *a:'fake')
    monkeypatch.setattr('backend.analysis.find_instances',lambda:[])
    class Session:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def query(self,*a,**kwargs):return [{'text':'测'*1000} for _ in range(ROW_LIMIT)]
    monkeypatch.setattr('backend.analysis._LiveSession',Session)
    query=app.state.chat.analysis.execute(app.state.powerbi.get(conv['model_id']),'EVALUATE ROW("x",1)')
    assert query['ok'] and query['truncated']
    assert 0<query['row_count']<ROW_LIMIT


def test_partial_query_failure_remains_explicit_in_the_final_reply(setup,monkeypatch):
    app,client,conv=setup
    query_plan=json.dumps({'queries':[{'title':'可查询的总额','dax':'EVALUATE ROW("x",1)'},
                                     {'title':'需要补充的数据','dax':'EVALUATE ROW("y",2)'}]})
    provider=Provider([query_plan,'本轮取得了部分数据。[Q1]'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    replies=iter([result(),{**result(ok=False),'connection_error':True}])
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:next(replies))
    send(client,conv)
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert 'Q2 未取得成功结果' in saved['content']
