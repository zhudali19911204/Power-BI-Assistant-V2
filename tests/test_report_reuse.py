import copy
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import encode, now
from backend.engine.context.base import Column, Measure, ModelContext, Relationship
from backend.powerbi import context_from_dict, fingerprint
from backend.reports import report_from_window, reports_for_instances, schema_signature
from backend.semantics import SemanticEdit, object_id
from backend.business_dictionary import BusinessEdit, OverviewEdit
from tests.test_semantics import DictionaryProvider, wait_ready


def context():
    return ModelContext(tables={'Sales':[Column('Amount','double','Sales'),Column('Code','string','Sales')],
        'Dim':[Column('Code','string','Dim')]},measures=[Measure('Actual','Sales',"SUM('Sales'[Amount])")],
        relationships=[Relationship('Sales','Code','Dim','Code')],table_queries={'Sales':'let Source = 1 in Source'})


@pytest.fixture
def setup(tmp_path,monkeypatch):
    app=create_app(tmp_path)
    state={'engines':[],'contexts':{},'reports':{},'metadata':{},'fail_metadata':False}
    monkeypatch.setattr('backend.powerbi.find_instances',lambda:state['engines'])
    monkeypatch.setattr('backend.semantics.find_instances',lambda:state['engines'])
    monkeypatch.setattr('backend.powerbi.reports_for_instances',lambda engines:{i.pid:state['reports'].get(i.pid,{}) for i in engines})
    def metadata(ctx):
        values={('column',t,c.name):{'description':'','actual_dtype':c.dtype,'source_column':c.name,'format_string':'','expression':''}
                for t,cols in ctx.tables.items() for c in cols}
        values.update(state['metadata'])
        return values
    class Source:
        def __init__(self,port):self.port=port
        def load(self):return state['contexts'][self.port]
        def semantic_metadata(self):
            if state['fail_metadata']:raise RuntimeError('test metadata error')
            return metadata(self.load())
    monkeypatch.setattr('backend.powerbi.LiveDesktopSource',Source)
    monkeypatch.setattr('backend.semantics.LiveDesktopSource',Source)
    monkeypatch.setattr(app.state.settings,'provider',lambda *a:pytest.fail('Connecting/reusing must not call the LLM'))

    def engine(name='费用分析',path=r'E:\Reports\费用分析.pbix',port=50001,pid=55,ctx=None):
        state['contexts'][port]=ctx or context()
        state['reports'][pid]={'name':name,'path':path,'source':'window_title'} if name else {}
        state['engines']=[SimpleNamespace(port=port,pid=pid)]
        return port

    def built(model):
        p=app.state.semantics.get(model['id'])
        for e in p['entries']:
            e.update(state='candidate',business_name='原有业务定义',meaning='已保存的候选含义')
            e['facts'].update(metadata(context_from_dict(model['context'])).get((e['kind'],e['table'],e['name']),{}))
        measure=next(e for e in p['entries'] if e['kind']=='measure')
        measure.update(state='confirmed',meaning='用户确认的实际金额定义',source='用户确认',reviewed_at=now())
        p.update(status='ready',coverage=len(p['entries']),pending_ids=[])
        app.state.semantics._store(model['id'],p)
        return copy.deepcopy(p)
    with TestClient(app) as client:
        yield app,client,state,engine,built


def connect(client,port,**body):
    r=client.post('/api/models/connect',json={'port':port,**body})
    assert r.status_code==200,r.text
    return r.json()


