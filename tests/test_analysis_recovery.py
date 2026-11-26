import json

import pytest

import backend.analysis as analysis
from backend import analysis_budget
from backend.analysis_budget import AnalysisBudget
from backend.analysis_protocol import parse_plan
from backend.analysis_recovery import recovery_problem
from backend.cancellation import GenerationCancelled
from backend.database import encode
from backend.powerbi import context_from_dict
from tests.test_adaptive_analysis import saved, wire
from tests.test_analysis import Provider, plan, result, send, setup


SCOPE = '0810 区域，比较两年同期的总销售额'
FILTER = """TREATAS({"0810"}, '客户'[地区])"""
ORIGINAL = f'EVALUATE ROW("同比", CALCULATE([不存在], {FILTER}))'
REPAIR = f'EVALUATE ROW("同比", CALCULATE(DIVIDE([总销售额], [同期]), {FILTER}))'
ALTERNATIVE = f"""EVALUATE SUMMARIZECOLUMNS('日期'[年份], {FILTER}, TREATAS({{6}}, '日期'[月份]),
    TREATAS({{2025,2026}}, '日期'[年份]), "金额", [总销售额])"""
ROWS = [{'年份': 2025, '金额': 100}, {'年份': 2026, '金额': 125}]


def fallback(dax=ALTERNATIVE, target='Q1', **extra):
    return encode({'scope': '另一个范围', 'queries': [{'title': '分别读取两期金额', 'dax': dax}],
                   'recovery': {'for_query': target, 'missing': '两年同期的实际金额',
                                'basis': '按年份直接取 [总销售额]，用两期实值比较'}, **extra})


def failure(error):
    return {**result(ok=False), 'error': error}


def execute(dax):
    if dax == ORIGINAL:
        return failure('找不到指定的对象')
    if dax == REPAIR:
        return failure('无法确定单个值')
    return result(ROWS)


def test_recovery_uses_the_locked_scope_and_persists_the_evidence_it_replaced(setup, monkeypatch):
    app, client, conv, provider, calls = wire(setup, monkeypatch, [
        plan(ORIGINAL, SCOPE), plan(REPAIR), fallback(), '两期分别为 100 和 125。[Q2]'], execute)
    events = send(client, conv, SCOPE)
    assert events[-1]['type'] == 'done' and calls == [ORIGINAL, REPAIR, ALTERNATIVE]
    record = saved(app, client, conv)
    assert [g['stage'] for g in record['generations']] == ['analysis_plan','analysis_repair','analysis_recovery','analysis_summary']
    assert [(r['label'], r['attempt'], r['batch']) for r in record['analysis']] == [('Q1',1,1),('Q1',2,1),('Q2',1,2)]
    replacement = record['analysis'][-1]
    assert replacement['scope'] == SCOPE and replacement['rows'] == ROWS
    assert replacement['recovery']['for_query'] == 'Q1'
    review = json.loads(provider.calls[2][1][-1].content.split('\n', 1)[1])
    assert review['locked_scope'] == SCOPE and review['recovery_candidates']['Q1']['first_error'] == '找不到指定的对象'
    assert review['recovery_candidates']['Q1']['first_dax'] == ORIGINAL
    assert review['evidence'][0]['error'] == '无法确定单个值'
    summary = json.loads(provider.calls[-1][1][-1].content.split('\n', 1)[1])
    assert summary['evidence'][-1]['recovery'] == replacement['recovery']
    assert summary['analysis_state']['attempts'] == 3
    assert 'Q1 未取得成功结果' not in record['content']
    assert '125' in client.get('/api/analysis/'+replacement['id']+'/csv').text
    assert '替代 Q1' in client.get(f"/api/conversations/{conv['id']}/export").text


def test_partial_batch_reuses_its_review_and_the_allowance_is_shared_across_batches(setup, monkeypatch):
    first = 'EVALUATE ROW("总额",[总销售额])'
    initial = encode({'queries':[{'title':'总额','dax':first},{'title':'同比','dax':ORIGINAL}]})
    later = ['EVALUATE Missing(1)', 'EVALUATE Missing(2)']
    def run(dax):
        return failure(dax) if dax in later else execute(dax)
    app, client, conv, provider, calls = wire(setup, monkeypatch, [
        initial, plan(REPAIR), fallback(target='Q2', needs_more=True), plan(later[0]), plan(later[1]),
        '已有总额和同期数据。[Q1][Q3]'], run)
    assert send(client, conv)[-1]['type'] == 'done'
    assert calls == [first, ORIGINAL, REPAIR, ALTERNATIVE, *later]
    record = saved(app, client, conv)
    assert [r['label'] for r in record['analysis'] if r['ok']] == ['Q1','Q3']
    assert 'Q4 未取得成功结果' in record['content']
    assert [g['stage'] for g in record['generations']].count('analysis_continue') == 2
    review = json.loads(provider.calls[2][1][-1].content.split('\n', 1)[1])
    assert review['evidence'][0]['ok'] and list(review['recovery_candidates']) == ['Q2']


