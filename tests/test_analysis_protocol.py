import json

import pytest

import backend.analysis as analysis_module
from backend.analysis import parse_plan
from backend.analysis_budget import AnalysisBudget
from backend.analysis_protocol import PlanFormatError
from backend.cancellation import GenerationCancelled
from backend.database import encode
from tests.test_analysis import setup, Provider, plan, result, send


def wire(setup, monkeypatch, replies):
    app, client, conv = setup
    provider = Provider(replies)
    queries = []
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    def execute(model, dax):
        queries.append(dax)
        return result(ok='Missing' not in dax)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', execute)
    return app, client, conv, provider, queries


def saved(client, conv):
    return client.get(f'/api/conversations/{conv["id"]}/messages').json()[-1]


def two_queries():
    return encode({'scope': '2026 年，保留工厂编号 0810', 'queries': [
        {'title': '月度明细', 'dax': 'EVALUATE Missing()'},
        {'title': '独立总额', 'dax': 'EVALUATE ROW("Total",125)'}]})


@pytest.mark.parametrize('wrapper', [lambda text: text, lambda text: '\ufeff \n'+text,
    lambda text: ' \n```JSON\n'+text+'\n```\n ', lambda text: '\ufeff```\n'+text+'\n```'])
def test_wrapper_cleanup_preserves_dax_literals_and_filter_values(wrapper):
    dax = 'EVALUATE ROW("编号","0810", "text", "{中文} ``  2026")'
    assert parse_plan(wrapper(plan(dax))).queries[0].dax == dax


@pytest.mark.parametrize('text,path,code', [
    ('Here is the result: '+plan(), '$', 'json_invalid'),
    (plan()+plan(), '$', 'json_invalid'),
    ('{"queries":[],"queries":[]}', '$', 'duplicate_key'),
    ('{"assessment":NaN}', '$', 'json_constant'),
    (encode({'queries': [{'title': '标题'}]}), 'queries[0].dax', 'missing'),
    (encode({'queries': {'title': '标题', 'dax': 'EVALUATE ROW("x",1)'}}), 'queries', 'list_type'),
    (encode({'queries': [], 'needs_more': 'false'}), 'needs_more', 'bool_type'),
    (encode({'queries': [{'title': '标题', 'dax': {'secret-value': 'not text'}}]}), 'queries[0].dax', 'string_type'),
    (encode({'sensitive-extra-key': 'sensitive-extra-value'}), '<未知字段>', 'extra_forbidden'),
    (encode({'queries': [{'title': '标题', 'dax': '   '}]}), 'queries[0].dax', 'value_error'),
])
def test_invalid_json_and_field_types_are_not_silently_reinterpreted(text, path, code):
    with pytest.raises(PlanFormatError) as caught:
        parse_plan(text)
    problem = caught.value.issues[0]
    assert problem['path'] == path and problem['type'] == code
    assert 'secret-value' not in str(caught.value) and 'sensitive-extra' not in str(caught.value)


def test_initial_plan_correction_uses_field_feedback_and_records_safe_diagnostics(setup, monkeypatch):
    bad = encode({'queries': [{'title': '标题', 'dax': {'private-value': 'do not persist'}}]})
    app, client, conv, provider, queries = wire(setup, monkeypatch, [bad, plan(), '总额125。[Q1]'])
    assert send(client, conv)[-1]['type'] == 'done'
    assert len(queries) == 1 and len(provider.calls) == 3
    feedback = provider.calls[1][1][-1].content
    assert 'queries[0].dax' in feedback and '需要字符串' in feedback
    assert '不猜测或替换业务条件' in feedback
    diagnostics = [row['diagnostic'] for row in saved(client, conv)['generations']]
    assert diagnostics[0]['format_validation']['issues'][0]['path'] == 'queries[0].dax'
    assert diagnostics[1]['format_validation'] == {'ok': True, 'correction_attempt': 1, 'issues': []}
    assert 'do not persist' not in encode(diagnostics)
    assert not app.state.chat.locks[conv['id']].locked()


def test_invalid_initial_plan_stops_after_one_correction_without_running_queries(setup, monkeypatch):
    app, client, conv, provider, queries = wire(setup, monkeypatch, ['bad json', 'still bad'])
    events = send(client, conv)
    assert events[-1]['type'] == 'error'
    assert len(provider.calls) == 2 and queries == []
    assert '查询规划回复格式不符合要求' in events[-1]['text'] and '已纠正一次' in events[-1]['text']
    assert 'ValidationError' not in saved(client, conv)['content']


