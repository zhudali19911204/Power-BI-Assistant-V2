import copy
import json

import pytest
from fastapi import HTTPException

import backend.analysis as analysis_module
from backend.analysis_budget import AnalysisBudget
from backend.cancellation import GenerationCancelled
from backend.field_discovery import MAX_FIELD_CHECKS, MAX_FIELD_SEARCHES, discovery_question
from tests.test_analysis import Provider, plan, result, send, setup
from tests.test_field_discovery import check_plan, profile


def checks(*items):
    return json.dumps({'field_checks':list(items)}, ensure_ascii=False)


def check(terms=(), column='类别'):
    return {'table':'产品', 'column':column, 'search_terms':list(terms)}


def saved(client, conv):
    return client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]


def quota(provider):
    return json.loads(provider.calls[-1][0].split('\n', 1)[0].split('：', 1)[1])


def wire(setup, monkeypatch, replies, reader=None):
    app, client, conv = setup
    provider = Provider(replies)
    calls = []
    monkeypatch.setattr(app.state.settings, 'provider', lambda:provider)
    def preview(model, body, **options):
        calls.append((body.column, options['search_terms']))
        value = reader() if reader else profile()
        value['search_terms'] = options['search_terms']
        return value
    monkeypatch.setattr(app.state.chat.analysis.fields, 'preview', preview)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a:pytest.fail('No queries before scope confirmation'))
    return app, client, conv, provider, calls


def test_total_cap_applies_inside_a_large_request_and_returns_real_choices(setup, monkeypatch):
    requested = [check()] + [check(column=f'Field{i}') for i in range(30)]
    app, client, conv, provider, calls = wire(setup, monkeypatch, [checks(*requested), checks(*requested)])
    assert send(client, conv)[-1]['type'] == 'done'
    record = saved(client, conv)
    assert len(calls) == MAX_FIELD_CHECKS == len(record['field_previews'])
    assert record['status'] == 'complete' and not record['analysis']
    assert '12 项字段检查上限' in record['clarification']['context']
    assert record['clarification']['options'][0]['label'] == 'personnel'
    assert 'F1' in record['clarification']['options'][0]['detail']
    assert quota(provider)['field_checks_closed'] is True
    assert quota(provider)['remaining_field_checks'] == 0


def test_search_limit_prevents_endless_synonym_changes_on_one_field(setup, monkeypatch):
    replies = [checks(check([f'word{i}'])) for i in range(MAX_FIELD_SEARCHES + 2)]
    app, client, conv, provider, calls = wire(setup, monkeypatch, replies)
    assert send(client, conv)[-1]['type'] == 'done'
    assert len(calls) == MAX_FIELD_SEARCHES
    record = saved(client, conv)
    assert '同一字段已完成 3 组搜索' in record['clarification']['context']
    assert record['clarification']['options'][0]['label'] == 'personnel'


@pytest.mark.parametrize('first, second', [([], ['new word']), (['人员', 'personnel'], ['PERSONNEL'])])
def test_complete_results_are_reused_before_business_queries(setup, monkeypatch, first, second):
    app, client, conv, provider, calls = wire(setup, monkeypatch,
        [checks(check(first)), checks(check(second)), plan(), '实际125。[Q1]'])
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a:result())
    events = send(client, conv)
    assert events[-1]['type'] == 'done' and len(calls) == 1
    assert len(saved(client, conv)['analysis']) == 1
    assert any('已复用字段取值' in e.get('text', '') for e in events)
    assert 'personnel' in provider.calls[2][1][-1].content


def test_truncated_full_profile_does_not_cover_a_new_search(setup, monkeypatch):
    def truncated():
        return {**profile(), 'truncated':True}
    app, client, conv, provider, calls = wire(setup, monkeypatch,
        [checks(check()), checks(check(['new word'])), plan(), '实际125。[Q1]'], truncated)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a:result())
    assert send(client, conv)[-1]['type'] == 'done' and len(calls) == 2


