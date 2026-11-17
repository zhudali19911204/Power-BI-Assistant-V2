import json
import re

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import encode
from backend.engine.context.base import Column, ModelContext
from backend.fields import FieldPreviewInput, field_ref
from backend.semantics import SemanticEdit, object_id
from backend.values import exact_value_query
from tests.test_analysis import Provider, plan, result


@pytest.fixture
def setup(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    ctx = ModelContext(tables={
        'Dim_Calendar': [Column('周编号', 'string', 'Dim_Calendar')],
        'Dim_Plant': [Column('Company Code', 'string', 'Dim_Plant')],
        'Dim_Cost Center': [Column('CC Number', 'string', 'Dim_Cost Center')],
        'Dim_Material': [Column('Material Code', 'string', 'Dim_Material')],
        'Fact': [Column('Amount', 'double', 'Fact'), Column('Other', 'unknown', 'Fact')],
    })
    model = app.state.powerbi.save('值证据测试', 'desktop', 50001, ctx)
    state = {'error': False, 'malformed': False, 'connected': True, 'values': {
        field_ref('Dim_Calendar', '周编号'): ['202645'],
        field_ref('Dim_Plant', 'Company Code'): ['0257', '0887'],
        field_ref('Dim_Cost Center', 'CC Number'): ['0001'],
        field_ref('Dim_Material', 'Material Code'): ['RM1'],
        field_ref('Fact', 'Other'): [257],
    }, 'calls': []}

    def live(*a, **k):
        if not state['connected']:
            raise HTTPException(409, 'Desktop 连接已断开')
        return model, ctx

    monkeypatch.setattr(app.state.powerbi, 'require_live', live)
    monkeypatch.setattr('backend.values.find_instances', lambda: [])
    monkeypatch.setattr('backend.values.find_adomd_dll', lambda *a: 'fake')
    monkeypatch.setattr('backend.fields.find_instances', lambda: [])
    monkeypatch.setattr('backend.fields.find_adomd_dll', lambda *a: 'fake')

    class Session:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def query(self, dax, max_rows, timeout_seconds):
            state['calls'].append((dax, max_rows, timeout_seconds))
            if state['error']: raise RuntimeError('simulated query failure')
            if state['malformed']: return []
            if '"row_count"' in dax:
                return [{'[row_count]': 2, '[distinct_count]': 2, '[blank_count]': 0,
                         '[empty_string_count]': 0, '[matched_distinct_count]': 2}]
            if 'SUMMARIZECOLUMNS' in dax:
                return [{'[__value]': v, '[__count]': 1} for v in ['0257', '0887']][:max_rows]
            literal = re.search(r'== "([0-9]+)"', dax)[1]
            fields = re.findall(r'ROW\("__index", (\d+), "__match", COALESCE\(COUNTROWS\(FILTER\(DISTINCT\((.*?)\),', dax)
            return [{'[__index]': int(i), '[__match]': int(literal in [str(v) for v in state['values'].get(ref, [])])}
                    for i, ref in fields]

    monkeypatch.setattr('backend.values._LiveSession', Session)
    monkeypatch.setattr('backend.fields._LiveSession', Session)
    p = app.state.semantics.ensure(model, ctx)
    p.update(status='ready', coverage=len(p['entries']))
    for e in p['entries']:
        e['state'] = 'candidate'
        if e['name'] == 'Company Code': e['business_name'] = '公司代码'
        if e['name'] == 'CC Number': e['business_name'] = '成本中心编号'
    app.state.semantics._store(model['id'], p)
    with TestClient(app) as client:
        conv = client.post('/api/conversations', json={'model_id': model['id'], 'mode': 'analysis'}).json()
        yield app, client, model, ctx, conv, state


def send(client, conv, content):
    response = client.post(f"/api/conversations/{conv['id']}/messages", json={'content': content})
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines()]


