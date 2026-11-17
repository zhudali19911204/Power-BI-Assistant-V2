import json

import pytest

from backend.analysis_protocol import parse_plan, validate_action, PlanFormatError
from backend.database import encode
from tests.test_analysis import setup, Provider, plan, result, send
from tests.test_clarifications import clarify, question, latest, reply
from tests.test_field_discovery import check_plan, profile


def lookup(*queries):
    return encode({'semantic_queries': list(queries)})


def wire(setup, monkeypatch, replies):
    app, client, conv = setup
    provider = Provider(replies)
    executed = []
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute',
                        lambda model, dax: (executed.append(dax), result())[1])
    monkeypatch.setattr(app.state.semantics.values, 'locate',
                        lambda *a: pytest.fail('Must not scan fields before AI interpretation'))
    return app, client, conv, provider, executed


@pytest.mark.parametrize('payload', [
    {'semantic_queries': '收入'}, {'semantic_queries': [' ']}, {'semantic_queries': [123]},
    {'semantic_queries': ['x' * 301]}, {'semantic_queries': ['a', 'b', 'c', 'd']},
    {'semantic_queries': ['收入'], 'queries': [{'title': 'x', 'dax': 'EVALUATE ROW("x",1)'}]},
    {'semantic_queries': ['收入'], 'field_checks': [{'table': '产品', 'column': '类别'}]},
])
def test_semantic_actions_are_bounded_and_exclusive(payload):
    with pytest.raises(PlanFormatError):validate_action(parse_plan(encode(payload)), 'analysis_plan')


def test_confirmation_takes_priority_and_repairs_cannot_start_semantic_discovery():
    parsed = validate_action(parse_plan(clarify(semantic_queries=['收入'])), 'analysis_plan')
    assert parsed.clarification and not parsed.semantic_queries
    with pytest.raises(PlanFormatError):validate_action(parse_plan(lookup('收入')), 'analysis_repair')


def test_model_uses_semantic_layer_then_analyzes_without_forced_code_confirmation(setup, monkeypatch):
    scope = '按现有总销售额度量的定义分析，0810 保留前导零。'
    app, client, conv, provider, executed = wire(setup, monkeypatch,
        [lookup('[总销售额]'), plan(scope=scope), '实际总额为 125。[Q1]'])
    before = app.state.semantics.get(conv['model_id'])
    events = send(client, conv, '分析0810的总销售额')
    assert events[-1]['type'] == 'done' and len(executed) == 1
    assert not any(e['type'] == 'clarification' for e in events)
    assert any('正在查询语义层' in e.get('text', '') for e in events)
    retrieved = json.loads(provider.calls[1][0].split('语义层补充检索', 1)[1].split('\n', 1)[1])
    assert retrieved[0]['ok'] and retrieved[0]['query'] == '[总销售额]'
    assert any(e['name'] == '总销售额' and e['kind'] == 'measure'
               for e in retrieved[0]['context']['entries'])
    assert '语义层补充检索' in provider.calls[-1][0]
    assert scope in latest(client, conv)['content']
    assert app.state.semantics.get(conv['model_id']) == before
    assert not app.state.semantics.bindings(conv['id'])


def test_semantic_lookup_can_lead_to_targeted_value_check_then_query(setup, monkeypatch):
    app, client, conv, provider, executed = wire(setup, monkeypatch,
        [lookup('人员费用'), check_plan(), plan(), '总额125。[Q1]'])
    checks = []
    monkeypatch.setattr(app.state.chat.analysis.fields, 'preview',
                        lambda *a, **k: (checks.append(k), profile())[1])
    events = send(client, conv, '分析0810人员费用')
    assert events[-1]['type'] == 'done' and len(checks) == len(executed) == 1
    assert '语义层补充检索' in provider.calls[2][0]
    assert 'personnel' in provider.calls[2][1][-1].content
    assert latest(client, conv)['field_previews'][0]['ok']


def test_remaining_ambiguity_offers_choices_and_user_selection_reaches_next_turn(setup, monkeypatch):
    card = question(topic='统计日期', text='按哪个日期统计费用？')
    card['options'] = [{'label': '入账日期', 'value': '按入账日期统计费用'},
                       {'label': '支付日期', 'value': '按支付日期统计费用'}]
    app, client, conv, provider, executed = wire(setup, monkeypatch,
        [lookup('费用统计日期'), clarify(card), plan(), '总额125。[Q1]'])
    events = send(client, conv, '分析0810费用')
    assert not executed
    saved = latest(client, conv)
    assert next(e['clarification'] for e in events if e['type'] == 'clarification')['question'] == card['question']
    response = reply(client, conv, saved, option='B')
    assert response.status_code == 200 and len(executed) == 1
    assert '按支付日期统计费用' in provider.calls[2][1][-1].content
    assert not app.state.semantics.bindings(conv['id'])


