from copy import deepcopy
from dataclasses import asdict

from backend.engine.context.base import Column, Measure, ModelContext, Relationship
from backend.model_prompt import ModelPrompt


def entry(kind, table, name, **facts):
    return {'kind': kind, 'table': table, 'name': name, 'state': 'confirmed', **facts}


def selection(entries, relationships=(), **extra):
    return {'entries': entries, 'retrieval': {'relationships': list(relationships), 'omitted_relevant': 1}, **extra}


def test_same_object_gets_one_definition_and_uncovered_structure_survives():
    expression = "VAR ValueNow = SUM('Sales'[Amount])\nRETURN ValueNow - [Forecast]"
    model = ModelContext(
        tables={'Sales': [Column('Amount', 'double', 'Sales', 0), Column('Code', 'auto', 'Sales')],
                'Forecasts': [Column('Amount', 'int64', 'Forecasts')], 'Date': []},
        date_tables=['Date'], calculated_tables={'Date': 'CALENDARAUTO()', 'OnlyCalc': 'ROW("K",1)'},
        measures=[Measure('Amount', 'Sales', expression), Measure('Forecast', 'Forecasts', 'SUM(Forecasts[Amount])')])
    inputs = selection([
        entry('table', 'Date', 'Date'), entry('column', 'Sales', 'Code', actual_dtype='string'),
        entry('column', 'Sales', 'Amount', actual_dtype='decimal', unit='元'),
        entry('measure', 'Sales', 'Amount', meaning='实际与预测差额', state='candidate', expression='old formula'),
        entry('table', 'OnlyCalc', 'OnlyCalc'), entry('column', 'Old', 'Amount', meaning='历史字段')])
    original = deepcopy((model, inputs))
    prompt = ModelPrompt(model, inputs)
    rows = {(e['kind'], e['table'], e['name']): e for e in prompt.semantic['entries']}
    amount = rows['column', 'Sales', 'Amount']
    assert (amount['actual_dtype'], amount['semantic_dtype'], amount['cardinality'], amount['unit']) == ('double', 'decimal', 0, '元')
    assert rows['column', 'Sales', 'Code']['actual_dtype'] == 'string'
    measure = rows['measure', 'Sales', 'Amount']
    assert (measure['expression'], measure['meaning'], measure['state']) == (expression, '实际与预测差额', 'candidate')
    assert rows['table', 'Date', 'Date']['date_table'] and rows['table', 'Date', 'Date']['calculated_table']
    assert rows['table', 'OnlyCalc', 'OnlyCalc']['calculated_table'] and rows['column', 'Old', 'Amount']['in_snapshot'] is False
    schema = prompt.schema()
    assert "  'Forecasts':\n    [Amount] (int64)" in schema and "'Forecasts'[Forecast] = SUM(Forecasts[Amount])" in schema
    assert 'double' not in schema and 'CALCULATED TABLES' not in schema and expression not in schema
    assert 'old formula' not in str(prompt.semantic)
    assert not any('expression' in e for e in ModelPrompt(model, inputs, include_expressions=False).semantic['entries'])
    assert (model, inputs) == original


def test_relationship_state_comes_from_snapshot_and_parallel_relationships_stay_distinct():
    current = Relationship('Fact', 'Key', 'Dim', 'Key', 'single', False)
    parallel = Relationship('Fact', 'Key', 'Dim', 'Key', 'both', True)
    other = Relationship('Dim', 'Key', 'Fact', 'Other')
    model = ModelContext(relationships=[current, parallel, other])
    stale = {**asdict(current), 'is_active': True}
    gone = {**stale, 'from_column': 'Removed'}
    data = selection([], [asdict(current), stale, gone])
    prompt = ModelPrompt(model, data)
    rows = prompt.semantic['retrieval']['relationships']
    assert rows[:2] == [asdict(current), asdict(parallel)] and stale not in rows
    assert rows[2]['in_snapshot'] is False and 'is_active' not in rows[2]
    assert prompt.schema() == "RELATIONSHIPS:\n  'Dim'[Key] -> 'Fact'[Other]"
    exact = ModelPrompt(model, selection([], [asdict(parallel)]))
    assert "'Fact'[Key] -> 'Dim'[Key]  [inactive]" in exact.schema()
    assert '<->' not in exact.schema()