@pytest.mark.parametrize('mode', ['dax', 'analysis'])
def test_code_question_is_interpreted_by_ai_without_automatic_value_scan(setup, monkeypatch, mode):
    app, client, model, ctx, conv, state = setup
    app.state.db.execute('UPDATE conversations SET mode=? WHERE id=?', (mode, conv['id']))
    provider = Provider(['0257 的含义需结合模型语义。'] if mode == 'dax' else [plan(), '实际结果125。[Q1]'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a: result())
    events = send(client, conv, '0257 是什么意思')
    assert events[0]['type'] == 'status' and events[-1]['type'] == 'done'
    assert not any(e['type'] == 'clarification' for e in events)
    text = ''.join(e.get('text', '') for e in events if e['type'] == 'delta')
    assert provider.calls and '0257' in provider.calls[0][1][-1].content
    assert not state['calls']
    assert not app.state.semantics.bindings(conv['id'])
    saved = client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['status'] == 'complete' and saved['content'] == text
    assert not app.state.chat.generation_status(conv['id'])['running']


def test_preview_evidence_survives_restart_but_is_only_a_hint_until_live_checked(setup):
    app, client, model, ctx, conv, state = setup
    p = app.state.chat.analysis.fields.preview(model['id'], FieldPreviewInput(table='Dim_Plant', column='Company Code'))
    restarted = create_app(app.state.db.path.parent)
    assert restarted.state.semantics.values.preview_hints(model, '0257') == {('Dim_Plant', 'Company Code')}
    assert not restarted.state.semantics.values.preview_hints({**model, 'fingerprint': 'different'}, '0257')
    assert not app.state.db.all('SELECT * FROM messages')
    assert p['run_verified']
    state['values'][field_ref('Dim_Plant', 'Company Code')] = ['0887']
    evidence = app.state.semantics.values.locate(model['id'], '0257', app.state.semantics.get(model['id'])['entries'])
    assert not evidence['matches'] and evidence['complete']


@pytest.mark.parametrize('mode', ['dax', 'analysis'])
def test_saved_value_evidence_is_labeled_historical_and_does_not_become_user_confirmed(setup, monkeypatch, mode):
    app, client, model, ctx, conv, state = setup
    app.state.db.execute('UPDATE conversations SET mode=? WHERE id=?', (mode, conv['id']))
    app.state.semantics.values.locate(model['id'], '0257', app.state.semantics.get(model['id'])['entries'])
    state['calls'].clear()
    replies = ['按历史查值，0257 曾出现在公司代码字段。'] if mode == 'dax' else [plan(), '实际结果125。[Q1]']
    provider = Provider(replies)
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a: result())
    events = send(client, conv, '分析0257的金额')
    assert not any(e['type'] == 'clarification' for e in events)
    assert events[-1]['type'] == 'done' and provider.calls
    evidence = provider.calls[0][0].split('历史编号查值线索', 1)[1]
    assert 'Dim_Plant' in evidence and 'Company Code' in evidence and '0257' in evidence
    assert 'Dim_Calendar' not in evidence and '本会话' in evidence
    assert '不是本轮重新查值' in evidence and 'checked_at' in evidence and not state['calls']
    assert not app.state.semantics.bindings(conv['id'])
    assert all(e['state'] == 'candidate' for e in app.state.semantics.get(model['id'])['entries'])


