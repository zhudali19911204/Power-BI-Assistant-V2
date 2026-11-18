import json
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.analysis import readonly_query
from backend.analysis_protocol import PlanFormatError, parse_plan, validate_action
from backend.app import create_app
from backend.database import encode, now
from backend.cancellation import GenerationCancelled
from backend.engine.context.base import Column, Measure, ModelContext
from backend.fields import FieldCheck, field_ref
from backend.field_discovery import cached_check
from backend.scope_checks import ScopeCheck, candidate_probes, read_scope_batch
from tests.test_analysis import Provider, plan, result, send
from tests.test_clarifications import clarify, latest, reply
from tests.test_field_discovery import profile


CC = 'Dim_Cost Center'
CE = 'Dim_Cost Element'
ORG = {'term': '0887', 'aliases': ['887'], 'candidates': [
    {'table': CC, 'column': 'CC Number'}, {'table': CC, 'column': 'Legal Entity'}]}
CATEGORY = {'term': 'overhead', 'candidates': [{'table': CE, 'column': 'Class 3I'}]}
BUSINESS_DAX = '''EVALUATE SUMMARIZECOLUMNS(TREATAS({"0887"}, 'Dim_Cost Center'[Legal Entity]),
    TREATAS({"Overhead"}, 'Dim_Cost Element'[Class 2]), "Amount", 125)'''


@pytest.fixture
def case(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    cols = {CC: ['CC Number', 'Legal Entity', 'Name', 'City', 'Active', 'CC Cluster Number'],
            CE: ['Class 1', 'Class 2', 'Class 3I', 'CE Category'], 'Dim_Plant': ['Company Code']}
    ctx = ModelContext(tables={t: [Column(n, 'string', t) for n in names] for t, names in cols.items()})
    model = app.state.powerbi.save('test report', 'desktop', 50100, ctx)
    semantic = app.state.semantics.ensure(model, ctx)
    semantic.update(status='ready', coverage=len(semantic['entries']))
    app.state.semantics._store(model['id'], semantic)
    s = SimpleNamespace(app=app, ctx=ctx, model=model, live=[], reads=[], amounts=[],
        values={field_ref(CC, 'CC Number'): ['887'], field_ref(CC, 'Legal Entity'): ['0887'],
                field_ref(CE, 'Class 2'): ['Overhead']}, transform=lambda rows: rows)
    def live(*args, **kwargs):
        s.live.append(1)
        return model, ctx
    class Session:
        def __init__(self, *args):pass
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def query(self, dax, max_rows, timeout_seconds):
            assert readonly_query(dax) == dax
            s.reads.append(dax)
            probes = re.findall(r'FILTER\(DISTINCT\((.*?)\), .*? == "((?:[^"]|"")*)"\) RETURN ROW\("__index", (\d+)', dax)
            assert probes and max_rows == len(probes) + 1
            rows = []
            for ref, text, i in reversed(probes):
                actual = next((v for v in s.values.get(ref, []) if v.casefold()==text.replace('""','"').casefold()), None)
                rows.append({'[__index]': int(i), '[__match]': int(actual is not None), '[__value]': actual})
            return s.transform(rows)
    monkeypatch.setattr(app.state.powerbi, 'require_live', live)
    monkeypatch.setattr('backend.scope_checks._LiveSession', Session)
    monkeypatch.setattr('backend.scope_checks.find_instances', lambda: [])
    monkeypatch.setattr('backend.scope_checks.find_adomd_dll', lambda instances: 'test')
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda model, dax: (s.amounts.append(dax), result())[1])
    with TestClient(app) as client:
        s.client = client
        s.conv = client.post('/api/conversations', json={'model_id': model['id'], 'mode': 'analysis'}).json()
        def run(replies, text='分析 0887 的 overhead 实际与最新预测差异'):
            s.provider = Provider(replies)
            monkeypatch.setattr(app.state.settings, 'provider', lambda: s.provider)
            return send(client, s.conv, text)
        s.run = run
        yield s