def test_supplemental_hits_share_facts_and_keep_lookup_results_independently_readable():
    relation = Relationship('Fact', 'Code', 'Dim', 'Code')
    model = ModelContext(tables={'Fact': [Column('Code', 'string', 'Fact')], 'Dim': [Column('Code', 'int64', 'Dim')]},
                         relationships=[relation], measures=[Measure('Amount', 'Fact', 'SUM(Fact[Value])')])
    measure = entry('measure', 'Fact', 'Amount', meaning='本期金额', aliases=['本期'], unit='元')
    term = {'id': 't1', 'title': '结算口径', 'meaning': '按结算日', 'state': 'confirmed'}
    base = selection([measure], [asdict(relation)], business_dictionary={'terms': [term]})
    supplement = selection([measure, entry('column', 'Dim', 'Code')], [asdict(relation)],
                           business_dictionary={'terms': [term], 'omitted': 1})
    changed = selection([{**measure, 'state': 'unresolved', 'meaning': '', 'aliases': [], 'unit': ''}])
    lookups = [{'query': '结算', 'ok': True, 'context': supplement}, {'query': '变更', 'ok': True, 'context': changed},
               {'query': '未知', 'ok': False, 'context': None, 'error': '检索未完成'}]
    original = deepcopy((base, lookups))
    prompt = ModelPrompt(model, base, lookups)
    first, second, failed = prompt.lookups
    assert first['query'] == '结算' and first['context']['retrieval'] == {'relationships': [], 'omitted_relevant': 1}
    assert first['context']['business_dictionary'] == {'terms': [{'id': 't1', 'title': '结算口径'}], 'omitted': 1}
    column, reference = first['context']['entries']
    assert column['actual_dtype'] == 'int64' and reference == {'kind': 'measure', 'table': 'Fact', 'name': 'Amount'}
    assert second['context']['entries'] == [{**reference, 'state': 'unresolved', 'meaning': None, 'aliases': None, 'unit': None}]
    assert failed == lookups[-1] and (base, lookups) == original
    assert 'int64' not in prompt.schema() and '[Code] (string)' in prompt.schema()
    assert str(prompt.semantic).count('SUM(Fact[Value])') == 1 and 'SUM(Fact[Value])' not in str(prompt.lookups)


def test_full_model_fallback_and_missing_references_do_not_mask_current_objects():
    model = ModelContext(tables={'Fact': [Column('Code', 'string', 'Fact')]}, measures=[Measure('X', 'Fact', '1')])
    missing = {'kind': 'column', 'table': 'Fact', 'name': 'Code', 'reason': '超过预算'}
    for retrieved in (None, {'entries': [], 'retrieval': {'missing': [missing]}}):
        assert ModelPrompt(model, retrieved).schema() == model.serialize_for_prompt()
    complete = selection([entry('table', 'Fact', 'Fact'), entry('column', 'Fact', 'Code'),
                          entry('measure', 'Fact', 'X')])
    assert ModelPrompt(model, complete).schema() == ''


def test_m_keeps_applied_script_shared_queries_and_exact_output_names():
    script = 'let\n    Source = #"Staging Query"\nin\n    Source'
    model = ModelContext(tables={"O'Brien": [Column('ID', 'int64', "O'Brien"), Column('id', 'string', "O'Brien")]},
                         table_queries={"O'Brien": script}, shared_expressions={'Staging Query': '#table({}, {})'})
    selected = selection([entry('column', "O'Brien", 'ID')])
    facts = ModelPrompt(model, selected, include_expressions=False)
    query = facts.query_schema("O'Brien")
    assert script in query and "'Staging Query'  (parameter/shared)" in query
    assert '[id] (string)' in query and '[ID] (int64)' not in query
    assert facts.semantic['entries'][0]['actual_dtype'] == 'int64'
    assert "  'O''Brien':\n    [id] (string)" in facts.schema()
    assert ModelPrompt(model, None).query_schema("O'Brien") == model.serialize_query_for_prompt("O'Brien")
