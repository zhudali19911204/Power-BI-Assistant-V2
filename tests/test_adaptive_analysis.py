import copy
import json

import pytest
from fastapi import HTTPException

import backend.analysis as analysis_module
from backend.analysis_budget import AnalysisBudget, evidence_context, query_key
from backend.cancellation import GenerationCancelled
from backend.database import encode
from tests.test_analysis import setup, Provider, plan, result, send
from tests.test_field_discovery import check_plan, profile as field_profile
from tests.test_images import upload


def batch(start=1, count=1, more=False, *, broken=False):
    return encode({'scope':'当前模型既定业务范围', 'queries':[
        {'title':f'分析视角 {n}', 'dax':f'EVALUATE Missing({n})' if broken else f'EVALUATE ROW("Value",{n})'}
        for n in range(start,start+count)], 'needs_more':more,
        'remaining_questions':['继续检查业务构成'] if more else []})


def saved(app, client, conv):
    return client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]


def wire(setup, monkeypatch, replies, execute=None):
    app,client,conv=setup
    provider=Provider(replies);calls=[]
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    def run(model,dax):
        calls.append(dax)
        return execute(dax) if execute else result()
    monkeypatch.setattr(app.state.chat.analysis,'execute',run)
    return app,client,conv,provider,calls


def test_two_batches_can_execute_more_than_four_queries_and_share_real_evidence(setup,monkeypatch):
    app,client,conv,provider,calls=wire(setup,monkeypatch,[batch(1,3,True),batch(4,3),'已完成六个分析视角。[Q6]'])
    events=send(client,conv,'分析趋势、构成并下钻异常来源')
    assert events[-1]['type']=='done' and len(calls)==6
    record=saved(app,client,conv)
    assert [r['label'] for r in record['analysis']]==[f'Q{i}' for i in range(1,7)]
    assert [r['batch'] for r in record['analysis']]==[1,1,1,2,2,2]
    continuation=json.loads(provider.calls[1][1][-1].content.split('\n',1)[1])
    assert len(continuation['evidence'])==3 and continuation['evidence'][0]['rows']==[{'Value':125}]
    summary=json.loads(provider.calls[-1][1][-1].content.split('\n',1)[1])
    assert len(summary['evidence'])==6 and summary['analysis_state']['batches']==2
    assert summary['scope']=='当前模型既定业务范围'
    assert [g['stage'] for g in record['generations']]==['analysis_plan','analysis_continue','analysis_summary']
    assert '[Q6]' in record['content'] and '第 2 批' in ''.join(e.get('text','') for e in events)


def test_reassessment_stops_when_actual_evidence_is_sufficient(setup,monkeypatch):
    app,client,conv,provider,calls=wire(setup,monkeypatch,[batch(more=True),encode({'queries':[],'assessment':'实际结果已经回答原问题'}),'实际结果125。[Q1]'])
    events=send(client,conv)
    assert events[-1]['type']=='done' and len(calls)==1 and len(provider.calls)==3
    assert '尚未完成' not in saved(app,client,conv)['content']


@pytest.mark.parametrize('size,rounds,limit_text',[(4,3,'12 条业务查询上限'),(1,4,'4 批查询上限')])
def test_total_query_and_batch_caps_stop_followups_and_preserve_summary(setup,monkeypatch,size,rounds,limit_text):
    replies=[batch(i*size+1,size,True) for i in range(rounds)]+['已取得的结果如下。[Q1]']
    app,client,conv,provider,calls=wire(setup,monkeypatch,replies)
    assert send(client,conv)[-1]['type']=='done'
    assert len(calls)==size*rounds and len(provider.calls)==rounds+1
    assert limit_text in saved(app,client,conv)['content']
    assert '继续检查业务构成' in saved(app,client,conv)['content']


def test_retry_attempts_have_an_independent_cap(setup,monkeypatch):
    replies=[]
    for start in (1,5,9):
        replies.append(batch(start,4,True,broken=True))
        for n in range(start,min(start+4,10)):replies.append(batch(n))
    replies.append('已有数据可用于部分分析。[Q1]')
    app,client,conv,provider,calls=wire(setup,monkeypatch,replies,lambda dax:result(ok='Missing' not in dax))
    assert send(client,conv)[-1]['type']=='done'
    assert len(calls)==18
    record=saved(app,client,conv)
    assert len(record['analysis'])==18 and len({r['label'] for r in record['analysis']})==9
    assert all(r['attempt'] in (1,2) for r in record['analysis'])
    assert '执行次数上限' in record['content'] and 'Q10' in record['content']