def test_failed_alternative_is_neither_repaired_nor_followed_by_another_review(setup, monkeypatch):
    app, client, conv, provider, calls = wire(setup, monkeypatch, [
        plan(ORIGINAL), plan(REPAIR), fallback(needs_more=True)],
        lambda dax: failure('替代也失败') if dax == ALTERNATIVE else execute(dax))
    assert send(client, conv)[-1]['type'] == 'done'
    record = saved(app, client, conv)
    assert calls == [ORIGINAL, REPAIR, ALTERNATIVE] and len(provider.calls) == 3
    assert '已结束补救' in record['content'] and not any(r['ok'] for r in record['analysis'])


@pytest.mark.parametrize('reason', ['重复根因', '访问权限不足', 'The query timed out'])
def test_repeated_root_cause_or_service_error_does_not_trigger_recovery(setup, monkeypatch, reason):
    errors = iter(['Query (1, 9) 重复根因', 'Query (12, 45) '+reason])
    app, client, conv, provider, calls = wire(setup, monkeypatch, [plan(ORIGINAL),plan(REPAIR)], lambda _:failure(next(errors)))
    assert send(client, conv)[-1]['type'] == 'done'
    assert len(provider.calls) == len(calls) == 2
    assert '未取得可用于分析的数据' in saved(app,client,conv)['content']


@pytest.mark.parametrize('hint', [True, False])
def test_same_error_needs_a_grounded_alternative_clue(setup, monkeypatch, hint):
    broken = f'EVALUATE SUMMARIZECOLUMNS({FILTER}, "旧金额", [缺失度量])'
    repaired = json.loads(plan(broken))
    if hint:
        repaired['assessment'] = '可以逐期直接读取 [总销售额] 再比较'
    app, client, conv, provider, calls = wire(setup, monkeypatch, [
        plan(ORIGINAL), encode(repaired), fallback(), '已查得两期数值。[Q2]'],
        lambda dax: result(ROWS) if dax == ALTERNATIVE else failure('缺少度量'))
    assert send(client, conv, '比较两期费用' if hint else '比较总销售额')[-1]['type'] == 'done'
    assert len(calls) == 3
    review = json.loads(provider.calls[2][1][-1].content.split('\n', 1)[1])
    assert '[总销售额]' in review['recovery_candidates']['Q1']['alternative_hint']


@pytest.mark.parametrize('proposal', [
    fallback(REPAIR.replace('"同比"', '"新名称"')), fallback(target='Q8'), fallback('EVALUATE ROW("数",125)'),
    fallback(ALTERNATIVE.replace(FILTER+', ','')), fallback(ALTERNATIVE.replace('"0810"', '"0810","0887"')),
    encode({'queries':[], 'assessment':'已有依据不能提供这部分数据'}), 'invalid',
])
def test_invalid_or_declined_alternative_never_executes_another_query(setup, monkeypatch, proposal):
    app, client, conv, provider, calls = wire(setup, monkeypatch, [plan(ORIGINAL),plan(REPAIR),proposal], execute)
    assert send(client, conv)[-1]['type'] == 'done'
    assert calls == [ORIGINAL,REPAIR] and len(provider.calls) == 3
    assert '未取得可用于分析的数据' in saved(app,client,conv)['content']


@pytest.mark.parametrize('limit', ['MAX_QUERIES','MAX_BATCHES','MAX_QUERY_ATTEMPTS','time'])
def test_extra_planning_respects_all_global_budgets(setup, monkeypatch, limit):
    elapsed = [0]
    if limit == 'time':
        budget = AnalysisBudget(100,clock=lambda:elapsed[0])
        monkeypatch.setattr(analysis, 'AnalysisBudget', lambda _:budget)
    else:
        monkeypatch.setattr(analysis_budget, limit, 2 if limit == 'MAX_QUERY_ATTEMPTS' else 1)
    def run(dax):
        if limit == 'time' and dax == REPAIR:
            elapsed[0] += 73
        return execute(dax)
    app, client, conv, provider, calls = wire(setup, monkeypatch, [plan(ORIGINAL),plan(REPAIR)], run)
    assert send(client,conv)[-1]['type'] == 'done' and len(provider.calls) == len(calls) == 2
    expected = '预留时间' if limit == 'time' else '上限'
    assert expected in saved(app,client,conv)['content']


def test_successful_evidence_can_end_review_without_a_replacement(setup, monkeypatch):
    total = 'EVALUATE ROW("金额",[总销售额])'
    original = encode({'queries':[{'title':'总额','dax':total},{'title':'补充视角','dax':ORIGINAL}]})
    replies = [original, plan(REPAIR), encode({'queries':[], 'assessment':'Q1 已回答原问题'}), '按 Q1 的实查结果回答。[Q1]']
    app, client, conv, provider, calls = wire(setup, monkeypatch, replies, execute)
    assert send(client,conv)[-1]['type'] == 'done' and calls == [total,ORIGINAL,REPAIR]
    record = saved(app,client,conv)
    assert record['analysis'][0]['rows'] == ROWS
    assert [g['stage'] for g in record['generations']][-2:] == ['analysis_continue','analysis_summary']