def test_dax_repair_gets_the_same_format_correction_then_reexecutes_in_engine(setup, monkeypatch):
    malformed = encode({'queries': [{'title': '修复查询'}]})
    app, client, conv, provider, queries = wire(setup, monkeypatch,
        [plan('EVALUATE Missing()'), malformed, plan(), '修复后结果125。[Q1]'])
    events = send(client, conv)
    assert events[-1]['type'] == 'done' and len(queries) == 2
    assert 'queries[0].dax' in provider.calls[2][1][-1].content
    record = saved(client, conv)
    assert [q['ok'] for q in record['analysis']] == [False, True]
    repairs = [r for r in record['generations'] if r['stage'] == 'analysis_repair']
    assert [r['attempt'] for r in repairs] == [1, 2]
    assert repairs[1]['diagnostic']['format_validation']['correction_attempt'] == 1


def test_malformed_repair_does_not_discard_independent_query_or_its_csv(setup, monkeypatch):
    app, client, conv, provider, queries = wire(setup, monkeypatch, [two_queries(), 'bad', 'still bad',
        encode({'queries': [], 'assessment': '仅能回答总额'}), '已查得总额125，月度明细未完成。[Q2]'])
    assert send(client, conv)[-1]['type'] == 'done'
    assert len(queries) == 2 and len(provider.calls) == 5
    record = saved(client, conv)
    assert [q['ok'] for q in record['analysis']] == [False, True]
    assert 'DAX 修复回复格式不符合要求' in record['analysis'][0]['warnings'][0]
    assert record['analysis'][1]['rows'] == [{'Value': 125}]
    assert '125' in client.get(f'/api/analysis/{record["analysis"][1]["id"]}/csv').text
    assert 'ValidationError' not in record['content']
    assert '0810' in provider.calls[2][1][-3].content  # The original repair request survives correction.


def test_followup_format_correction_does_not_requery_successful_evidence(setup, monkeypatch):
    initial = encode({'queries': [{'title': '总额', 'dax': 'EVALUATE ROW("v",125)'}], 'needs_more': True})
    app, client, conv, provider, queries = wire(setup, monkeypatch, [initial,
        '{"queries":[],"needs_more":"false"}', '{"queries":[],"needs_more":false}', '总额125。[Q1]'])
    assert send(client, conv)[-1]['type'] == 'done' and len(queries) == 1
    assert 'needs_more' in provider.calls[2][1][-1].content
    assert [r['stage'] for r in saved(client, conv)['generations']] == [
        'analysis_plan', 'analysis_continue', 'analysis_continue', 'analysis_summary']


@pytest.mark.parametrize('bad', ['{}', '{"queries":[],"field_checks":[]}',
    encode({'queries': [{'title': 'x', 'dax': 'EVALUATE ROW("x",1)'}],
            'field_checks': [{'table': '客户', 'column': '地区'}]})])
def test_empty_or_conflicting_actions_are_corrected_before_any_query(setup, monkeypatch, bad):
    app, client, conv, provider, queries = wire(setup, monkeypatch, [bad, plan(), '结果125。[Q1]'])
    assert send(client, conv)[-1]['type'] == 'done' and len(queries) == 1
    assert saved(client, conv)['generations'][0]['diagnostic']['format_validation']['ok'] is False


def test_repair_with_multiple_queries_is_not_partially_executed(setup, monkeypatch):
    app, client, conv, provider, queries = wire(setup, monkeypatch,
        [plan('EVALUATE Missing()'), two_queries(), plan(), '结果125。[Q1]'])
    assert send(client, conv)[-1]['type'] == 'done' and len(queries) == 2
    assert all('"Total"' not in q for q in queries)
    assert '只能返回一个' in provider.calls[2][1][-1].content


def test_repair_can_ask_for_business_clarification_instead_of_guessing_a_query(setup, monkeypatch):
    question = encode({'clarification': {'topic': '年份', 'question': '按哪个年份统计？',
        'options': [{'label': '2025年', 'value': '2025年'}, {'label': '2026年', 'value': '2026年'}]}, 'queries': []})
    app, client, conv, provider, queries = wire(setup, monkeypatch, [two_queries(), question])
    assert send(client, conv)[-1]['type'] == 'done'
    record = saved(client, conv)
    assert len(queries) == 1 and len(provider.calls) == 2
    assert record['clarification']['topic'] == '年份' and not record['clarification']['answered']
    assert '需要确认业务口径' in record['content']