def test_duplicate_queries_are_not_executed_again_but_literals_stay_distinct(setup,monkeypatch):
    duplicate='// equivalent formatting\nevaluate  ROW ( "Value", 1 ) ;'
    followup=encode({'queries':[{'title':'同一查询','dax':duplicate}],'needs_more':True})
    app,client,conv,provider,calls=wire(setup,monkeypatch,[batch(more=True),followup,'实际结果125。[Q1]'])
    assert send(client,conv)[-1]['type']=='done' and len(calls)==1
    assert '停止重复查询' in saved(app,client,conv)['content']
    assert query_key('EVALUATE ROW("x", "A B")')!=query_key('EVALUATE ROW("x", "AB")')
    assert query_key('EVALUATE ROW("x", "A")')!=query_key('EVALUATE ROW("x", "a")')
    assert query_key('EVALUATE ROW("//", "/* hi */")')!=query_key('EVALUATE ROW("", "")')


def test_malformed_followup_keeps_successful_results_and_still_summarizes(setup,monkeypatch):
    app,client,conv,provider,calls=wire(setup,monkeypatch,[batch(more=True),'bad json','still bad','已有结果125。[Q1]'])
    assert send(client,conv)[-1]['type']=='done' and len(calls)==1
    assert saved(app,client,conv)['analysis'][0]['ok']
    assert '有效查询计划' in saved(app,client,conv)['content']


def test_truncated_result_requests_evidence_review_even_without_initial_followup_flag(setup,monkeypatch):
    values=iter([{**result(),'truncated':True},result([{'Total':500}])])
    app,client,conv,provider,calls=wire(setup,monkeypatch,[batch(),batch(2),'独立聚合总额500。[Q2]'],lambda dax:next(values))
    assert send(client,conv)[-1]['type']=='done' and len(calls)==2
    evidence=json.loads(provider.calls[1][1][-1].content.split('\n',1)[1])['evidence']
    assert evidence[0]['truncated'] and '500' in saved(app,client,conv)['content']


def test_partial_failure_can_be_followed_by_a_different_evidence_query(setup,monkeypatch):
    # The first plan includes one successful query and one query that still fails after its repair.
    initial=encode({'queries':[{'title':'总额','dax':'EVALUATE ROW("Value",1)'},{'title':'明细','dax':'EVALUATE Missing(1)'}]})
    app,client,conv,provider,calls=wire(setup,monkeypatch,[initial,batch(2,broken=True),batch(3),'已有总额和替代证据。[Q3]'],lambda dax:result(ok='Missing' not in dax))
    assert send(client,conv)[-1]['type']=='done' and len(calls)==4
    assert 'Q2 未取得成功结果' in saved(app,client,conv)['content']
    assert saved(app,client,conv)['analysis'][-1]['label']=='Q3'


def test_continuation_can_check_a_field_before_running_its_next_batch(setup,monkeypatch):
    app,client,conv,provider,calls=wire(setup,monkeypatch,[batch(more=True),check_plan(),batch(2),'结果见[Q1]和[Q2]。'])
    checked=[]
    monkeypatch.setattr(app.state.chat.analysis.fields,'preview',lambda *a,**k:(checked.append(len(calls)),field_profile())[1])
    assert send(client,conv)[-1]['type']=='done'
    assert checked==[1] and len(calls)==2
    assert len(saved(app,client,conv)['field_previews'])==1


def test_field_discovery_limit_is_shared_across_query_batches(setup,monkeypatch):
    one=check_plan()
    two=encode({'field_checks':[{'table':'客户','column':'地区'}]})
    three=encode({'field_checks':[{'table':'日期','column':'年份'}]})
    app,client,conv,provider,calls=wire(setup,monkeypatch,[one,two,batch(more=True),three,'已有结果125。[Q1]'])
    checked=[]
    monkeypatch.setattr(app.state.chat.analysis.fields,'preview',lambda *a,**k:(checked.append(1),field_profile())[1])
    assert send(client,conv)[-1]['type']=='done'
    assert len(checked)==2 and len(calls)==1
    assert '已完成两轮' in saved(app,client,conv)['content']


def test_followup_clarification_keeps_existing_results_and_waits_for_user(setup,monkeypatch):
    clarification={'topic':'时间口径','question':'补充分析采用哪个期间？','options':[
        {'label':'本年','value':'按本年范围分析'}, {'label':'上年','value':'按上年范围分析'}]}
    app,client,conv,provider,calls=wire(setup,monkeypatch,[batch(more=True),encode({'clarification':clarification}),'当前范围的实际结果125。[Q1]'])
    events=send(client,conv)
    assert events[-1]['type']=='done' and len(calls)==1
    record=saved(app,client,conv)
    assert record['clarification'] and record['analysis'][0]['ok']
    assert '进一步分析需要确认' in record['content']


