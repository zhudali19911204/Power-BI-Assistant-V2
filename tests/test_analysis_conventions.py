import json

import pytest

from backend.analysis_conventions import convention_issues
from backend.analysis_protocol import parse_plan
from backend.business_dictionary import select
from backend.database import encode
from backend.engine.context.base import Measure
from backend.model_prompt import ModelPrompt
from backend.semantics import object_id
from tests.test_analysis import Provider, plan, result, send
from tests.test_business_dictionary import term, url
from tests.test_semantics import ready, setup


FIELD = object_id('column', 'Dim Cost Center', 'CC Number')
REF = "'Dim Cost Center'[CC Number]"
FULL = f'EVALUATE ROW("金额", CALCULATE([Actual Amount], TREATAS({{"0257","0810"}}, {REF})))'
TOTAL = 'EVALUATE ROW("金额", [Actual Amount])'
MARKER = '本轮已确认的分析约定（词典数据）：\n'


def convention(**changes):
    return dict({'kind': 'convention', 'title': '默认成本中心', 'meaning': '未指定时取这两个成本中心',
                 'confirmed': True, 'filters': [{'object_id': FIELD, 'operator': 'in', 'values': ['0257', '0810']}]},
                **changes)


def focused(system):
    return json.JSONDecoder().raw_decode(system.split(MARKER, 1)[1])[0]


def test_implicit_confirmed_default_gets_space_alongside_exact_business_terms(setup):
    app, client, model, ctx, _ = setup
    ready(app, model, ctx)
    endpoint = url(model, '/items')
    for i in range(8):
        assert client.post(endpoint, json=term(title=f'词条{i}', confirmed=True)).is_success
    assert client.post(endpoint, json=convention()).is_success
    selected = select(app.state.semantics.get(model['id']), {'entries': []}, '费用实绩', budget=1800)
    assert [r['title'] for r in selected['conventions']] == ['默认成本中心']
    assert selected['terms'] and selected['omitted'] == 8 - len(selected['terms'])


def test_ignored_default_replanned_before_any_read_and_shared_with_summary(setup, monkeypatch):
    app, client, model, ctx, conv = setup
    ready(app, model, ctx)
    for body in (convention(), convention(title='候选口径', confirmed=False),
                 convention(title='采购专用口径', scope='topic', topic='采购')):
        assert client.post(url(model, '/items'), json=body).is_success
    partial = FULL.replace(',"0810"', '')
    provider = Provider([plan(partial, scope='汇总 0257 和 0810 的费用'), plan(FULL), '实际费用为 125。[Q1]'])
    calls = []
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda _, dax: (calls.append(dax), result())[1])
    events = send(client, conv, '汇总实际费用')
    assert events[-1]['type'] == 'done' and calls == [FULL]
    assert any('尚未核对已确认的分析约定' in e.get('text', '') for e in events)
    assert 'C1' in provider.calls[1][1][-1].content
    for system, _ in provider.calls:
        rules = focused(system)
        assert list(rules) == ['C1'] and rules['C1']['title'] == '默认成本中心'
        assert rules['C1']['filters'] == [{'table': 'Dim Cost Center', 'column': 'CC Number',
                                          'operator': 'in', 'values': ['0257', '0810']}]


def test_user_exception_only_exempts_its_query_and_reaches_saved_evidence(setup, monkeypatch):
    app, client, model, ctx, conv = setup
    ready(app, model, ctx)
    assert client.post(url(model, '/items'), json=convention()).is_success
    reason = '用户指定以全部成本中心为分母'
    def comparison(numerator):
        return encode({'queries': [{'title': '默认范围', 'dax': numerator},
                                   {'title': '全量分母', 'dax': TOTAL, 'convention_exceptions': {'C1': reason}}]})
    provider = Provider([comparison(TOTAL), comparison(FULL), '占比见本轮查询。[Q1][Q2]'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    executed = []
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda _, dax: (executed.append(dax), result())[1])
    assert send(client, conv, '默认范围的费用占全部成本中心的多少？')[-1]['type'] == 'done'
    assert executed == [FULL, TOTAL]
    saved = client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]['analysis']
    evidence = json.loads(provider.calls[-1][1][-1].content.split('\n', 1)[1])['evidence']
    for first, second in (saved, evidence):
        assert 'convention_exceptions' not in first
        assert second['convention_exceptions'] == {'C1': {'title': '默认成本中心', 'reason': reason}}
    assert reason in client.get(f"/api/conversations/{conv['id']}/export").text