def test_both_terms_found_in_one_engine_read_and_two_plans(case):
    s = case
    events = s.run([encode({'scope_checks': [ORG, CATEGORY], 'semantic_queries': ['最新预测版本规则']}),
                    plan(BUSINESS_DAX), '125 [Q1]'])
    assert events[-1]['type'] == 'done' and s.amounts == [BUSINESS_DAX]
    assert len(s.reads) == 1 and len(s.live) == 2  # Chat entry, then one verification for the batch.
    assert len(s.provider.calls) == 3  # Two plans plus the final summary.
    assert '语义层补充检索' in s.provider.calls[1][0] and 'scope_evidence' in s.provider.calls[1][1][-1].content
    records = latest(s.client, s.conv)['field_previews']
    assert len(records) == 5 and {r['round'] for r in records} == {1}
    adopted = {(r['table'], r['column'], r['adopted_value']) for r in records if 'adopted_value' in r}
    assert adopted == {(CC, 'Legal Entity', '0887'), (CE, 'Class 2', 'Overhead')}
    assert '== "887"' not in s.reads[0] and 'CONTAINSSTRING' not in s.reads[0]
    assert sum(len(r['profile']['dax_queries']) for r in records) == 1
    assert not s.app.state.semantics.bindings(s.conv['id'])
    export = s.client.get(f"/api/conversations/{s.conv['id']}/export")
    assert export.status_code == 200 and '候选字段未命中' in export.text and 'Overhead' in export.text
    assert export.text.count(s.reads[0]) == 1
    assert cached_check(FieldCheck(table=CE, column='Class 2', search_terms=['overhead']), records) is None


def test_conflicting_hits_ask_only_about_the_organization_then_resume(case, monkeypatch):
    s = case
    s.values[field_ref(CC, 'CC Number')].append('0887')
    s.run([encode({'scope_checks': [ORG, CATEGORY]})])
    assert not s.amounts and len(s.reads)==1 and len(s.provider.calls)==1
    msg = latest(s.client, s.conv)
    card = msg['clarification']
    assert '0887' in card['question'] and 'overhead' not in card['question'].lower()
    assert [o['label'] for o in card['options']][:2] == ['CC Number · 0887','Legal Entity · 0887']
    assert 'Class 2' in card['context'] and 'Overhead' in card['context']
    provider = Provider([plan(BUSINESS_DAX), '125 [Q1]'])
    monkeypatch.setattr(s.app.state.settings,'provider',lambda:provider)
    assert reply(s.client,s.conv,msg,option='B').status_code==200 and s.amounts==[BUSINESS_DAX]
    assert 'Legal Entity' in provider.calls[0][1][-1].content
    assert s.app.state.semantics.bindings(s.conv['id']) == [
        {'literal':'0887','table_name':CC,'column_name':'Legal Entity'}]
    prior=s.client.get(f"/api/conversations/{s.conv['id']}/messages").json()[1]
    assert [r['column'] for r in prior['field_previews'] if r.get('confirmed_value')=='0887']==['Legal Entity']


def test_0257_card_follows_the_eight_field_results_without_another_model_call(case):
    s = case
    s.values[field_ref(CC, 'Legal Entity')].append('0257')
    s.values[field_ref('Dim_Plant', 'Company Code')] = ['0257']
    org = {'term': '0257', 'candidates': [{'table': t, 'column': c} for t, c in
           [(CC, 'CC Number'), (CC, 'Legal Entity'), ('Dim_Plant', 'Company Code'), (CC, 'CC Cluster Number')]]}
    overhead = {'term': 'overhead', 'candidates': [{'table': CE, 'column': c}
                for c in ['Class 1', 'Class 2', 'CE Category', 'Class 3I']]}
    events = s.run([encode({'scope_checks': [org, overhead]})], text='2026 年 0257的overhead和去年对比是什么情况')
    kinds = [e['type'] for e in events]
    assert kinds[kinds.index('field_previews') + 1] == 'clarification' and kinds[-1] == 'done'
    assert len(s.provider.calls) == len(s.reads) == 1 and not s.amounts
    saved = latest(s.client, s.conv)
    assert len(saved['field_previews']) == 8
    card = saved['clarification']
    assert '0257' in card['question'] and 'overhead' not in card['question'].lower()
    assert {c['field']: c['column'] for c in card['scope_binding']['choices'].values()} == {
        'F3': 'Legal Entity', 'F5': 'Company Code'}
    assert 'Class 2' in card['context'] and 'Overhead' in card['context']


