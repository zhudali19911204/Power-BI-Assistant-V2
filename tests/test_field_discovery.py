import json

import pytest
from fastapi import HTTPException

from tests.test_analysis import Provider,plan,result,send,setup
from tests.test_clarifications import clarify,question


def check_plan(**extra):
    return json.dumps({'field_checks':[{'table':'产品','column':'类别','search_terms':['人员','personnel'],
                                       'reason':'核对人员费用分类'}],'queries':[],**extra},ensure_ascii=False)


def profile():
    return {'table':'产品','column':'类别','dtype':'string','values':[{'value':'personnel','count':3,'value_truncated':False}],
            'samples':[{'value':'personnel','value_truncated':False}],
            'statistics':{'row_count':5,'distinct_count':2,'blank_count':1,'empty_string_count':0,'matched_distinct_count':1},
            'truncated':False,'search_terms':['人员','personnel'],'limit':20,'scope':'全模型，未应用报表筛选',
            'checked_at':'2026-11-06T00:00:00Z','seconds':0.01,'dax_queries':['EVALUATE ROW("x",1)'],'run_verified':True}


def matching_question():
    card=question()
    card['question']='是否按产品类别中的 personnel 确认人员费用？'
    card['options']=[{'label':'按此实际分类','value':"使用 '产品'[类别] = personnel",'detail':'F1 实际返回 personnel'},
                     {'label':'补充业务定义','value':'由用户补充人员费用分类范围'}]
    return card


def test_reads_actual_values_before_clarifying_and_persists_and_exports_sources(setup,monkeypatch):
    app,client,conv=setup
    provider=Provider([check_plan(),clarify(matching_question())]);calls=[]
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis.fields,'preview',lambda *a,**k:(calls.append((a,k)),profile())[1])
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:pytest.fail('No business queries while awaiting confirmation'))
    events=send(client,conv,'分析人员费用')
    assert len(calls)==1 and calls[0][1]['automatic']
    kinds=[e['type'] for e in events]
    assert kinds.index('field_previews')<kinds.index('clarification')
    evidence=provider.calls[1][1][-1].content
    assert 'personnel' in evidence and 'blank_count' in evidence and 'field_observations' in evidence
    assert 'dax_queries' not in evidence
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['field_previews'][0]['profile']['values'][0]['value']=='personnel'
    assert saved['clarification'] and not saved['analysis']
    exported=client.get(f"/api/conversations/{conv['id']}/export").text
    assert '字段取值检查 F1' in exported and 'personnel' in exported
    client.delete(f"/api/conversations/{conv['id']}")
    assert not app.state.db.all('SELECT * FROM field_observations')


def test_premature_question_referencing_real_fields_triggers_read_before_it_is_shown(setup,monkeypatch):
    app,client,conv=setup
    provider=Provider([clarify(matching_question()),clarify(matching_question())]);calls=[]
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis.fields,'preview',lambda *a,**k:(calls.append(k),profile())[1])
    events=send(client,conv,'分析人员费用')
    assert len(calls)==1 and calls[0]['search_terms']==[]
    assert [e['type'] for e in events].count('clarification')==1
    assert len(provider.calls)==2


def test_inferred_filter_value_is_checked_before_business_query_without_rewriting_it(setup,monkeypatch):
    app,client,conv=setup
    dax='EVALUATE CALCULATETABLE(ROW("Amount",[总销售额]),\'产品\'[类别] = "personnel")'
    provider=Provider([plan(dax),plan(dax),'实际结果125。[Q1]']);order=[]
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis.fields,'preview',lambda *a,**k:(order.append(('field',k['search_terms'])),profile())[1])
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda model,query:(order.append(('business',query)),result())[1])
    events=send(client,conv,'分析人员费用')
    assert order==[('field',['personnel']),('business',dax)]
    assert events[-1]['type']=='done'


def test_explicit_field_check_precedes_metrics_and_summary_still_uses_business_results(setup,monkeypatch):
    app,client,conv=setup
    provider=Provider([check_plan(),plan(),'实际125。[Q1]']);order=[]
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis.fields,'preview',lambda *a,**k:(order.append('field'),profile())[1])
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:(order.append('business'),result())[1])
    send(client,conv,'分析人员费用')
    assert order==['field','business']
    assert '125' in provider.calls[-1][1][-1].content
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert len(saved['field_previews'])==1 and len(saved['analysis'])==1