def test_business_knowledge_follows_same_report_and_rechecks_changed_formula(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    business=app.state.semantics.business
    business.edit(old['id'],OverviewEdit(description='工厂销售分析',confirmed=True))
    term=BusinessEdit(kind='term',title='销售实绩',meaning='用于销售分析',confirmed=True,
                      targets=[{'object_id':object_id('measure','Sales','Actual')}])
    business.edit(old['id'],term)
    new=connect(client,engine(port=61111,pid=77))
    data=app.state.semantics.get(new['id'])['business_dictionary']
    assert data['overview']['state']=='confirmed' and data['items'][0]['state']=='confirmed'
    changed=context();changed.measures[0].expression="SUM('Sales'[Amount]) * 2"
    newer=connect(client,engine(port=62222,pid=88,ctx=changed))
    data=app.state.semantics.get(newer['id'])['business_dictionary']
    assert data['overview']['state']=='unresolved' and data['items'][0]['state']=='unresolved'
    unrelated=connect(client,engine(name='另一份报表',path=r'E:\Other\另一份报表.pbix',port=63333,pid=99))
    assert app.state.semantics.get(unrelated['id'])['business_dictionary']['items']==[]


def test_reassociating_history_preserves_business_edits_and_deleted_records(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    business=app.state.semantics.business
    body=BusinessEdit(kind='convention',title='默认分析范围',meaning='用户未指定时的范围说明',confirmed=True)
    first=business.edit(old['id'],body)['business_dictionary']['items'][0]
    second=business.edit(old['id'],body.model_copy(update={'title':'删除测试'}))['business_dictionary']['items'][1]
    new=connect(client,engine(port=61111,pid=77))
    business.edit(new['id'],body.model_copy(update={'meaning':'当前报表修改后的定义'}),first['id'])
    business.delete(new['id'],second['id'])
    p=app.state.semantics.reuse(new['id'],old['id'])
    assert len(p['business_dictionary']['items'])==1
    assert p['business_dictionary']['items'][0]['meaning']=='当前报表修改后的定义'


def test_reopened_report_reuses_dictionary_after_app_restart_and_new_pid_port(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());saved=built(old)
    restarted=create_app(app.state.db.path.parent)
    with TestClient(restarted) as other:
        new=connect(other,engine(port=61111,pid=77))
        p=restarted.state.semantics.get(new['id'])
        assert new['id']!=old['id'] and new['name']=='费用分析' and new['report']['name']=='费用分析'
        assert p['status']=='ready' and p['coverage']==len(saved['entries'])
        assert p['reuse']['matched_by']=='report' and p['reuse']['source_model_id']==old['id']
        assert p['entries']==saved['entries']
        assert not restarted.state.db.all('SELECT * FROM audit')


def test_report_name_works_when_file_path_is_unavailable(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine(path=''));built(old)
    new=connect(client,engine(path='',port=60001,pid=77))
    assert new['semantics']['status']=='ready'


def test_same_name_in_different_known_folders_does_not_automatically_reuse(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    new=connect(client,engine(path=r'F:\Other\费用分析.pbix',port=60001,pid=77))
    assert new['semantics']['status']=='inventory'
    assert app.state.semantics.get(new['id'])['reuse_candidates']


def test_different_named_reports_with_the_same_schema_do_not_automatically_share_definitions(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    new=connect(client,engine(name='人事分析',path=r'E:\Reports\人事分析.pbix',port=60001,pid=77))
    assert new['semantics']['status']=='inventory'


def test_ambiguous_same_name_without_path_offers_explicit_association(setup):
    app,client,state,engine,built=setup
    first=connect(client,engine());built(first)
    second=connect(client,engine(path=r'F:\Other\费用分析.pbix',port=60001,pid=77));built(second)
    new=connect(client,engine(path='',port=61001,pid=88))
    assert new['semantics']['status']=='inventory'
    p=app.state.semantics.get(new['id'])
    assert len(p['reuse_candidates'])==2
    response=client.post(f"/api/models/{new['id']}/semantics/reuse",json={'source_model_id':second['id']})
    assert response.status_code==200 and response.json()['status']=='ready'
    assert response.json()['reuse']['matched_by']=='user_selection'


def test_legacy_dictionary_is_adopted_once_and_then_bound_to_its_report(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine(name='',path=''));saved=built(old)
    new=connect(client,engine(port=60001,pid=77))
    assert new['semantics']['status']=='ready'
    assert app.state.semantics.get(new['id'])['reuse']['matched_by']=='legacy_schema'
    assert app.state.powerbi.get(old['id'])['report']['name']=='费用分析'
    unrelated=connect(client,engine(name='另一个报表',path=r'E:\Reports\另一个报表.pbix',port=61001,pid=88))
    assert unrelated['semantics']['status']=='inventory'


def test_legacy_duplicate_snapshots_from_one_engine_are_not_treated_as_separate_reports(setup):
    app,client,state,engine,built=setup
    first=connect(client,engine(name='',path=''));built(first)
    second=connect(client,50001)
    assert second['semantics']['status']=='ready'
    new=connect(client,engine(port=60001,pid=77))
    assert new['semantics']['status']=='ready'
    assert app.state.powerbi.get(first['id'])['report']['name']=='费用分析'
    assert app.state.powerbi.get(second['id'])['report']['name']=='费用分析'


def test_empty_named_connection_does_not_hide_a_completed_legacy_dictionary(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine(name='',path=''));built(old)
    # An earlier incomplete connection may already have acquired the report name.
    port=engine(port=60001,pid=77)
    placeholder=app.state.powerbi.connect(port)
    app.state.semantics.ensure(placeholder)
    new=connect(client,port)
    assert new['semantics']['status']=='ready'
    assert app.state.semantics.get(new['id'])['reuse']['source_model_id']==old['id']


def test_ambiguous_legacy_reports_require_selection_instead_of_arbitrary_reuse(setup):
    app,client,state,engine,built=setup
    first=connect(client,engine(name='',path=''));built(first)
    second=connect(client,engine(name='',path='',port=60001,pid=77));built(second)
    new=connect(client,engine(port=61001,pid=88))
    assert new['semantics']['status']=='inventory'
    response=client.post(f"/api/models/{new['id']}/semantics/reuse",json={'source_model_id':first['id']})
    assert response.status_code==200 and response.json()['status']=='ready'
    assert not app.state.powerbi.get(second['id'])['report'].get('name')


def test_changed_fields_preserve_unchanged_definitions_and_only_build_pending_objects(setup,monkeypatch):
    app,client,state,engine,built=setup
    old=connect(client,engine());saved=built(old)
    changed=context();changed.tables['Sales'].append(Column('New Category','string','Sales'))
    new=connect(client,engine(port=60001,pid=77,ctx=changed))
    p=app.state.semantics.get(new['id'])
    assert p['status']=='inventory' and len(p['pending_ids'])==2  # changed table + new column
    pending=set(p['pending_ids'])
    old_entries={e['id']:e for e in saved['entries']}
    assert all(e==old_entries[e['id']] for e in p['entries'] if e['id'] not in pending)
    provider=DictionaryProvider()
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    client.post(f"/api/models/{new['id']}/semantics/build",json={})
    finished=wait_ready(app,new['id'])
    assert finished['status']=='ready' and not finished['pending_ids']
    assert {e['id'] for _,payload in provider.calls for e in payload['objects']}==pending
    assert next(e for e in finished['entries'] if e['kind']=='measure')['meaning']=='用户确认的实际金额定义'


def test_interrupted_incremental_build_resumes_only_its_remaining_changed_objects(setup,monkeypatch):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    changed=context();changed.tables['Sales'] += [Column(f'New{i}','string','Sales') for i in range(25)]
    new=connect(client,engine(port=60001,pid=77,ctx=changed))
    class Interrupted(DictionaryProvider):
        def stream(self,*a,**k):
            if self.calls:raise OSError('test interrupted second batch')
            yield from super().stream(*a,**k)
    provider=Interrupted();monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    client.post(f"/api/models/{new['id']}/semantics/build",json={})
    partial=wait_ready(app,new['id'])
    assert partial['status']=='error' and len(partial['pending_ids'])==6
    pending=set(partial['pending_ids'])
    provider=DictionaryProvider()
    client.post(f"/api/models/{new['id']}/semantics/build",json={})
    finished=wait_ready(app,new['id'])
    assert finished['status']=='ready' and finished['coverage']==len(finished['entries'])
    assert {e['id'] for _,payload in provider.calls for e in payload['objects']}==pending


@pytest.mark.parametrize('change',['formula','description','type'])
def test_changed_formula_description_or_actual_type_invalidates_only_its_entry(setup,change):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    changed=context()
    if change=='formula':changed.measures[0].expression="SUM('Sales'[Amount]) * 2"
    else:state['metadata'][('column','Sales','Code')]={
        'description':'新的字段含义' if change=='description' else '',
        'actual_dtype':'int64' if change=='type' else 'string','source_column':'Code','format_string':'','expression':''}
    new=connect(client,engine(port=60001,pid=77,ctx=changed))
    p=app.state.semantics.get(new['id'])
    assert p['status']=='inventory' and len(p['pending_ids'])==1
    assert p['pending_ids'][0]==object_id('measure','Sales','Actual') if change=='formula' else p['pending_ids'][0]==object_id('column','Sales','Code')


def test_order_and_cardinality_changes_do_not_require_rebuilding(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    changed=context();changed.tables={k:list(reversed(v)) for k,v in reversed(list(changed.tables.items()))}
    changed.tables['Sales'][0].cardinality=12345
    assert fingerprint(changed)!=old['fingerprint']
    assert schema_signature(asdict(changed))==schema_signature(old['context'])
    new=connect(client,engine(port=60001,pid=77,ctx=changed))
    assert new['semantics']['status']=='ready'


def test_metadata_failure_does_not_claim_the_old_dictionary_is_fully_verified(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    state['fail_metadata']=True
    new=connect(client,engine(port=60001,pid=77))
    assert new['semantics']['status']=='inventory'
    assert app.state.semantics.get(new['id'])['error']


def test_fallback_manual_report_name_is_persisted_and_reused(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine(name='',path=''),report_name='手工关联报表');built(old)
    assert old['report']['source']=='manual'
    new=connect(client,engine(name='',path='',port=60001,pid=77),report_name='手工关联报表')
    assert new['semantics']['status']=='ready'


def test_manual_dictionary_with_model_differences_is_allowed_and_unknown_source_is_rejected(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    changed=context();changed.tables['Sales'].append(Column('New','string','Sales'))
    new=connect(client,engine(port=60001,pid=77,ctx=changed))
    response=client.post(f"/api/models/{new['id']}/semantics/reuse",json={'source_model_id':old['id']})
    assert response.status_code==200
    assert response.json()['dictionary_notice']['status']=='needs_update'
    assert response.json()['pending_ids']
    response=client.post(f"/api/models/{new['id']}/semantics/reuse",json={'source_model_id':'unknown-id'})
    assert response.status_code==404


def test_manual_association_preserves_current_user_review(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    new=connect(client,engine(name='副本',path=r'F:\副本.pbix',port=60001,pid=77))
    entry_id=object_id('column','Sales','Code')
    app.state.semantics.edit(new['id'],entry_id,SemanticEdit(meaning='本报表用户修订的编号含义',confirmed=True))
    response=client.post(f"/api/models/{new['id']}/semantics/reuse",json={'source_model_id':old['id']})
    assert response.status_code==200
    e=next(e for e in response.json()['entries'] if e['id']==entry_id)
    assert e['meaning']=='本报表用户修订的编号含义' and e['state']=='confirmed'


def test_value_preview_and_lookup_cache_are_not_carried_to_a_new_engine(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    app.state.db.execute('INSERT INTO model_value_lookups VALUES (?,?,?,?,?)',
        (old['id'],'0257',old['fingerprint'],encode({'matches':[{'table':'Sales','column':'Code'}]}),now()))
    new=connect(client,engine(port=60001,pid=77))
    assert not app.state.db.all('SELECT * FROM model_value_lookups WHERE model_id=?',(new['id'],))


@pytest.mark.parametrize('title,command,name,path',[
    ('采购分析 - Power BI Desktop',r'"PBIDesktop.exe" "E:\Reports\采购分析.pbix"','采购分析',r'E:\Reports\采购分析.pbix'),
    ('Purchase Report * – Power BI Desktop',r'"C:\Power BI\PBIDesktop.exe" "E:\Reports\Purchase Report.pbix"','Purchase Report',r'E:\Reports\Purchase Report.pbix'),
    ('新报表 - Power BI Desktop',r'PBIDesktop.exe "E:\Reports\旧报表.pbix"','新报表',''),
    ('模型 - Microsoft Power BI Desktop (Preview)',r'PBIDesktop.exe E:\Data\模型.pbip','模型',r'E:\Data\模型.pbip'),
])
def test_window_name_and_optional_file_path_are_parsed_without_using_stale_command_lines(title,command,name,path):
    report=report_from_window(title,command,123)
    assert report['name']==name and report['path']==path and report['desktop_pid']==123


@pytest.mark.parametrize('title',['无标题 - Power BI Desktop','Untitled - Power BI Desktop','Untitled 2 - Power BI Desktop','','another application'])
def test_unsaved_or_unrecognized_windows_do_not_create_stable_report_names(title):
    assert report_from_window(title)=={}


def test_engine_parent_chain_maps_two_reports_to_the_correct_windows(monkeypatch):
    monkeypatch.setattr('backend.reports._report_snapshot',lambda:{'nodes':[
        {'ProcessId':11,'ParentProcessId':20},{'ProcessId':20,'ParentProcessId':100},
        {'ProcessId':12,'ParentProcessId':200}], 'desktops':[
        {'id':100,'title':'采购 - Power BI Desktop','command':''},
        {'id':200,'title':'销售 - Power BI Desktop','command':''}]})
    reports=reports_for_instances([SimpleNamespace(pid=11),SimpleNamespace(pid=12),SimpleNamespace(pid=999)])
    assert reports[11]['name']=='采购' and reports[12]['name']=='销售' and 999 not in reports


def test_new_desktop_versions_can_expose_only_the_report_name_in_the_window_title():
    assert report_from_window('Sales_BI_Report',desktop_pid=100,verified_desktop=True)['name']=='Sales_BI_Report'
    assert not report_from_window('Power BI Desktop',desktop_pid=100,verified_desktop=True)