@pytest.mark.parametrize('source', ['user', 'alias', 'binding', 'dictionary', 'description', 'measure'])
def test_multiple_exact_hits_still_use_existing_evidence_to_resolve_scope(case, source):
    s = case
    s.values[field_ref(CC, 'CC Number')].append('0887')
    question = '分析 0887 的 overhead'
    dax = BUSINESS_DAX
    dictionary = s.app.state.semantics.get(s.model['id'])
    entry = next(e for e in dictionary['entries'] if e['kind'] == 'column' and e['name'] == 'Legal Entity')
    basis = ''
    if source == 'user':
        question = '分析 Legal Entity 0887 的 overhead'
    elif source == 'alias':
        entry.update(state='confirmed', aliases=['法人编号'])
        question = '分析法人编号 0887 的 overhead'
    elif source == 'binding':
        s.app.state.db.execute('INSERT INTO conversation_bindings VALUES (?,?,?,?,?)',
                               (s.conv['id'], '0887', CC, 'Legal Entity', now()))
    elif source == 'dictionary':
        dictionary['business_dictionary']['items'] = [{'id': 'org', 'kind': 'term', 'scope': 'model',
            'state': 'confirmed', 'title': '0887', 'aliases': [], 'topic': '', 'targets': [], 'scope_object_ids': [],
            'filters': [{'object_id': entry['id'], 'operator': 'eq', 'values': ['0887']}]}]
    elif source == 'description':
        basis = '运营口径中 0887 指本字段的法人实体'
        entry['facts']['description'] = basis
    else:
        measure = Measure('运营金额', CC, f'CALCULATE([Amount], {field_ref(CC, "Legal Entity")} = "0887")')
        s.ctx.measures.append(measure)
        dax = 'EVALUATE ROW("Amount", [运营金额])'
    s.app.state.semantics._store(s.model['id'], dictionary)
    query = json.loads(plan(dax))
    if basis:
        query['scope_resolutions'] = [{'field': 'F2', 'value': '0887', 'basis': basis}]
    s.run([encode({'scope_checks': [ORG]}), encode(query), '125 [Q1]'], text=question)
    assert s.amounts == [dax] and not latest(s.client, s.conv)['clarification']
    assert any(r.get('adopted_value') == '0887' and r['column'] == 'Legal Entity'
               for r in latest(s.client, s.conv)['field_previews'])


def test_ambiguous_followup_releases_the_card_with_earlier_query_results(case):
    s = case
    s.values[field_ref(CC, 'CC Number')].append('0887')
    first = json.loads(plan())
    first['needs_more'] = True
    events = s.run([encode(first), encode({'scope_checks': [ORG, CATEGORY]})])
    msg = latest(s.client, s.conv)
    assert events[-1]['type'] == 'done' and len(s.provider.calls) == 2
    assert '0887' in msg['clarification']['question'] and '[Q1]' in msg['content']
    assert len(msg['analysis']) == len(s.amounts) == 1 and msg['analysis'][0]['ok']


def test_immediate_card_only_asks_about_the_unresolved_term(case):
    s = case
    s.values[field_ref(CC, 'CC Number')].append('0887')
    s.values[field_ref(CE, 'Class 1')] = ['Overhead']
    s.run([encode({'scope_checks': [ORG, CATEGORY]})], text='对比 Legal Entity 0887 的 overhead')
    card = latest(s.client, s.conv)['clarification']
    assert len(s.provider.calls) == 1 and not s.amounts
    assert 'overhead' in card['question'] and '0887' not in card['question']
    assert {c['column'] for c in card['scope_binding']['choices'].values()} == {'Class 1', 'Class 2'}


