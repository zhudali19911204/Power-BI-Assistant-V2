import copy
import json
from dataclasses import asdict

import pytest

from backend.database import encode, now, uid
from backend.engine.context.base import Column, Measure, ModelContext, Relationship
from backend.semantic_retrieval import retrieve
from backend.semantics import inventory
from tests.test_analysis import Provider
from tests.test_semantics import setup, ready


def dictionary(ctx):
    entries = inventory(ctx)
    for entry in entries:
        entry.update(state='confirmed', source='用户确认', meaning='已审阅')
    return entries


def find(entries, kind, table, name):
    return next(e for e in entries if (e['kind'], e['table'], e['name']) == (kind, table, name))


def refs(payload):
    return {(e['kind'], e['table'], e['name']) for e in payload['entries']}


def sales_context():
    return ModelContext(
        tables={'Sales': [Column('Amount', 'double', 'Sales'), Column('CompanyKey', 'string', 'Sales'),
                          Column('Date', 'datetime', 'Sales')],
                'Companies': [Column('Key', 'string', 'Companies'), Column('RegionKey', 'string', 'Companies')],
                'Regions': [Column('Key', 'string', 'Regions'), Column('Region', 'string', 'Regions')],
                'Calendar': [Column('Date', 'datetime', 'Calendar'), Column('YearMonth', 'string', 'Calendar')],
                'Measures': [], 'Other': [Column('Unrelated', 'double', 'Other')]},
        measures=[Measure('Base', 'Measures', "SUM('Sales'[Amount])"),
                  Measure('Net', 'Measures', '[Base] * 0.9'),
                  Measure('Profit', 'Other', "SUM('Other'[Unrelated])")],
        relationships=[Relationship('Sales', 'CompanyKey', 'Companies', 'Key'),
                       Relationship('Companies', 'RegionKey', 'Regions', 'Key'),
                       Relationship('Sales', 'Date', 'Calendar', 'Date')],
        date_tables=['Calendar'])


def test_tail_alias_beats_unrelated_confirmed_definitions_over_old_budget():
    ctx = ModelContext(tables={'Archive': [Column(f'Field{i}', 'string', 'Archive') for i in range(150)],
                               'Facts': [Column('Amount', 'double', 'Facts')]},
                       measures=[Measure('Target', 'Facts', "SUM('Facts'[Amount])")])
    entries = dictionary(ctx)
    for entry in entries:
        entry['meaning'] = '无关历史字段说明' * 60
    target = find(entries, 'measure', 'Facts', 'Target')
    target.update(aliases=['人员费用', 'personnel cost'], state='candidate', meaning='待业务确认')
    assert len(encode(entries[:-1])) > 50000
    for question in ('今年人员费用是多少', 'Show PERSONNEL COST'):
        result = retrieve(entries, asdict(ctx), question)
        assert ('measure', 'Facts', 'Target') in refs(result)
        assert ('column', 'Facts', 'Amount') in refs(result)
        assert not any(e['table'] == 'Archive' for e in result['entries'])
        assert find(result['entries'], 'measure', 'Facts', 'Target')['state'] == 'candidate'


def test_retrieves_transitive_dependencies_bridge_keys_and_date_context():
    ctx = sales_context()
    result = retrieve(dictionary(ctx), asdict(ctx), "[Net] 按 'Regions'[Region] 分析")
    assert {('measure', 'Measures', 'Base'), ('column', 'Sales', 'Amount'),
            ('column', 'Sales', 'CompanyKey'), ('column', 'Companies', 'Key'),
            ('column', 'Companies', 'RegionKey'), ('column', 'Regions', 'Key'),
            ('column', 'Calendar', 'YearMonth')} <= refs(result)
    assert ('measure', 'Other', 'Profit') not in refs(result)
    assert result['retrieval']['relationships'] == asdict(ctx)['relationships']
    assert result['retrieval']['omitted_relevant'] == 0


@pytest.mark.parametrize('reference', ["'A'[Code]", 'A[Code]'])
def test_qualified_field_does_not_select_same_name_or_entire_table(reference):
    ctx = ModelContext(tables={'A': [Column('Code', 'string', 'A'), Column('Noise', 'string', 'A')],
                               'B': [Column('Code', 'string', 'B')]})
    result = retrieve(dictionary(ctx), asdict(ctx), f'解释 {reference}')
    assert refs(result) == {('column', 'A', 'Code'), ('table', 'A', 'A')}


