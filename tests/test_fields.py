import datetime

import pytest
from fastapi import HTTPException

from backend.analysis import readonly_query
from backend.app import create_app
from backend.fields import FieldCheck, FieldPreviewInput, profile_queries
from tests.test_analysis import setup


@pytest.fixture
def engine(setup,monkeypatch):
    app,client,conv=setup
    calls=[]
    stats={'[row_count]':10,'[distinct_count]':4,'[blank_count]':2,'[empty_string_count]':1,'[matched_distinct_count]':4}
    rows=[{'[__value]':'personnel','[__count]':4},{'[__value]':'0257','[__count]':3},
          {'[__value]':None,'[__count]':2},{'[__value]':'','[__count]':1}]
    state={'stats':[stats],'rows':rows,'error':None}
    class Session:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def query(self,dax,max_rows,timeout_seconds):
            calls.append((dax,max_rows,timeout_seconds))
            if state['error']:raise state['error']
            return state['stats'] if dax.startswith('EVALUATE ROW(') else state['rows'][:max_rows]
    monkeypatch.setattr('backend.fields.find_instances',lambda:[])
    monkeypatch.setattr('backend.fields.find_adomd_dll',lambda *a:'fake')
    monkeypatch.setattr('backend.fields._LiveSession',Session)
    return app,client,conv,calls,state


def test_field_preview_returns_actual_counts_samples_and_leading_zero_text(engine):
    app,client,conv,calls,state=engine
    response=client.post(f"/api/models/{conv['model_id']}/fields/preview",json={'table':'产品','column':'类别'})
    assert response.status_code==200,response.text
    profile=response.json()
    assert profile['statistics']=={'row_count':10,'distinct_count':4,'blank_count':2,'empty_string_count':1,'matched_distinct_count':4}
    assert [v['value'] for v in profile['values']]==['personnel','0257',None,'']
    assert profile['run_verified'] and not profile['truncated']
    assert len(calls)==2 and calls[1][1]==51
    assert all(call[2]==15 for call in calls)
    assert not app.state.db.all('SELECT * FROM audit')


def test_search_has_separate_full_model_samples_and_escapes_literals_and_wildcards(engine):
    app,client,conv,calls,state=engine
    body=FieldPreviewInput(table='产品',column='类别',search='人*?~"员',limit=3)
    profile=app.state.chat.analysis.fields.preview(conv['model_id'],body)
    assert len(calls)==3 and calls[-1][1]==6
    assert 'CONTAINSSTRING' in calls[0][0] and 'CONTAINSSTRING' in calls[1][0]
    assert 'CONTAINSSTRING' not in calls[2][0]
    assert '~*~?~~""' in calls[0][0]
    assert profile['search_terms']==['人*?~"员'] and profile['truncated']
    assert profile['samples']


def test_automatic_reads_use_smaller_time_and_value_budgets(engine):
    app,client,conv,calls,state=engine
    state['rows']=[{'[__value]':'长'*600,'[__count]':1} for _ in range(21)]
    state['stats'][0]['[matched_distinct_count]']=21
    profile=app.state.chat.analysis.fields.preview(conv['model_id'],FieldPreviewInput(table='产品',column='类别',limit=20),automatic=True)
    assert profile['truncated'] and len(profile['values'])<20
    assert all(v['value_truncated'] and len(v['value'])==500 for v in profile['values'])
    assert all(len(v['value'])<=120 for v in profile['samples'])
    assert all(call[2]==8 for call in calls)


@pytest.mark.parametrize('body',[{'table':'不存在','column':'类别'}, {'table':'产品','column':'不存在'},
                                  {'table':"产品'); DROP TABLE X; --",'column':'类别'}])
def test_nonexistent_identifiers_are_rejected_without_querying(engine,body):
    app,client,conv,calls,state=engine
    assert client.post(f"/api/models/{conv['model_id']}/fields/preview",json=body).status_code==422
    assert not calls


def test_preview_requires_real_desktop_and_fails_honestly_on_engine_errors(engine,tmp_path):
    app,client,conv,calls,state=engine
    state['error']=RuntimeError('engine failure')
    response=client.post(f"/api/models/{conv['model_id']}/fields/preview",json={'table':'产品','column':'类别'})
    assert response.status_code==422 and 'engine failure' in response.json()['detail']
    other=create_app(tmp_path/'demo-only')
    with pytest.raises(HTTPException) as failure:
        other.state.chat.analysis.fields.preview(other.state.powerbi.demo()['id'],FieldPreviewInput(table='产品',column='类别'))
    assert failure.value.status_code==409


def test_empty_column_is_zero_rows_while_missing_statistics_is_an_error(engine):
    app,client,conv,calls,state=engine
    state['rows']=[]
    state['stats']=[{f'[{k}]':0 for k in ('row_count','distinct_count','blank_count','empty_string_count','matched_distinct_count')}]
    body=FieldPreviewInput(table='产品',column='类别')
    profile=app.state.chat.analysis.fields.preview(conv['model_id'],body)
    assert profile['values']==[] and profile['statistics']['row_count']==0 and not profile['truncated']
    state['stats']=[]
    with pytest.raises(HTTPException):app.state.chat.analysis.fields.preview(conv['model_id'],body)


def test_dates_and_nonfinite_values_remain_json_serializable(engine):
    app,client,conv,calls,state=engine
    state['rows']=[{'[__value]':datetime.datetime(2026,5,1),'[__count]':1},
                   {'[__value]':float('nan'),'[__count]':1}]
    response=client.post(f"/api/models/{conv['model_id']}/fields/preview",json={'table':'产品','column':'类别'})
    assert response.status_code==200
    assert response.json()['values'][0]['value']=='2026-05-01T00:00:00'
    assert response.json()['values'][1]['nonfinite']


def test_generated_queries_are_readonly_and_escape_quotes_brackets_and_injection_text():
    queries=profile_queries("O'Brien",'x]y','string',['" ); EVALUATE DROP ~*?'],5)
    assert all(readonly_query(query)==query for query in queries if query)
    assert "'O''Brien'[x]]y]" in queries[0]
    assert '"" ); EVALUATE DROP' in queries[0]
    assert 'FILTER(ALL(' in queries[1]
    assert profile_queries('Table','__pbi_preview_count','string',[],5)[1].count('"__pbi_preview_count_"')==1


def test_profile_and_probe_limits_are_validated(engine):
    app,client,conv,calls,state=engine
    assert client.post(f"/api/models/{conv['model_id']}/fields/preview",json={'table':'产品','column':'类别','limit':101}).status_code==422
    with pytest.raises(ValueError):FieldCheck(table='产品',column='类别',search_terms=['a']*4)
    with pytest.raises(ValueError):FieldCheck(table='产品',column='类别',search_terms=['x'*121])
    assert FieldCheck(table='产品',column='类别',search_terms=[' 人员 ','人员','']).search_terms==['人员']