def test_repeated_omission_stops_before_execution(setup, monkeypatch):
    app, client, model, ctx, conv = setup
    ready(app, model, ctx)
    client.post(url(model, '/items'), json=convention())
    provider = Provider([plan(TOTAL)] * 3)
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *args: pytest.fail('Unchecked query ran'))
    events = send(client, conv, '汇总费用')
    assert events[-1] == {'type': 'done', 'partial': True}
    record = client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert '分析约定' in record['content'] and record['analysis_resume']
    assert len(provider.calls) == 3 and not record['analysis']


@pytest.mark.parametrize('failed_rule', [1, 2])
def test_two_rejections_execute_only_queries_with_all_confirmed_filters(setup, monkeypatch, failed_rule):
    app, client, model, ctx, conv = setup
    ready(app, model, ctx)
    other = object_id('column', 'Dim Material', 'Material Code')
    for body in (convention(), convention(title='默认材料', filters=[{'object_id':other,'operator':'in','values':['P01']}])):
        assert client.post(url(model, '/items'), json=body).is_success
    material = 'TREATAS({"P01"},\'Dim Material\'[Material Code])'
    full = FULL.replace('[Actual Amount],', '[Actual Amount],'+material+',')
    bad = FULL if failed_rule==2 else f'EVALUATE ROW("金额",CALCULATE([Actual Amount],{material}))'
    plans = encode({'queries':[{'title':'基础', 'dax':full}, {'title':'分项', 'dax':bad}]})
    provider = Provider([plans, plans, '基础结果 [Q1]'])
    executed = []
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda _, dax: (executed.append(dax), result())[1])
    assert send(client, conv, '分析 P01 费用')[-1]['partial']
    assert executed==[full] and len(provider.calls)==3
    record=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert record['analysis_resume'] and '分项' in record['content']


def test_same_convention_and_unchanged_filter_evidence_do_not_spend_another_correction(setup, monkeypatch):
    from tests.test_field_discovery import profile
    app, client, model, ctx, conv = setup
    ready(app, model, ctx)
    client.post(url(model, '/items'), json=convention())
    field=encode({'field_checks':[{'table':'Dim Material', 'column':'Material Code'}]})
    provider=Provider([plan(TOTAL), field, plan(TOTAL), plan(FULL), '结果 [Q1]'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis.fields,'preview',lambda *a,**k:profile())
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:result())
    assert send(client,conv,'汇总费用')[-1]=={'type':'done'}
    record=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert [g['stage'] for g in record['generations']]==['analysis_plan']*3+['analysis_minimal','analysis_summary']
    assert sum(g['diagnostic'].get('format_validation',{}).get('correction_attempt',0) for g in record['generations'])==1


def test_auto_column_uses_verified_text_type_for_quoted_filters(setup, monkeypatch):
    app, client, model, ctx, conv = setup
    ctx.tables['Dim Cost Center'][0].dtype = 'auto'
    profile = ready(app, model, ctx)
    profile['metadata_status'] = 'read'
    next(e for e in profile['entries'] if e['id'] == FIELD)['facts']['actual_dtype'] = 'string'
    app.state.semantics._store(model['id'], profile)
    client.post(url(model, '/items'), json=convention(filters=[{'object_id':FIELD, 'operator':'in', 'values':['2026']}]))
    dax = FULL.replace('"0257","0810"', '"2026"')
    provider = Provider([plan(dax.replace('"2026"', '2026')), plan(dax), '费用来自 [Q1]'])
    executed = []
    monkeypatch.setattr(app.state.settings,'provider',lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda _, q: (executed.append(q),result())[1])
    assert send(client,conv,'汇总费用')[-1]=={'type':'done'} and executed == [dax]
    assert '经本模型元数据核对的实际类型' in provider.calls[0][0]