def test_short_followup_retains_latest_user_subject_not_older_subject_or_assistant():
    ctx = sales_context()
    history = [{'role': 'user', 'content': '分析 [Profit]'},
               {'role': 'user', 'content': '改看 [Net]'},
               {'role': 'assistant', 'content': '建议采用 [Profit]'}]
    result = retrieve(dictionary(ctx), asdict(ctx), '按月拆开', history=history)
    assert ('measure', 'Measures', 'Net') in refs(result)
    assert ('measure', 'Other', 'Profit') not in refs(result)
    switched = retrieve(dictionary(ctx), asdict(ctx), '改看 [Profit]', history=history)
    assert ('measure', 'Other', 'Profit') in refs(switched)
    assert ('measure', 'Measures', 'Net') not in refs(switched)


def test_description_tokens_recall_without_literal_alias_and_latin_substrings_do_not():
    ctx = ModelContext(tables={'Fact': []}, measures=[Measure('M1', 'Fact', '1'), Measure('ID', 'Fact', '2')])
    entries = dictionary(ctx)
    find(entries, 'measure', 'Fact', 'M1')['meaning'] = '统计退货商品产生的金额'
    result = retrieve(entries, asdict(ctx), '退货金额是多少')
    assert ('measure', 'Fact', 'M1') in refs(result)
    assert result['retrieval']['mode'] == 'question'
    assert retrieve(entries, asdict(ctx), 'paid')['retrieval']['mode'] == 'fallback'


def test_bound_code_pins_real_field_without_promoting_candidate_or_old_definition():
    ctx = ModelContext(tables={'Plant': [Column('CompanyCode', 'string', 'Plant')],
                               'Materials': [Column('Code', 'string', 'Materials')]})
    entries = dictionary(ctx)
    entry = find(entries, 'column', 'Plant', 'CompanyCode')
    entry.update(state='unresolved', meaning='', business_name='', aliases=[],
                 previous_definition={'meaning': '过期口径', 'aliases': ['不再使用的别名']},
                 change={'kind': 'modified', 'reasons': ['公式变化']})
    result = retrieve(entries, asdict(ctx), '0257的情况', bindings=[{
        'literal': '0257', 'table_name': 'Plant', 'column_name': 'CompanyCode'}])
    assert result['retrieval']['mode'] == 'question'
    assert ('column', 'Materials', 'Code') not in refs(result)
    assert ('column', 'Plant', 'CompanyCode') in refs(result)
    assert '过期口径' not in encode(result) and '公式变化' in encode(result)
    assert find(result['entries'], 'column', 'Plant', 'CompanyCode')['state'] == 'unresolved'
    old = retrieve(entries, asdict(ctx), '不再使用的别名')
    assert old['retrieval']['mode'] == 'fallback'


def test_budget_reports_relevant_omissions_and_keeps_smaller_later_entries():
    ctx = sales_context()
    entries = dictionary(ctx)
    find(entries, 'measure', 'Measures', 'Net')['meaning'] = '很长的定义' * 1000
    original = copy.deepcopy(entries)
    result = retrieve(entries, asdict(ctx), '[Net]', max_chars=1300)
    assert result['retrieval']['used_chars'] <= 1300
    assert ('measure', 'Measures', 'Net') not in refs(result)
    assert ('measure', 'Measures', 'Base') in refs(result)
    assert result['retrieval']['omitted_relevant'] > 0
    assert any(e['name'] == 'Net' for e in result['retrieval']['missing'])
    assert entries == original


def test_no_match_fallback_spreads_budget_across_tables():
    ctx = ModelContext(tables={'A': [Column(f'Field{i}', 'string', 'A') for i in range(100)],
                               'B': [Column('Code', 'string', 'B')], 'C': []})
    result = retrieve(dictionary(ctx), asdict(ctx), '完全没有对应的业务说法', max_chars=1000)
    assert result['retrieval']['mode'] == 'fallback'
    assert {('table', t, t) for t in ('A', 'B', 'C')} <= refs(result)
    assert result['retrieval']['omitted_overview'] > 0