@pytest.mark.parametrize('failure', [False, True])
def test_repeated_semantic_lookup_is_reused_then_closed_even_after_failure(setup, monkeypatch, failure):
    app, client, conv, provider, executed = wire(setup, monkeypatch,
        [lookup('Revenue'), lookup(' revenue '), lookup('REVENUE')])
    context = app.state.semantics.context
    calls = []
    def tracked(model_id, text='', **kwargs):
        if kwargs.get('max_chars') == 6000:
            calls.append(text)
            if failure:raise RuntimeError('unavailable')
        return context(model_id, text, **kwargs)
    monkeypatch.setattr(app.state.semantics, 'context', tracked)
    events = send(client, conv, '分析收入')
    assert calls == ['Revenue'] and len(provider.calls) == 3 and not executed
    assert events[-1]['type'] == 'done' and any(e['type'] == 'clarification' for e in events)
    assert '"semantic_queries_closed": true' in provider.calls[2][0]
    if failure:assert '不能视为定义不存在' in provider.calls[1][0]
    assert not app.state.chat.locks[conv['id']].locked()


def test_closing_semantic_lookup_still_allows_a_final_evidence_based_query(setup, monkeypatch):
    app, client, conv, provider, executed = wire(setup, monkeypatch,
        [lookup('收入'), lookup('收入'), plan(), '总额125。[Q1]'])
    events = send(client, conv)
    assert events[-1]['type'] == 'done' and len(executed) == 1
    assert not any(e['type'] == 'clarification' for e in events)


def test_semantic_budget_is_shared_across_query_batches(setup, monkeypatch):
    first = json.loads(plan())
    first.update(needs_more=True, remaining_questions=['进一步解释'])
    app, client, conv, provider, executed = wire(setup, monkeypatch,
        [lookup('a', 'b', 'c'), encode(first), lookup('d', 'e', 'f'), lookup('g'), '已完成部分分析。[Q1]'])
    calls = []
    context = app.state.semantics.context
    def tracked(model_id, text='', **kwargs):
        if kwargs.get('max_chars') == 6000:calls.append(text)
        return context(model_id, text, **kwargs)
    monkeypatch.setattr(app.state.semantics, 'context', tracked)
    events = send(client, conv)
    assert calls == ['a', 'b', 'c', 'd', 'e', 'f'] and len(executed) == 1
    assert events[-1]['type'] == 'done' and any(e['type'] == 'clarification' for e in events)
    assert '"remaining_semantic_queries": 0' in provider.calls[3][0]


def test_field_results_survive_an_intervening_semantic_lookup(setup, monkeypatch):
    app, client, conv, provider, executed = wire(setup, monkeypatch,
        [check_plan(), lookup('人员费用'), plan(), '总额125。[Q1]'])
    monkeypatch.setattr(app.state.chat.analysis.fields, 'preview', lambda *a, **k: profile())
    events = send(client, conv)
    assert events[-1]['type'] == 'done' and len(executed) == 1
    assert 'personnel' in provider.calls[2][1][-1].content
    assert '语义层补充检索' in provider.calls[2][0]


@pytest.mark.parametrize('during_lookup', [False, True])
def test_time_budget_stops_semantic_work_without_inventing_business_candidates(setup, monkeypatch, during_lookup):
    from backend.analysis_budget import AnalysisBudget
    elapsed = [0]
    budget = AnalysisBudget(100, clock=lambda: elapsed[0])
    monkeypatch.setattr('backend.analysis.AnalysisBudget', lambda total: budget)
    app, client, conv, provider, executed = wire(setup, monkeypatch, [lookup('统计日期')])
    stream, context = provider.stream, app.state.semantics.context
    lookups = []
    def slow_stream(*args, **kwargs):
        yield from stream(*args, **kwargs)
        if not during_lookup:elapsed[0] = 74
    def slow_context(model_id, text='', **kwargs):
        if kwargs.get('max_chars') == 6000:
            lookups.append(text)
            elapsed[0] = 74
        return context(model_id, text, **kwargs)
    monkeypatch.setattr(provider, 'stream', slow_stream)
    monkeypatch.setattr(app.state.semantics, 'context', slow_context)
    events = send(client, conv)
    assert len(lookups) == int(during_lookup) and len(provider.calls) == 1 and not executed
    assert events[-1]['type'] == 'done'
    card = next(e['clarification'] for e in events if e['type'] == 'clarification')
    assert card['topic'] == '分析口径' and '时间不足' in card['context']
    assert '字段' not in card['question']


def test_invalid_semantic_action_is_corrected_before_lookup(setup, monkeypatch):
    app, client, conv, provider, executed = wire(setup, monkeypatch,
        [encode({'semantic_queries': '收入'}), lookup('收入'), plan(), '总额125。[Q1]'])
    events = send(client, conv)
    assert events[-1]['type'] == 'done' and len(executed) == 1
    assert 'semantic_queries' in provider.calls[1][1][-1].content
    assert '需要数组' in provider.calls[1][1][-1].content
    assert '语义层补充检索' in provider.calls[2][0]