def test_format_correction_cannot_bypass_readonly_engine_guard(setup, monkeypatch):
    app, client, conv = setup
    real_execute = app.state.chat.analysis.execute
    provider = Provider([plan('EVALUATE Missing()'), 'bad', plan('DELETE FROM Sales')])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    def execute(model, dax):
        return result(ok=False) if 'Missing' in dax else real_execute(model, dax)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', execute)
    def unexpected(*args, **kwargs):
        pytest.fail('An unsafe corrected query must not reach the engine')
    monkeypatch.setattr(analysis_module, '_LiveSession', unexpected)
    assert send(client, conv)[-1]['type'] == 'done'
    record = saved(client, conv)
    assert len(record['analysis']) == 2 and all(not r['ok'] for r in record['analysis'])
    assert not record['analysis'][-1]['run_verified']


def test_format_correction_budget_is_shared_across_planning_repair_and_followup(setup, monkeypatch):
    monkeypatch.setattr('backend.analysis_budget.MAX_FORMAT_CORRECTIONS', 1)
    app, client, conv, provider, queries = wire(setup, monkeypatch,
        ['bad plan', two_queries(), 'bad repair', 'bad followup', '仅总额取得结果125。[Q2]'])
    assert send(client, conv)[-1]['type'] == 'done'
    assert len(provider.calls) == 5 and len(queries) == 2
    record = saved(client, conv)
    assert '本轮 1 次格式纠正上限' in record['content']
    assert sum(g['diagnostic'].get('format_validation', {}).get('correction_attempt', 0)
               for g in record['generations']) == 1


def test_slow_malformed_repair_does_not_spend_summary_reserve_on_format_retry(setup, monkeypatch):
    app, client, conv = setup
    elapsed = [0]
    budget = AnalysisBudget(100, clock=lambda: elapsed[0])
    monkeypatch.setattr(analysis_module, 'AnalysisBudget', lambda total: budget)
    class TimedProvider(Provider):
        def stream(self, *args, **kwargs):
            if len(self.calls) == 1:
                elapsed[0] += 72
            yield from super().stream(*args, **kwargs)
    provider = TimedProvider([plan('EVALUATE Missing()'), 'bad repair'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *args: result(ok=False))
    assert send(client, conv)[-1]['type'] == 'done' and len(provider.calls) == 2
    assert budget.remaining(summary=True) == 28
    assert '剩余时间不足以纠正格式' in saved(client, conv)['content']


@pytest.mark.parametrize('reason', ['length', 'content_filter', 'tool_calls'])
def test_interrupted_repair_is_not_misclassified_as_json_error(setup, monkeypatch, reason):
    app, client, conv = setup
    class Interrupted:
        calls = 0
        def stream_events(self, *args, **kwargs):
            self.calls += 1
            yield {'type': 'delta', 'text': plan('EVALUATE Missing()') if self.calls == 1 else '{"queries":['}
            yield {'type': 'metadata', 'finish_reason': 'stop' if self.calls == 1 else reason}
    provider = Interrupted()
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *args: result(ok=False))
    assert send(client, conv)[-1]['type'] == 'done' and provider.calls == 2
    assert 'format_validation' not in saved(client, conv)['generations'][-1]['diagnostic']


@pytest.mark.parametrize('error', [OSError('private endpoint'), GenerationCancelled()])
def test_transport_and_cancellation_never_trigger_format_retry(setup, monkeypatch, error):
    app, client, conv = setup
    class Interrupted(Provider):
        def stream(self, *args, **kwargs):
            if self.calls:
                self.calls.append((args[0], args[1]))
                raise error
            yield from super().stream(*args, **kwargs)
    provider = Interrupted([plan('EVALUATE Missing()')])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *args: result(ok=False))
    events = send(client, conv)
    assert len(provider.calls) == 2
    assert events[-1]['type'] == ('cancelled' if isinstance(error, GenerationCancelled) else 'done')
    assert 'private endpoint' not in saved(client, conv)['content']