def test_exact_checks_ignore_case_order_and_duplicate_search_terms(setup, monkeypatch):
    requested = [checks(check(['人员', 'personnel'])), checks(check(['PERSONNEL', '人员', 'personnel'])), plan(), '实际125。[Q1]']
    app, client, conv, provider, calls = wire(setup, monkeypatch, requested,
        lambda:{**profile(), 'truncated':True})
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a:result())
    assert send(client, conv)[-1]['type'] == 'done' and len(calls) == 1


def test_a_full_profile_covers_later_checks_in_the_same_batch(setup, monkeypatch):
    app, client, conv, provider, calls = wire(setup, monkeypatch,
        [checks(check(), check(['new word'])), plan(), '实际125。[Q1]'])
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a:result())
    assert send(client, conv)[-1]['type'] == 'done' and len(calls) == 1


def test_failed_repeat_is_not_rerun_or_turned_into_a_verified_candidate(setup, monkeypatch):
    def failed():
        raise HTTPException(422, '字段读取失败')
    app, client, conv, provider, calls = wire(setup, monkeypatch, [check_plan()] * 3, failed)
    assert send(client, conv)[-1]['type'] == 'done' and len(calls) == 1
    record = saved(client, conv)
    assert not record['field_previews'][0]['ok'] and record['field_previews'][0]['profile'] is None
    assert '未取得可安全列出的候选取值' in record['clarification']['context']
    assert 'personnel' not in str(record['clarification']['options'])


class Clock:
    value = 0
    def __call__(self):return self.value
    def advance(self, seconds):self.value += seconds


def test_slow_field_stops_the_rest_of_the_batch_and_keeps_summary_time(setup, monkeypatch):
    clock = Clock();budget = AnalysisBudget(100, clock=clock)
    monkeypatch.setattr(analysis_module, 'AnalysisBudget', lambda total:budget)
    def slow():
        clock.advance(60)
        return profile()
    requested = checks(check(), check(column='产品名称'), check(column='产品ID'))
    app, client, conv, provider, calls = wire(setup, monkeypatch, [requested, requested], slow)
    provider.generation_timeout = 100
    assert send(client, conv)[-1]['type'] == 'done' and len(calls) == 1
    assert budget.remaining(summary=True) == 40 and budget.remaining() == 15
    assert '剩余时间不足' in saved(client, conv)['clarification']['context']
    assert quota(provider)['field_checks_closed'] is True


def test_insufficient_time_after_planning_produces_a_card_without_more_io(setup, monkeypatch):
    clock = Clock();budget = AnalysisBudget(100, clock=clock)
    monkeypatch.setattr(analysis_module, 'AnalysisBudget', lambda total:budget)
    app, client, conv, provider, calls = wire(setup, monkeypatch, [])
    class SlowPlanner(Provider):
        generation_timeout = 100
        def stream(self, *args, **kwargs):
            clock.advance(74)
            yield from super().stream(*args, **kwargs)
    provider = SlowPlanner([check_plan()])
    monkeypatch.setattr(app.state.settings, 'provider', lambda:provider)
    assert send(client, conv)[-1]['type'] == 'done' and not calls
    assert budget.remaining(summary=True) == 26
    assert saved(client, conv)['clarification']


def test_field_wait_timeout_is_persisted_and_closes_further_checks(setup, monkeypatch):
    app, client, conv, provider, calls = wire(setup, monkeypatch, [check_plan(), check_plan()])
    def timeout(*args, **kwargs):
        raise HTTPException(504, '等待字段取值超时')
        yield
    monkeypatch.setattr(analysis_module, 'task_events', timeout)
    assert send(client, conv)[-1]['type'] == 'done' and not calls
    record = saved(client, conv)
    assert len(record['field_previews']) == 1 and not record['field_previews'][0]['ok']
    assert '字段取值检查超时' in record['clarification']['context']


def test_final_planner_failure_falls_back_to_the_existing_real_values(setup, monkeypatch):
    app, client, conv, provider, calls = wire(setup, monkeypatch, [check_plan(), check_plan()])
    # The provider runs out of replies on the final planning attempt.
    assert send(client, conv)[-1]['type'] == 'done' and len(calls) == 1
    assert saved(client, conv)['clarification']['options'][0]['label'] == 'personnel'