@pytest.mark.parametrize('situation', ['alias', 'partial', 'inferred'])
def test_scope_checks_requiring_interpretation_keep_the_planning_step(case, situation):
    s = case
    s.values[field_ref(CE, 'Class 1')] = ['Overhead']
    category = CATEGORY
    text = '分析 overhead'
    if situation == 'alias':
        category = {**CATEGORY, 'term': '场地费用', 'aliases': ['overhead']}
        text = '分析场地费用'
    elif situation == 'partial':
        # Class 3I failed, two other columns genuinely matched.
        s.transform = lambda rows: [row for row in rows if row['[__index]'] != 0]
    else:
        text = '分析费用'
    events = s.run([encode({'scope_checks': [category]}), clarify(COMPARISON_CARD)], text=text)
    assert len(s.provider.calls) == 2 and len(s.reads) == 1 and events[-1]['type'] == 'done'
    assert latest(s.client, s.conv)['clarification']['topic'] == COMPARISON_CARD['topic']


ACTIVE_CHECK = {'table': CC, 'column': 'Active', 'search_terms': ['Yes', 'Y', 'Active']}
ACTIVE_DAX = BUSINESS_DAX.replace('"Amount",', '''FILTER(ALL('Dim_Cost Center'[Active]), 'Dim_Cost Center'[Active] = "Yes"), "Amount",''')
COMPARISON_CARD = {'topic': '对比方式', 'question': '选择哪种对比方式？',
                   'options': [{'label': '同比', 'value': '与上年同期对比'}, {'label': '环比', 'value': '与上期对比'}]}


def active_preview(monkeypatch, case, **overrides):
    calls = []
    def preview(model_id, body, *, search_terms, automatic):
        calls.append((body.column, search_terms))
        return {**profile(), 'table': body.table, 'column': body.column, 'search_terms': search_terms,
                'values': [{'value': 'Yes', 'count': 5}], 'samples': [], 'checked_at': now(), **overrides}
    monkeypatch.setattr(case.app.state.chat.analysis.fields, 'preview', preview)
    return calls


def test_clarification_resumes_with_verified_active_filter_and_still_asks_about_0887(case, monkeypatch):
    s = case
    reads = active_preview(monkeypatch, s)
    s.values[field_ref(CC, 'CC Number')].append('0887')
    s.run([encode({'field_checks': [ACTIVE_CHECK]}), encode({'scope_checks': [ORG, CATEGORY]})])
    first = latest(s.client, s.conv)
    assert '0887' in first['clarification']['question'] and 'Active' not in first['clarification']['question']
    assert first['field_previews'][0]['column'] == 'Active' and first['field_previews'][0]['profile']['values'][0]['value'] == 'Yes'

    resumed = Provider([plan(ACTIVE_DAX), '125 [Q1]'])
    monkeypatch.setattr(s.app.state.settings, 'provider', lambda: resumed)
    response = reply(s.client, s.conv, first, option='B')
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]['type'] == 'done' and s.amounts == [ACTIVE_DAX]
    assert reads == [('Active', ['Yes', 'Y', 'Active'])] and len(s.reads) == 1
    assert len(resumed.calls) == 2 and any('沿用口径确认前' in e.get('text', '') for e in events)
    assert any('"Active"' in m.content and '"Yes"' in m.content for m in resumed.calls[0][1])
    assert not latest(s.client, s.conv)['field_previews'] and not any(e['type'] == 'field_previews' for e in events)
    assert s.app.state.semantics.bindings(s.conv['id'])[0]['column_name'] == 'Legal Entity'