def test_images_are_retained_in_continuation_and_summary(setup,monkeypatch):
    app,client,conv,provider,calls=wire(setup,monkeypatch,[batch(more=True),batch(2),'结果见[Q1]和[Q2]。'])
    img=upload(client)
    response=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'分步分析截图内容','image_ids':[img['id']]})
    assert json.loads(response.text.splitlines()[-1])['type']=='done'
    assert all(any(m.images for m in messages) for _,messages in provider.calls)


class Clock:
    value=0
    def __call__(self):return self.value
    def advance(self,seconds):self.value+=seconds


def test_slow_first_batch_stops_before_replanning_and_preserves_summary_budget(setup,monkeypatch):
    clock=Clock();budget=AnalysisBudget(100,clock=clock)
    monkeypatch.setattr(analysis_module,'AnalysisBudget',lambda total:budget)
    app,client,conv=setup
    class TimedProvider(Provider):
        generation_timeout=100
        def stream(self,*a,**k):
            if not self.calls:clock.advance(10)
            yield from super().stream(*a,**k)
    provider=TimedProvider([batch(more=True),'已取得实际结果125。[Q1]']);seen=[]
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:(clock.advance(50),result())[1])
    original=analysis_module.completion_events
    def observed(*a,**k):
        seen.append(k['timeout']);yield from original(*a,**k)
    monkeypatch.setattr(analysis_module,'completion_events',observed)
    assert send(client,conv)[-1]['type']=='done'
    assert len(provider.calls)==2 and seen==[75,40]
    assert '预留时间' in saved(app,client,conv)['content']


def test_repair_timeout_falls_back_to_summary_with_the_reserved_time(setup,monkeypatch):
    clock=Clock();budget=AnalysisBudget(100,clock=clock)
    monkeypatch.setattr(analysis_module,'AnalysisBudget',lambda total:budget)
    app,client,conv=setup
    initial=encode({'queries':[{'title':'总额','dax':'EVALUATE ROW("x",1)'},{'title':'明细','dax':'EVALUATE Missing(2)'}]})
    class TimedProvider(Provider):
        generation_timeout=100
        def stream(self,system,messages,**opts):
            if len(self.calls)==1:
                self.calls.append((system,messages));clock.advance(75)
                raise HTTPException(504,'repair timeout')
            yield from super().stream(system,messages,**opts)
    provider=TimedProvider([initial,'已取得总额125，明细尚未完成。[Q1]'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda model,dax:result(ok='Missing' not in dax))
    events=send(client,conv)
    assert events[-1]['type']=='done' and len(provider.calls)==3
    assert budget.remaining(summary=True)==25
    assert '预留时间' in saved(app,client,conv)['content']


def test_query_wait_timeout_is_recorded_and_prevents_more_engine_work(setup,monkeypatch):
    app,client,conv,provider,calls=wire(setup,monkeypatch,[batch(1,3,True),'只能解释第一项结果。[Q1]'])
    original=analysis_module.task_events;attempts=[]
    def timeout(work,**kwargs):
        attempts.append(1)
        if len(attempts)==2:raise HTTPException(504,'等待本机模型查询超时')
        yield from original(work,**kwargs)
    monkeypatch.setattr(analysis_module,'task_events',timeout)
    assert send(client,conv)[-1]['type']=='done'
    assert len(calls)==1 and len(attempts)==2
    record=saved(app,client,conv)
    assert len(record['analysis'])==2 and not record['analysis'][1]['ok']
    assert '查询超时' in record['content'] and 'Q3' in record['content']


def test_cancellation_during_followup_does_not_request_summary_or_another_batch(setup,monkeypatch):
    app,client,conv=setup
    class CancelledProvider(Provider):
        def stream(self,*a,**k):
            if self.calls:raise GenerationCancelled()
            yield from super().stream(*a,**k)
    provider=CancelledProvider([batch(more=True)])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:result())
    assert send(client,conv)[-1]['type']=='cancelled'
    record=saved(app,client,conv)
    assert record['status']=='cancelled' and len(record['analysis'])==1


def test_bounded_evidence_discloses_missing_rows_without_altering_saved_records():
    records=[{'label':f'Q{i}','title':f'Result {i}','dax':'EVALUATE ROW("x",1)',
              **result([{'value':'x'*800}] * 15)} for i in range(1,13)]
    before=copy.deepcopy(records)
    compact=evidence_context(records,max_chars=12000)
    assert len(encode(compact))<=12000 and len(compact)==12
    assert all(r['context_truncated'] and r['shown_rows']<r['row_count'] for r in compact)
    assert all(not r['truncated'] for r in compact)
    assert records==before
    repaired=[{'label':'Q1','title':'failed',**result(ok=False)}, {'label':'Q1','title':'fixed',**result()}]
    assert len(evidence_context(repaired))==1 and evidence_context(repaired)[0]['ok']