def test_dependency_cycles_and_parallel_inactive_relationships_are_preserved():
    ctx = sales_context()
    ctx.measures[0].expression = "[Net] + SUM('Sales'[Amount])"
    ctx.tables['Sales'].append(Column('ShipDate', 'datetime', 'Sales'))
    ctx.relationships.append(Relationship('Sales', 'ShipDate', 'Calendar', 'Date', is_active=False))
    result = retrieve(dictionary(ctx), asdict(ctx), '[Net]')
    assert len(refs(result)) == len(result['entries'])
    assert any(not r['is_active'] for r in result['retrieval']['relationships'])
    assert ('column', 'Sales', 'ShipDate') in refs(result)


def test_exact_alias_ranks_before_verbose_fuzzy_description_and_caps_are_disclosed():
    ctx = ModelContext(tables={'Facts': []}, measures=[Measure(f'Metric{i}', 'Facts', '1') for i in range(40)])
    entries = dictionary(ctx)
    for entry in entries:
        if entry['kind'] == 'measure':
            entry['meaning'] = '月度 年度 分组 地区 商品 客户 费用 收入 退货 销售'
    target = find(entries, 'measure', 'Facts', 'Metric39')
    target.update(aliases=['特殊指标'], state='candidate', meaning='')
    result = retrieve(entries, asdict(ctx), '特殊指标的月度 年度 分组 地区 商品 客户 费用 收入 退货 销售')
    assert result['entries'][0]['name'] == 'Metric39'
    assert result['retrieval']['matched'] == 40
    assert result['retrieval']['omitted_relevant'] == 8
    assert len(result['retrieval']['missing']) == 8


def test_all_equal_bridge_paths_are_included_without_claiming_one_is_correct():
    ctx = ModelContext(tables={t: [Column('Key', 'string', t)] for t in ('Start', 'BridgeA', 'BridgeB', 'End')},
                       relationships=[Relationship('Start', 'Key', bridge, 'Key') for bridge in ('BridgeA', 'BridgeB')]
                       + [Relationship(bridge, 'Key', 'End', 'Key') for bridge in ('BridgeA', 'BridgeB')])
    result = retrieve(dictionary(ctx), asdict(ctx), "解释 'Start'[Key] 和 'End'[Key]")
    assert len(result['retrieval']['relationships']) == 4
    assert all(('column', t, 'Key') in refs(result) for t in ctx.tables)


def test_assistant_only_suggestion_does_not_count_as_a_question_match():
    ctx = sales_context()
    result = retrieve(dictionary(ctx), asdict(ctx), '继续', history=[{'role': 'assistant', 'content': '[Profit]'}])
    assert result['retrieval']['mode'] == 'fallback'
    assert result['retrieval']['matched'] == 0


@pytest.mark.parametrize('mode', ['dax', 'analysis'])
def test_both_generation_paths_pass_current_question_and_user_history(setup, monkeypatch, mode):
    app, client, model, ctx, conv = setup
    p = ready(app, model, ctx)
    find(p['entries'], 'measure', 'Fact', 'Actual Amount')['aliases'] = ['人员费用']
    app.state.semantics._store(model['id'], p)
    app.state.db.execute('UPDATE conversations SET mode=? WHERE id=?', (mode, conv['id']))
    for role, content in [('user', '分析人员费用'), ('assistant', "应该使用 'Dim Material'[Material Code]")]:
        app.state.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
                             (uid(), conv['id'], role, content, 'complete', now()))
    reply = encode({'queries': []}) if mode == 'analysis' else '已参考本轮相关语义。'
    provider = Provider([reply, reply])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    for question, expected, absent in [
        ('按月拆开', ('measure', 'Fact', 'Actual Amount'), ('column', 'Dim Material', 'Material Code')),
        ("解释 'Dim Cost Center'[CC Number]", ('column', 'Dim Cost Center', 'CC Number'),
         ('column', 'Dim Material', 'Material Code')),
    ]:
        response = client.post(f"/api/conversations/{conv['id']}/messages", json={'content': question})
        assert response.status_code == 200
        assert not any(e['type'] == 'error' for e in map(json.loads, response.text.splitlines()))
        tail = provider.calls[-1][0].split('模型语义词典（未确认建议不可视为事实）：\n', 1)[1]
        payload, _ = json.JSONDecoder().raw_decode(tail)
        assert expected in refs(payload) and absent not in refs(payload)
        assert payload['retrieval']['mode'] == 'question'