@pytest.mark.parametrize('request_profile', [False, True])
def test_exact_probe_also_covers_a_resumed_equality_but_not_a_column_profile(case, monkeypatch, request_profile):
    s = case
    reads = active_preview(monkeypatch, s)
    s.values[field_ref(CC, 'CC Number')].append('0887')
    s.values[field_ref(CC, 'Active')] = ['Yes']
    active = {'term': 'Yes', 'candidates': [{'table': CC, 'column': 'Active'}]}
    s.run([encode({'scope_checks': [ORG, active]}), plan(ACTIVE_DAX)])
    first = latest(s.client, s.conv)
    assert first['clarification'] and not reads
    profile_request = encode({'field_checks': [{'table': CC, 'column': 'Active'}]})
    resumed = Provider(([profile_request] if request_profile else []) + [plan(ACTIVE_DAX), '125 [Q1]'])
    monkeypatch.setattr(s.app.state.settings, 'provider', lambda: resumed)
    reply(s.client, s.conv, first, option='B')
    assert reads == ([('Active', [])] if request_profile else [])
    assert len(s.reads) == 1 and s.amounts == [ACTIVE_DAX]
    assert len(latest(s.client, s.conv)['field_previews']) == int(request_profile)


@pytest.mark.parametrize('change', ['new_value', 'different_column', 'truncated', 'miss', 'engine', 'expired', 'new_question'])
def test_uncovered_filter_still_reads_desktop_after_clarification(case, monkeypatch, change):
    s = case
    overrides = {'truncated': True} if change == 'truncated' else {}
    if change == 'miss':
        overrides.update(values=[], samples=[{'value': 'Yes'}])
    reads = active_preview(monkeypatch, s, **overrides)
    s.run([encode({'field_checks': [ACTIVE_CHECK]}), clarify(COMPARISON_CARD)])
    first = latest(s.client, s.conv)
    assert first['clarification']['topic'] == '对比方式'
    if change == 'engine':
        s.model['report']['engine_pid'] = 100
    if change == 'expired':
        s.app.state.db.execute('UPDATE field_observations SET created_at=? WHERE message_id=?',
                               ('2000-01-01T00:00:00+00:00', first['id']))
    dax = ACTIVE_DAX.replace('"Yes"', '"No"') if change == 'new_value' else ACTIVE_DAX
    if change == 'different_column':
        dax = dax.replace('[Active]', '[Name]')
    resumed = Provider([plan(dax), plan(dax), '125 [Q1]'])
    monkeypatch.setattr(s.app.state.settings, 'provider', lambda: resumed)
    if change == 'new_question':
        send(s.client, s.conv, '重新分析实际金额')
    else:
        assert reply(s.client, s.conv, first).status_code == 200
    assert len(reads) == 2 and s.amounts == [dax]
    assert len(latest(s.client, s.conv)['field_previews']) == 1


def test_successive_cards_keep_original_evidence_and_covered_field_searches(case, monkeypatch):
    s = case
    reads = active_preview(monkeypatch, s)
    s.run([encode({'field_checks': [ACTIVE_CHECK]}), clarify(COMPARISON_CARD)])
    first = latest(s.client, s.conv)
    second = Provider([clarify({'topic': '期间', 'question': '采用哪个期间？',
                                'options': [{'label': '本期', 'value': '采用本期'}, {'label': '上期', 'value': '采用上期'}]})])
    monkeypatch.setattr(s.app.state.settings, 'provider', lambda: second)
    reply(s.client, s.conv, first)
    next_card = latest(s.client, s.conv)
    resumed = Provider([encode({'field_checks': [{**ACTIVE_CHECK, 'search_terms': ['Yes']}]}), plan(ACTIVE_DAX), '125 [Q1]'])
    monkeypatch.setattr(s.app.state.settings, 'provider', lambda: resumed)
    reply(s.client, s.conv, next_card)
    assert len(reads) == 1 and s.amounts == [ACTIVE_DAX]
    assert 'prior_field_profiles' in resumed.calls[1][1][-1].content and 'matched_distinct_count' in resumed.calls[1][1][-1].content
    assert not latest(s.client, s.conv)['field_previews']