def test_two_exhausted_queries_share_one_choice_after_the_whole_batch(setup, monkeypatch):
    one, two = 'EVALUATE MissingA()', 'EVALUATE MissingB()'
    initial = encode({'queries':[{'title':'辅助问题','dax':one},{'title':'同期金额','dax':ORIGINAL}]})
    def run(dax):
        return failure(dax) if dax in (one, two) else execute(dax)
    app,client,conv,provider,calls = wire(setup,monkeypatch,[initial,plan(two),plan(REPAIR),fallback(target='Q2'),
                                                            '可用两期金额回答同期问题。[Q3]'],run)
    assert send(client,conv)[-1]['type'] == 'done'
    assert calls == [one,two,ORIGINAL,REPAIR,ALTERNATIVE]
    review = json.loads(provider.calls[3][1][-1].content.split('\n',1)[1])
    assert list(review['recovery_candidates']) == ['Q1','Q2']
    record = saved(app,client,conv)
    assert record['analysis'][-1]['recovery']['for_query'] == 'Q2'
    assert 'Q1 未取得成功结果' in record['content']


def test_fiscal_year_comparison_preserves_a_requested_filter_even_if_the_repair_lost_it(setup):
    app,_,conv = setup
    ctx = context_from_dict(app.state.powerbi.get(conv['model_id'])['context'])
    ctx.date_tables = []
    task = '0810 区域 2026 年 6 月与去年同期相比如何'
    original = ORIGINAL.replace(FILTER, FILTER+", '日期'[年份] = 2026")
    repaired = REPAIR.replace(FILTER, "'日期'[年份] = 2026")
    attempts = [{'label':'Q1','scope':task,'dax':dax} for dax in (original,repaired)]
    assert recovery_problem(parse_plan(fallback()),{'Q1':{}},attempts,ctx,[],task) == ''
    dropped = parse_plan(fallback(ALTERNATIVE.replace(FILTER+', ','')))
    assert '未保留原筛选' in recovery_problem(dropped,{'Q1':{}},attempts,ctx,[],task)

    ctx.date_tables = ['日期']
    for attempt in attempts:
        attempt['dax'] = attempt['dax'].replace("'日期'[年份] = 2026", "'日期'[年份] = 2026, '日期'[月份] = 6")
    assert recovery_problem(parse_plan(fallback()), {'Q1':{}}, attempts, ctx, [], task) == ''
    for dax in (ALTERNATIVE.replace('{6}', '{6,7}'), ALTERNATIVE.replace('2025,2026', '2024,2026')):
        assert '未保留原筛选' in recovery_problem(parse_plan(fallback(dax)), {'Q1':{}}, attempts, ctx, [], task)
    attempts[1]['dax'] = attempts[1]['dax'].replace("'日期'[月份] = 6", "'日期'[月份] IN {6, 7}")
    added_month = parse_plan(fallback(ALTERNATIVE.replace('{6}', '{6,7}')))
    assert '未保留原筛选' in recovery_problem(added_month, {'Q1':{}}, attempts, ctx, [], task)


def test_recovery_can_pause_for_clarification_without_executing_speculative_work(setup, monkeypatch):
    reply = json.loads(fallback())
    reply['clarification'] = {'topic':'期间', 'question':'上一期采用哪种期间？',
                              'options':[{'label':'自然月','value':'采用自然月'}, {'label':'财务月','value':'采用财务月'}]}
    app,client,conv,_,calls = wire(setup,monkeypatch,[plan(ORIGINAL),plan(REPAIR),encode(reply)],execute)
    assert send(client,conv)[-1]['type'] == 'done' and len(calls) == 2
    card = saved(app,client,conv)['clarification']
    assert card['topic'] == '期间' and not card['answered']


@pytest.mark.parametrize('interrupt', ['cancel','budget'])
def test_cancel_or_budget_expiry_while_planning_never_runs_the_alternative(setup, monkeypatch, interrupt):
    app,client,conv = setup
    elapsed = [0]
    budget = AnalysisBudget(100,clock=lambda:elapsed[0])
    monkeypatch.setattr(analysis, 'AnalysisBudget', lambda _:budget)
    class Interrupted(Provider):
        def stream(self,*args,**kwargs):
            if len(self.calls) == 2:
                if interrupt == 'cancel':
                    raise GenerationCancelled()
                elapsed[0] += 75
            yield from super().stream(*args,**kwargs)
    provider = Interrupted([plan(ORIGINAL),plan(REPAIR),fallback()])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    calls = []
    monkeypatch.setattr(app.state.chat.analysis,'execute', lambda model,dax: (calls.append(dax),execute(dax))[1])
    events = send(client,conv)
    assert calls == [ORIGINAL,REPAIR]
    assert events[-1]['type'] == ('cancelled' if interrupt=='cancel' else 'done')
    assert len(saved(app,client,conv)['analysis']) == 2