@pytest.mark.parametrize('status',[409,422])
def test_failed_field_read_is_saved_as_failure_and_never_as_an_empty_success(setup,monkeypatch,status):
    app,client,conv=setup
    provider=Provider([check_plan(),clarify(matching_question())])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    def failed(*a,**k):raise HTTPException(status,'字段读取失败')
    monkeypatch.setattr(app.state.chat.analysis.fields,'preview',failed)
    events=send(client,conv,'分析人员费用')
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    record=saved['field_previews'][0]
    assert not record['ok'] and record['profile'] is None and record['error']=='字段读取失败'
    assert events[-1]['type']==('error' if status==409 else 'done')
    if status==422:assert '失败不代表字段无值' in provider.calls[1][1][-1].content


def test_duplicate_checks_cannot_loop_forever(setup,monkeypatch):
    app,client,conv=setup
    provider=Provider([check_plan(),check_plan(),check_plan()]);calls=[]
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis.fields,'preview',lambda *a,**k:(calls.append(1),profile())[1])
    events=send(client,conv)
    assert len(calls)==1 and len(provider.calls)==3 and events[-1]['type']=='done'
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['clarification'] and 'personnel' in saved['content']
    assert '重复读取' in saved['clarification']['context']


def test_more_than_four_checks_run_in_one_request_across_two_rounds(setup,monkeypatch):
    app,client,conv=setup
    checks=[{'table':'产品','column':name} for name in ('类别','产品名称','产品ID')]
    checks += [{'table':'客户','column':name} for name in ('客户名称','地区','客户ID')]
    more=[{'table':'销售明细','column':name} for name in ('订单日期','销售额')]
    provider=Provider([json.dumps({'field_checks':checks}),json.dumps({'field_checks':more}),clarify(matching_question())]);calls=[]
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis.fields,'preview',lambda model,body,**k:(calls.append((body.table,body.column)),profile())[1])
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:pytest.fail('No metrics before confirmation'))
    events=send(client,conv)
    assert calls==[(c['table'],c['column']) for c in checks+more] and events[-1]['type']=='done'
    assert len(provider.calls)==3 and '剩余 6 项' in provider.calls[1][1][-1].content
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['status']=='complete' and len(saved['field_previews'])==8
    assert saved['clarification'] and not saved['analysis']


def test_plan_accepts_more_than_four_field_checks_but_business_queries_are_still_bounded():
    from backend.analysis import parse_plan
    checks=[{'table':'产品','column':f'Field{i}'} for i in range(20)]
    assert len(parse_plan(json.dumps({'field_checks':checks})).field_checks)==20
    with pytest.raises(ValueError):
        parse_plan(json.dumps({'queries':[{'title':'查询','dax':'EVALUATE ROW("x",1)'}]*5}))


def test_implicit_checks_are_not_truncated_to_four_fields():
    from backend.analysis import AnalysisService,parse_plan
    from backend.engine.context.base import Column,ModelContext
    ctx=ModelContext(tables={'分类表':[Column(f'分类{i}','string','分类表') for i in range(7)]})
    card=matching_question();card['context']=' '.join(f"'分类表'[分类{i}]" for i in range(7))
    plan=parse_plan(clarify(card))
    assert len(AnalysisService.implicit_checks(plan,ctx,'分析费用'))==7


def test_third_round_can_continue_to_business_queries(setup,monkeypatch):
    app,client,conv=setup
    batches=[[{'table':'产品','column':f'Field{i}'} for i in range(6)],
             [{'table':'客户','column':'地区'}],[{'table':'日期','column':'年份'}]]
    provider=Provider([*[json.dumps({'field_checks':batch}) for batch in batches],plan(),'实际125。[Q1]']);calls=[]
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis.fields,'preview',lambda *a,**k:(calls.append(1),profile())[1])
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:result())
    events=send(client,conv)
    assert len(calls)==8 and events[-1]['type']=='done'
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert len(saved['analysis'])==1 and len(saved['field_previews'])==8