@pytest.mark.parametrize('legacy,latest_profile', [(True, 'same'), (True, 'missing'), (True, 'newer'), (False, 'newer')])
def test_resume_checks_provenance_of_the_original_field_read(case, monkeypatch, legacy, latest_profile):
    s = case
    reads = active_preview(monkeypatch, s)
    s.run([encode({'field_checks': [ACTIVE_CHECK]}), clarify(COMPARISON_CARD)])
    first = latest(s.client, s.conv)
    if legacy:
        # Old cards have no server-side list of source observations.
        s.app.state.db.execute('UPDATE message_clarifications SET payload=? WHERE message_id=?',
                               (encode(first['clarification']), first['id']))
    if latest_profile != 'missing':
        p = dict(first['field_previews'][0]['profile'])
        if latest_profile == 'newer':
            p.update(checked_at=now(), values=[{'value': 'No', 'count': 5}])
        s.app.state.semantics.values.remember(s.model, p)
    reuse = latest_profile == 'same'
    resumed = Provider(([plan(ACTIVE_DAX)] if reuse else [plan(ACTIVE_DAX)] * 2) + ['125 [Q1]'])
    monkeypatch.setattr(s.app.state.settings, 'provider', lambda: resumed)
    reply(s.client, s.conv, first)
    assert s.amounts == [ACTIVE_DAX] and len(reads) == (1 if reuse else 2)


def test_ambiguous_selection_requires_an_existing_source(case):
    s = case
    s.values[field_ref(CC,'CC Number')].append('0887')
    query = json.loads(plan(BUSINESS_DAX))
    query['scope_resolutions'] = [{'field':'F2','value':'0887','basis':'通常应该是法人'}]
    s.run([encode({'scope_checks':[ORG]}), encode(query)], text='分析 CC Number 0887')
    assert len(s.provider.calls)==2 and not s.amounts and latest(s.client,s.conv)['clarification']
    query['scope_resolutions'][0]['basis']='Legal Entity 0887'
    s.run([encode({'scope_checks':[ORG]}),encode(query),'125 [Q1]'],text='分析 Legal Entity 0887')
    assert s.amounts == [BUSINESS_DAX]


def test_ai_requested_field_choice_uses_verified_options(case):
    s=case
    s.values[field_ref(CC,'CC Number')].append('0887')
    ask={'topic':'字段归属','question':'请选择 0887 的归属？','options':[
        {'label':'模型猜测一','value':'猜测一'},{'label':'模型猜测二','value':'猜测二'}]}
    s.run([encode({'scope_checks':[ORG, CATEGORY]}),encode({'clarification':ask})],text='请核对 Legal Entity 0887 的 overhead')
    card=latest(s.client,s.conv)['clarification']
    assert len(s.reads)==1 and len(s.provider.calls)==2 and not s.amounts
    assert len(card['scope_binding']['choices'])==2 and '猜测' not in encode(card)
    assert 'Class 2' in card['context']


def test_targeted_contains_lookup_can_resolve_a_term_after_an_exact_miss(case, monkeypatch):
    s=case
    s.values.clear()
    details=profile()
    details.update(values=[{'value':'Overhead Services','count':1,'value_truncated':False}],
                   search_terms=['overhead'],truncated=False)
    monkeypatch.setattr(s.app.state.chat.analysis.fields,'preview',lambda *a,**k:details)
    fuzzy={'field_checks':[{'table':CE,'column':'Class 2','related_term':'overhead','search_terms':['overhead']}]}
    dax=BUSINESS_DAX.replace('"Overhead"','"Overhead Services"')
    s.run([encode({'scope_checks':[CATEGORY]}),encode(fuzzy),plan(dax),'125 [Q1]'],text='分析 Class 2 的 overhead')
    records=latest(s.client,s.conv)['field_previews']
    assert len(s.reads)==1 and s.amounts==[dax] and records[-1]['adopted_value']=='Overhead Services'