def test_duplicate_cached_values_do_not_force_a_question_before_ai_interpretation(setup, monkeypatch):
    app, client, model, ctx, conv, state = setup
    state['values'][field_ref('Dim_Material', 'Material Code')] = ['0257']
    app.state.semantics.values.locate(model['id'], '0257', app.state.semantics.get(model['id'])['entries'])
    state['calls'].clear()
    provider = Provider([plan(scope='依据用户问题和公司代码定义采用公司范围'), '总额125。[Q1]'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a: result())
    events = send(client, conv, '分析0257的金额')
    evidence = provider.calls[0][0].split('历史编号查值线索', 1)[1]
    assert 'Company Code' in evidence and 'Material Code' in evidence
    assert not any(e['type'] == 'clarification' for e in events)
    assert not state['calls'] and events[-1]['type'] == 'done'


@pytest.mark.parametrize('failure', ['error', 'malformed'])
def test_failed_or_missing_lookup_is_never_a_negative_match_or_name_based_recommendation(setup, monkeypatch, failure):
    app, client, model, ctx, conv, state = setup
    state[failure] = True
    evidence = app.state.semantics.values.locate(model['id'], '0257', app.state.semantics.get(model['id'])['entries'])
    assert evidence['errors'] and not evidence['complete'] and not evidence['matches']
    assert '没有匹配不代表整个模型不存在' in app.state.semantics.value_context(conv, '0257')


def test_no_match_keeps_leading_zeros_and_does_not_equate_0257_with_257(setup):
    app, client, model, ctx, conv, state = setup
    state['values'][field_ref('Dim_Plant', 'Company Code')] = ['0887']
    p = app.state.semantics.get(model['id'])
    evidence = app.state.semantics.values.locate(model['id'], '0257', p['entries'])
    assert not evidence['matches'] and evidence['complete']
    assert evidence['searched_fields'] == 5
    assert 'Amount' not in state['calls'][0][0]
    assert '== "0257"' in state['calls'][0][0]


def test_legacy_lookup_utility_is_bounded_and_reports_partial_coverage(setup, monkeypatch):
    app, client, model, ctx, conv, state = setup
    monkeypatch.setattr('backend.values.MAX_LOOKUP_FIELDS', 2)
    evidence = app.state.semantics.values.locate(model['id'], '0257', app.state.semantics.get(model['id'])['entries'])
    assert evidence['searched_fields'] == 2 and not evidence['complete']
    assert all(c[1] <= 12 and c[2] <= 5 for c in state['calls'])


def test_conflicting_user_label_is_not_silently_replaced_by_the_only_matching_field(setup, monkeypatch):
    app, client, model, ctx, conv, state = setup
    from tests.test_clarifications import clarify
    provider = Provider([clarify({'topic':'编号口径','question':'0257 按哪种范围分析？','options':[
        {'label':'成本中心','value':'按成本中心0257分析，补充对应定义'},
        {'label':'公司','value':'按公司0257分析'}]})])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    events = send(client, conv, '分析成本中心0257的金额')
    card = next(e['clarification'] for e in events if e['type'] == 'clarification')
    assert '成本中心0257' in encode(card['options']) and '按公司0257' in encode(card['options'])
    assert '成本中心0257' in provider.calls[0][1][-1].content
    assert not app.state.semantics.bindings(conv['id'])


def test_explicit_mapping_is_preserved_in_user_history_without_global_confirmation(setup, monkeypatch):
    app, client, model, ctx, conv, state = setup
    app.state.db.execute('UPDATE conversations SET mode=? WHERE id=?', ('dax', conv['id']))
    provider = Provider(['采用用户明确指定的物料字段。', '沿用用户指定的物料字段解释0257。'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    events = send(client, conv, '使用 \'Dim_Material\'[Material Code] = "0257"，解释筛选含义')
    assert events[-1]['type'] == 'done' and not state['calls']
    assert not app.state.semantics.bindings(conv['id'])
    events = send(client, conv, '0257是什么意思')
    assert any('Material Code' in m.content for m in provider.calls[1][1] if m.role == 'user')
    assert not state['calls']


def test_reviewed_definition_and_candidate_limits_are_supplied_to_ai(setup, monkeypatch):
    app, client, model, ctx, conv, state = setup
    app.state.semantics.edit(model['id'], object_id('column', 'Dim_Plant', 'Company Code'),
        SemanticEdit(business_name='公司代码', meaning='这是集团内各法人公司的文本代码，保留前导零。', confirmed=True))
    app.state.db.execute('UPDATE conversations SET mode=? WHERE id=?', ('dax', conv['id']))
    provider = Provider(['按已审阅的公司代码定义解释0257。'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    events = send(client, conv, '解释一下公司代码0257')
    assert events[-1]['type'] == 'done'
    assert '集团内各法人公司' in provider.calls[0][0] and 'candidate 仅是待确认' in provider.calls[0][0]


def test_actual_datatype_skips_numeric_columns_even_when_structural_type_is_unknown(setup):
    app, client, model, ctx, conv, state = setup
    p = app.state.semantics.get(model['id'])
    e = next(e for e in p['entries'] if e['name'] == 'Other')
    e['facts']['actual_dtype'] = 'int64'
    evidence = app.state.semantics.values.locate(model['id'], '0257', p['entries'])
    assert evidence['searched_fields'] == 4 and 'Other' not in state['calls'][0][0]


def test_lookup_templates_escape_identifiers_and_remain_readonly():
    from backend.analysis import readonly_query
    query = exact_value_query([("O'Brien", 'x]y'), ('Table', 'Code')], '0257')
    assert readonly_query(query) == query
    assert "'O''Brien'[x]]y]" in query
    assert '== "0257"' in query and ' & ""' in query


def test_stale_fingerprint_cannot_supply_value_evidence_to_another_schema_or_model(setup):
    app, client, model, ctx, conv, state = setup
    p = app.state.semantics.get(model['id'])
    app.state.semantics.values.locate(model['id'], '0257', p['entries'])
    assert app.state.semantics.values.for_prompt(model, ['0257'])
    assert not app.state.semantics.values.for_prompt({**model, 'fingerprint': 'changed'}, ['0257'])
    other = app.state.powerbi.save('其他模型', 'desktop', 50002, ctx)
    assert not app.state.semantics.values.for_prompt(other, ['0257'])


def test_disconnected_engine_blocks_cached_explanations(setup):
    app, client, model, ctx, conv, state = setup
    p = app.state.semantics.get(model['id'])
    app.state.semantics.values.locate(model['id'], '0257', p['entries'])
    state['connected'] = False
    response = client.post(f"/api/conversations/{conv['id']}/messages", json={'content': '0257是什么意思'})
    assert response.status_code == 409