def test_repair_rechecks_the_filter_and_only_runs_the_corrected_query(setup, monkeypatch):
    app, client, model, ctx, conv = setup
    ready(app, model, ctx)
    client.post(url(model, '/items'), json=convention())
    failed = FULL.replace('CALCULATE(', 'MissingFunction(')
    provider = Provider([plan(failed), plan(TOTAL), plan(FULL), '修复后取得金额。[Q1]'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    executed = []
    def execute(_, dax):
        executed.append(dax)
        return result(ok=dax == FULL)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', execute)
    assert send(client, conv, '汇总费用')[-1]['type'] == 'done'
    assert executed == [failed, FULL]
    assert [r['ok'] for r in client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]['analysis']] == [False, True]


def test_lookup_picks_up_object_convention_and_downgrade_removes_focus(setup):
    app, client, model, ctx, _ = setup
    ready(app, model, ctx)
    body = term(kind='convention', title='金额显示口径', meaning='费用按千元展示', scope='objects',
                scope_object_ids=[object_id('measure', 'Fact', 'Actual Amount')], confirmed=True)
    client.post(url(model, '/items'), json=body)
    service = app.state.semantics
    base = service.context(model['id'], '无关问题')
    hit = {'ok': True, 'context': service.context(model['id'], '[Actual Amount]')}
    facts = ModelPrompt(ctx, base, [hit, hit], focus_conventions=True)
    rule, = facts.conventions.values()
    assert rule['scope_objects'] == [{'kind': 'measure', 'table': 'Fact', 'name': 'Actual Amount'}]
    assert rule['targets'][0]['purpose'] == '费用金额指标'
    assert not convention_issues(parse_plan(plan(TOTAL)), facts.conventions, ctx)
    generic = 'EVALUATE ROW("费用", SUM(Fact[Amount]))'
    assert convention_issues(parse_plan(plan(generic)), facts.conventions, ctx)
    assert not convention_issues(parse_plan(plan(generic, scope='金额显示口径按千元展示')), facts.conventions, ctx)
    assert '费用按千元展示' not in encode(facts.lookups)
    record, = hit['context']['business_dictionary']['conventions']
    stale = {'ok': True, 'context': {'business_dictionary': {'stale': [{'id': record['id'], 'title': record['title']}]}}}
    assert not ModelPrompt(ctx, base, [hit, stale], focus_conventions=True).conventions
    assert record['meaning'] == '费用按千元展示'


def test_direct_and_nested_measure_filters_and_complete_values(setup):
    _, _, _, ctx, _ = setup
    ctx.measures.extend([Measure('限定范围', 'Fact', f'CALCULATE([Actual Amount], {REF} IN {{"0810","0257"}})'),
                         Measure('间接金额', 'Fact', '[限定范围]')])
    rules = {'C1': {'filters': [{'table': 'Dim Cost Center', 'column': 'CC Number', 'values': ['0257', '0810']}]}}
    for dax in (FULL, 'EVALUATE ROW("结果", [间接金额])'):
        assert not convention_issues(parse_plan(plan(dax)), rules, ctx)
    for dax in (FULL.replace('"0257"', '257'), FULL.replace('"0257",', ''), FULL.replace(REF, "'Dim Material'[Material Code]"),
                f'EVALUATE ROW("金额", CALCULATE([Actual Amount], NOT({REF} IN {{"0257","0810"}})))',
                TOTAL + '\n// ' + FULL, 'EVALUATE ROW("间接金额", 125)', FULL.replace('[Actual Amount]', '[间接金额]').replace('"0257"', '"1111"')):
        assert convention_issues(parse_plan(plan(dax)), rules, ctx)[0]['path'] == 'queries[0].dax'


@pytest.mark.parametrize('mode', ['dax', 'mquery'])
def test_dax_receives_focused_conventions_and_m_keeps_existing_dictionary(setup, monkeypatch, mode):
    app, client, model, ctx, conv = setup
    ctx.table_queries['Fact'] = 'let Source = #table({"Amount"}, {}) in Source'
    ready(app, model, ctx)
    client.post(url(model, '/items'), json=convention())
    app.state.db.execute('UPDATE conversations SET mode=?,query_name=? WHERE id=?', (mode, 'Fact', conv['id']))
    provider = Provider(['已核对模型口径。'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    assert send(client, conv, '解释当前口径')[-1]['type'] == 'done'
    system = provider.calls[0][0]
    assert '未指定时取这两个成本中心' in system
    assert (MARKER in system) == (mode == 'dax')