@pytest.mark.parametrize('switch_to_exact', [True, False])
def test_repeated_legacy_misses_get_a_targeted_batch_or_a_single_question(case, monkeypatch, switch_to_exact):
    s=case
    reads=[]
    def miss(model,body,**kwargs):
        reads.append(body.column)
        p=profile()
        p.update(values=[],truncated=False,search_terms=['0887'])
        p['statistics']['matched_distinct_count']=0
        return p
    monkeypatch.setattr(s.app.state.chat.analysis.fields,'preview',miss)
    def search(column):
        return encode({'field_checks':[{'table':CC,'column':column,'search_terms':['0887']}]})
    replies=[search('City'),search('Name'),search('CC Number')]
    replies+=([encode({'scope_checks':[ORG]}),plan(BUSINESS_DAX),'125 [Q1]'] if switch_to_exact else [search('CC Number')])
    s.run(replies)
    assert reads==['City','Name'] and '连续两批未命中' in s.provider.calls[3][1][-1].content
    if switch_to_exact:
        assert len(s.reads)==1 and s.amounts==[BUSINESS_DAX]
    else:
        assert not s.reads and not s.amounts and '0887' in latest(s.client,s.conv)['clarification']['question']


def test_missing_row_is_an_error_and_other_terms_keep_their_real_hits(case):
    s = case
    s.transform=lambda rows: [r for r in rows if r['[__index]']!=0]
    s.run([encode({'scope_checks':[ORG, CATEGORY]}), plan(BUSINESS_DAX)])
    msg=latest(s.client,s.conv)
    assert not s.amounts and msg['clarification'] and '0887' in msg['clarification']['question']
    assert not msg['field_previews'][0]['ok']
    assert any(r['ok'] and r['column']=='Class 2' and r['profile']['values'] for r in msg['field_previews'])


def test_duplicate_miss_stops_without_another_physical_read(case):
    s=case
    by_name={'scope_checks':[{'term':'Unknown','candidates':[{'table':CC,'column':'Name'}]}]}
    s.run([encode(by_name),encode(by_name)])
    assert len(s.reads)==1 and len(s.provider.calls)==2 and not s.amounts
    card=latest(s.client,s.conv)['clarification']
    assert 'Unknown' in card['question'] and '0887' not in encode(card)


@pytest.mark.parametrize('wrong', [BUSINESS_DAX.replace('"0887"','"887"'),
                                   BUSINESS_DAX.replace('[Class 2]','[Class 3I]')])
def test_wrong_known_field_or_changed_code_is_fixed_before_querying(case, wrong):
    s=case
    s.run([encode({'scope_checks':[ORG, CATEGORY]}),plan(wrong),plan(BUSINESS_DAX),'125 [Q1]'])
    assert s.amounts==[BUSINESS_DAX] and len(s.reads)==1
    assert '筛选不一致' in s.provider.calls[2][1][-1].content


def test_a_confirmed_report_filter_adds_the_correct_field_and_actual_spelling(case):
    s=case
    dictionary=s.app.state.semantics.get(s.model['id'])
    target=next(e['id'] for e in dictionary['entries'] if e['name']=='Class 2' and e['kind']=='column')
    dictionary['business_dictionary']['items']=[{'id':'meaning','kind':'term','scope':'model','title':'间接费',
        'state':'confirmed','aliases':[],'topic':'','targets':[],'scope_object_ids':[],
        'filters':[{'object_id':target,'operator':'eq','values':['Overhead']}]}]
    s.app.state.semantics._store(s.model['id'],dictionary)
    s.run([encode({'scope_checks':[{'term':'间接费','candidates':[{'table':CC,'column':'Name'}]}]}),
           plan(BUSINESS_DAX),'125 [Q1]'],text='分析间接费')
    records=latest(s.client,s.conv)['field_previews']
    assert s.amounts==[BUSINESS_DAX] and records[0]['column']=='Class 2' and records[0]['adopted_value']=='Overhead'


def test_two_unproductive_batches_stop_only_the_outstanding_term(case):
    s=case
    probes=[{'term':'unknown','candidates':[{'table':CC,'column':c}]} for c in ('City','Name','CC Number')]
    s.run([encode({'scope_checks':[probes[0], CATEGORY]}),
           encode({'scope_checks':[probes[1]]}),encode({'scope_checks':[probes[2]]})])
    assert len(s.reads)==2 and not s.amounts
    card=latest(s.client,s.conv)['clarification']
    assert 'unknown' in card['question'] and 'Class 2' in card['context']


