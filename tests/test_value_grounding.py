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
def test_code_explanation_uses_real_values_instead_of_forcing_a_filter_choice(setup, monkeypatch, mode):
    app, client, model, ctx, conv, state = setup
    app.state.db.execute('UPDATE conversations SET mode=? WHERE id=?', (mode, conv['id']))
    monkeypatch.setattr(app.state.settings, 'provider', lambda: pytest.fail('Explanation already has local evidence'))
    events = send(client, conv, '0257 是什么意思')
    assert events[0]['type'] == 'status' and events[-1]['type'] == 'done'
    assert not any(e['type'] == 'clarification' for e in events)
    text = ''.join(e.get('text', '') for e in events if e['type'] == 'delta')
    assert 'Dim_Plant[Company Code]' in text and '0257' in text
    assert 'Dim_Calendar[周编号]' not in text and '待审阅' in text
    assert not app.state.semantics.bindings(conv['id'])
    assert not app.state.db.all('SELECT * FROM analysis_queries')
    saved = client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['status'] == 'complete' and saved['content'] == text
    assert 'Company Code' in client.get(f"/api/conversations/{conv['id']}/export").text
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
    events = send(client, conv, '0257是什么意思？')
    text = ''.join(e.get('text', '') for e in events)
    assert '未取得' in text and '已验证存在精确取值' not in text


@pytest.mark.parametrize('mode', ['dax', 'analysis'])
def test_unique_exact_mapping_reaches_both_model_prompts_without_becoming_user_confirmed(setup, monkeypatch, mode):
    app, client, model, ctx, conv, state = setup
    app.state.db.execute('UPDATE conversations SET mode=? WHERE id=?', (mode, conv['id']))
    replies = ['按本轮实际取值，0257 出现在公司代码字段。'] if mode == 'dax' else [plan(), '实际结果125。[Q1]']
    provider = Provider(replies)
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a: result())
    events = send(client, conv, '分析0257的金额')
    assert not any(e['type'] == 'clarification' for e in events)
    assert events[-1]['type'] == 'done' and provider.calls
    evidence = provider.calls[0][0].split('本轮编号实际查值证据', 1)[1]
    assert 'Dim_Plant' in evidence and 'Company Code' in evidence and '0257' in evidence
    assert 'Dim_Calendar' not in evidence and '本会话' in evidence
    assert not app.state.semantics.bindings(conv['id'])
    assert all(e['state'] == 'candidate' for e in app.state.semantics.get(model['id'])['entries'])


def test_duplicate_values_only_offer_fields_with_exact_matches(setup, monkeypatch):
    app, client, model, ctx, conv, state = setup
    state['values'][field_ref('Dim_Material', 'Material Code')] = ['0257']
    monkeypatch.setattr(app.state.settings, 'provider', lambda: pytest.fail('Ambiguous code must be clarified'))
    events = send(client, conv, '分析0257的金额')
    card = next(e['clarification'] for e in events if e['type'] == 'clarification')
    options = encode(card['options'])
    assert 'Company Code' in options and 'Material Code' in options
    assert '周编号' not in options and 'CC Number' not in options
    assert len(card['value_evidence']['matches']) == 2


def test_explaining_a_duplicate_code_lists_matches_without_requesting_filter_intent(setup, monkeypatch):
    app, client, model, ctx, conv, state = setup
    state['values'][field_ref('Dim_Material', 'Material Code')] = ['0257']
    monkeypatch.setattr(app.state.settings, 'provider', lambda: pytest.fail('Local explanation'))
    events = send(client, conv, '0257是哪个字段')
    text = ''.join(e.get('text', '') for e in events)
    assert 'Company Code' in text and 'Material Code' in text
    assert not any(e['type'] == 'clarification' for e in events)


@pytest.mark.parametrize('failure', ['error', 'malformed'])
def test_failed_or_missing_lookup_is_never_a_negative_match_or_name_based_recommendation(setup, monkeypatch, failure):
    app, client, model, ctx, conv, state = setup
    state[failure] = True
    monkeypatch.setattr(app.state.settings, 'provider', lambda: pytest.fail('Cannot infer after lookup failure'))
    events = send(client, conv, '分析0257的金额')
    card = next(e['clarification'] for e in events if e['type'] == 'clarification')
    assert card['value_evidence']['errors'] and not card['value_evidence']['complete']
    assert not card['value_evidence']['matches']
    assert not any('周编号' in o['value'] or 'Company Code' in o['value'] for o in card['options'])


def test_no_match_keeps_leading_zeros_and_does_not_equate_0257_with_257(setup):
    app, client, model, ctx, conv, state = setup
    state['values'][field_ref('Dim_Plant', 'Company Code')] = ['0887']
    p = app.state.semantics.get(model['id'])
    evidence = app.state.semantics.values.locate(model['id'], '0257', p['entries'])
    assert not evidence['matches'] and evidence['complete']
    assert evidence['searched_fields'] == 5
    assert 'Amount' not in state['calls'][0][0]
    assert '== "0257"' in state['calls'][0][0]


def test_lookup_is_bounded_and_partial_coverage_does_not_automatically_resolve_a_single_match(setup, monkeypatch):
    app, client, model, ctx, conv, state = setup
    monkeypatch.setattr('backend.values.MAX_LOOKUP_FIELDS', 2)
    monkeypatch.setattr(app.state.settings, 'provider', lambda: pytest.fail('Partial coverage needs confirmation'))
    events = send(client, conv, '分析0257的金额')
    card = next(e['clarification'] for e in events if e['type'] == 'clarification')
    assert card['value_evidence']['searched_fields'] == 2 and not card['value_evidence']['complete']
    assert all(c[1] <= 12 and c[2] <= 5 for c in state['calls'])


def test_conflicting_user_label_is_not_silently_replaced_by_the_only_matching_field(setup, monkeypatch):
    app, client, model, ctx, conv, state = setup
    monkeypatch.setattr(app.state.settings, 'provider', lambda: pytest.fail('Do not silently replace cost center with company'))
    events = send(client, conv, '分析成本中心0257的金额')
    card = next(e['clarification'] for e in events if e['type'] == 'clarification')
    assert 'Company Code' in encode(card['options'])
    assert not app.state.semantics.bindings(conv['id'])


def test_explicit_mapping_still_controls_generation_and_explanation_can_check_it(setup, monkeypatch):
    app, client, model, ctx, conv, state = setup
    app.state.db.execute('UPDATE conversations SET mode=? WHERE id=?', ('dax', conv['id']))
    provider = Provider(['采用用户明确指定的物料字段。'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    events = send(client, conv, '使用 \'Dim_Material\'[Material Code] = "0257"，解释筛选含义')
    assert events[-1]['type'] == 'done' and not state['calls']
    assert app.state.semantics.bindings(conv['id'])[0]['table_name'] == 'Dim_Material'
    monkeypatch.setattr(app.state.settings, 'provider', lambda: pytest.fail('Local explanation'))
    events = send(client, conv, '0257是什么意思')
    assert 'Company Code' in ''.join(e.get('text', '') for e in events)


def test_reviewed_definition_is_used_but_candidate_meaning_is_not_presented_as_fact(setup):
    app, client, model, ctx, conv, state = setup
    app.state.semantics.edit(model['id'], object_id('column', 'Dim_Plant', 'Company Code'),
        SemanticEdit(business_name='公司代码', meaning='这是集团内各法人公司的文本代码，保留前导零。', confirmed=True))
    events = send(client, conv, '解释一下0257')
    assert '集团内各法人公司' in ''.join(e.get('text', '') for e in events)


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