def test_cancellation_is_not_converted_to_a_scope_question(setup, monkeypatch):
    def cancelled():raise GenerationCancelled()
    app, client, conv, provider, calls = wire(setup, monkeypatch, [check_plan()], cancelled)
    assert send(client, conv)[-1]['type'] == 'cancelled'
    assert not saved(client, conv)['clarification']


def test_fallback_values_keep_codes_and_exclude_clipped_or_failed_evidence(setup):
    app, client, conv = setup
    _, ctx = app.state.powerbi.require_live()
    record = {'label':'F1', 'table':'产品', 'column':'类别', 'ok':True, 'profile':profile()}
    record['profile'].update(truncated=True, values=[
        {'value':'0257', 'value_truncated':False}, {'value':'incomplete', 'value_truncated':True},
        {'value':None, 'nonfinite':True}, {'value':'', 'value_truncated':False}])
    other = {**copy.deepcopy(record), 'table':'客户', 'column':'地区', 'label':'F2', 'ok':False}
    before = copy.deepcopy([record, other])
    card = discovery_question([record, other], ctx, '额度已用完', [])
    assert card.options[0].label == '0257' and '"0257"' in card.options[0].value
    assert len(card.options) == 2 and 'incomplete' not in str(card)
    assert 'F1' in card.options[0].detail and '样例已截断' in card.context
    assert [record, other] == before


def test_total_cap_is_shared_with_followup_batches_and_preserves_business_results(setup, monkeypatch):
    requested = [check()] + [check(column=f'Field{i}') for i in range(MAX_FIELD_CHECKS - 1)]
    initial = json.loads(plan());initial['needs_more'] = True
    next_check = checks(check(column='产品名称'))
    app, client, conv, provider, calls = wire(setup, monkeypatch,
        [checks(*requested), json.dumps(initial), next_check, next_check, '已有实际结果125。[Q1]'])
    executed = []
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda model, dax:(executed.append(dax), result())[1])
    assert send(client, conv)[-1]['type'] == 'done'
    record = saved(client, conv)
    assert len(calls) == MAX_FIELD_CHECKS and len(executed) == 1
    assert record['analysis'][0]['ok'] and record['clarification']
    assert '12 项字段检查上限' in record['clarification']['context']
    assert '125' in record['content'] and '进一步分析需要确认' in record['content']


def test_cap_still_allows_business_queries_when_the_model_has_enough_evidence(setup, monkeypatch):
    requested = [check()] + [check(column=f'Field{i}') for i in range(MAX_FIELD_CHECKS - 1)]
    app, client, conv, provider, calls = wire(setup, monkeypatch,
        [checks(*requested), plan(), '实际125。[Q1]'])
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a:result())
    assert send(client, conv)[-1]['type'] == 'done'
    record = saved(client, conv)
    assert len(calls) == MAX_FIELD_CHECKS and len(record['analysis']) == 1
    assert not record['clarification']


def test_explicit_checks_do_not_hide_inferred_filters_on_another_field(setup, monkeypatch):
    dax = 'EVALUATE CALCULATETABLE(ROW("Amount",[总销售额]),\'客户\'[地区] = "华东")'
    combined = json.loads(plan(dax));combined['field_checks'] = [check()]
    app, client, conv, provider, calls = wire(setup, monkeypatch,
        [checks(check()), json.dumps(combined), plan(dax), '实际125。[Q1]'])
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a:result())
    assert send(client, conv)[-1]['type'] == 'done'
    assert calls == [('类别', []), ('地区', ['华东'])]


def test_additional_rounds_replace_old_snapshots_but_retain_all_observations(setup, monkeypatch):
    replies = [checks(check(column=name)) for name in ('类别', '产品名称', '产品ID')]
    app, client, conv, provider, calls = wire(setup, monkeypatch, [*replies, plan(), '实际125。[Q1]'])
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a:result())
    assert send(client, conv)[-1]['type'] == 'done'
    assert len(provider.calls[1][1]) == len(provider.calls[3][1])
    content = provider.calls[3][1][-1].content
    records = json.loads(content.split('\n', 1)[1])['field_observations']
    assert [r['label'] for r in records] == ['F1', 'F2', 'F3']