def test_previous_hits_are_checked_again_and_an_exact_miss_drops_the_hint(case):
    s=case
    s.run([encode({'scope_checks':[CATEGORY]}),plan(BUSINESS_DAX),'125 [Q1]'])
    evidence=s.app.state.semantics.values
    assert evidence.preview_hints(s.model,'overhead')=={(CE,'Class 2')}
    assert not evidence.preview_hints({**s.model,'fingerprint':'changed'},'overhead')
    s.values.clear()
    attempt={'scope_checks':[{'term':'overhead','candidates':[{'table':CC,'column':'Name'}]}]}
    s.run([encode(attempt),encode(attempt)])
    assert len(s.reads)==2 and field_ref(CE,'Class 2') in s.reads[-1]
    assert not evidence.preview_hints(s.model,'overhead') and latest(s.client,s.conv)['clarification']


def test_explicit_scope_can_produce_an_empty_result(case):
    s=case
    s.values.clear()
    s.run([encode({'scope_checks':[ORG]}),plan(BUSINESS_DAX),'本次筛选无匹配结果。[Q1]'],text='检查 Legal Entity 0887')
    assert s.amounts==[BUSINESS_DAX] and not latest(s.client,s.conv)['clarification']
    assert all(not r['profile']['values'] and 'adopted_value' not in r for r in latest(s.client,s.conv)['field_previews'])


@pytest.mark.parametrize('error', [GenerationCancelled(), HTTPException(504,'等待查询超时')])
def test_cancel_or_timeout_stops_the_batch_without_treating_failure_as_an_empty_field(case,monkeypatch,error):
    s=case
    def fail(*args):raise error
    monkeypatch.setattr('backend.analysis.read_scope_batch',fail)
    events=s.run([encode({'scope_checks':[ORG, CATEGORY]})])
    assert not s.amounts and len(s.provider.calls)==1
    msg=latest(s.client,s.conv)
    if isinstance(error,GenerationCancelled):
        assert msg['status']=='cancelled' and not msg['field_previews'] and not msg['clarification']
    else:
        assert events[-1]['type']=='done' and '超时' in msg['clarification']['context']
        assert all(not r['ok'] and r['profile'] is None for r in msg['field_previews'])


def test_fair_global_budget_and_skipped_fields_remain_unverified(case):
    s=case
    candidates=[{'table':CC,'column':c} for c in ('Name','Legal Entity','City','CC Number')]
    groups=[{'term':term,'candidates':candidates} for term in ('A','B','C','D')]
    s.run([encode({'scope_checks':groups}),plan(BUSINESS_DAX)])
    records=latest(s.client,s.conv)['field_previews']
    assert len(records)==12 and len(s.reads)==1
    assert {r['scope_term'] for r in records}=={'A','B','C','D'}
    assert all(sum(r['column']==field for r in records)<=3 for field in ('Name','Legal Entity','City','CC Number'))
    feedback=json.loads(s.provider.calls[1][1][-1].content.split('\n',1)[1])
    assert any(g['incomplete'] for g in feedback['scope_evidence'])


def test_unknown_field_does_not_break_other_probes_and_literals_are_escaped(case):
    s=case
    term='a"*?'
    s.values[field_ref(CC,'Name')]=[term]
    g=ScopeCheck(term=term,candidates=[{'table':CC,'column':'Name'},{'table':'missing','column':'bad'}])
    results,dax=read_scope_batch(s.app.state.powerbi,s.model['id'],candidate_probes([g],s.ctx,[],{}))
    assert '== "a""*?"' in dax and 'missing' not in dax
    assert results[0]['profile']['values']==[{'value':term}] and not results[1]['ok']


@pytest.mark.parametrize('data', [
    {'scope_checks':[{'term':' ','candidates':[]}]}, {'scope_checks':[ORG]*5},
    {'scope_checks':[ORG],'queries':json.loads(plan())['queries']},
    {'scope_resolutions':[{'field':'F1','value':'x','basis':'x'}]},
])
def test_protocol_rejects_invalid_scope_actions(data):
    with pytest.raises(PlanFormatError):validate_action(parse_plan(encode(data)),'analysis_plan')
