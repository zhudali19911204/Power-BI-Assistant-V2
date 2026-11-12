import copy
import json
import threading

import pytest

from backend.business_dictionary import BusinessEdit, OverviewEdit
from backend.database import encode
from backend.engine.context.base import Column, Measure
from backend.semantics import SemanticEdit, object_id
from backend.powerbi import fingerprint
from dataclasses import asdict
from tests.test_report_reuse import setup, connect, context


def library(client, model=None):
    suffix='?current_model_id='+model['id'] if model else ''
    response=client.get('/api/semantic-dictionaries'+suffix)
    assert response.status_code==200,response.text
    return response.json()


def preview(client,target,source):
    return client.get(f"/api/models/{target['id']}/semantics/reuse-preview",params={'source_model_id':source['id']})


def delete(client,source,**params):
    return client.delete('/api/semantic-dictionaries/'+source['id'],params=params)


def rename(client,model,name,**extra):
    return client.put('/api/semantic-dictionaries/'+model['id']+'/name',json={'name':name,**extra})


def test_catalog_groups_report_revisions_and_retains_distinct_paths(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    newer=connect(client,engine(port=61111,pid=77))
    other=connect(client,engine(path=r'F:\Other\费用分析.pbix',port=62222,pid=88));built(other)
    data=library(client,newer)
    rows={r['model_id']:r for r in data['dictionaries']}
    assert len(rows)==3
    assert rows[old['id']]['group']==rows[newer['id']]['group']
    assert rows[old['id']]['group']!=rows[other['id']]['group']
    assert rows[newer['id']]['is_current'] and not rows[old['id']]['is_current']
    assert rows[old['id']]['same_schema'] and rows[old['id']]['shared_objects']==rows[old['id']]['total']
    assert all('entries' not in row and 'context' not in row for row in rows.values())


def test_catalog_includes_partial_business_only_dictionaries_but_excludes_empty_inventory(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine())
    assert library(client)['dictionaries']==[]
    app.state.semantics.business.edit(source['id'],OverviewEdit(description='费用分析模型',confirmed=True))
    rows=library(client)['dictionaries']
    assert len(rows)==1 and rows[0]['business_count']==1 and rows[0]['coverage']==0


def test_manual_association_previews_differences_without_mutating_profiles_and_preserves_compatible_definitions(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    business=app.state.semantics.business
    business.edit(source['id'],OverviewEdit(description='费用业务总览',confirmed=True))
    term=BusinessEdit(kind='term',title='实际费用',meaning='费用汇总',confirmed=True,
                      targets=[{'object_id':object_id('measure','Sales','Actual')}])
    business.edit(source['id'],term)
    changed=context();changed.measures=[Measure('Actual','Sales',"SUM('Sales'[Amount]) * 2")]
    changed.tables['Sales'].append(Column('Department','string','Sales'))
    target=connect(client,engine(name='费用副本',path=r'F:\Copy.pbix',port=61111,pid=77,ctx=changed))
    before=app.state.db.all('SELECT * FROM model_semantics ORDER BY model_id')
    response=preview(client,target,source)
    assert response.status_code==200
    data=response.json()
    assert not data['same_schema'] and data['changes']['added']==1 and data['changes']['modified']>=2
    assert data['dictionary_notice']['status']=='needs_update'
    assert data['dictionary_notice']['pending_business']==2
    assert app.state.db.all('SELECT * FROM model_semantics ORDER BY model_id')==before
    response=client.post(f"/api/models/{target['id']}/semantics/reuse",json={
        'source_model_id':source['id'],'source_updated_at':data['source_updated_at']})
    assert response.status_code==200
    p=response.json()
    actual=next(e for e in p['entries'] if e['kind']=='measure')
    unchanged=next(e for e in p['entries'] if e['kind']=='column' and e['table']=='Dim')
    assert actual['state']=='unresolved' and actual['facts']['expression']==changed.measures[0].expression
    assert unchanged['state']=='candidate' and unchanged['meaning']=='已保存的候选含义'
    assert p['business_dictionary']['overview']['state']=='unresolved'
    assert p['business_dictionary']['items'][0]['state']=='unresolved'
    source_before=json.loads(next(row['payload'] for row in before if row['model_id']==source['id']))
    assert app.state.semantics.get(source['id'])['entries']==source_before['entries']


def test_ready_dictionary_can_be_manually_associated_and_preview_reports_local_review_precedence(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    target=connect(client,engine(name='副本',path=r'F:\Copy.pbix',port=61111,pid=77));built(target)
    key=object_id('column','Sales','Code')
    app.state.semantics.edit(target['id'],key,SemanticEdit(meaning='本地已审阅定义',confirmed=True))
    p=preview(client,target,source).json()
    assert p['preserved_reviews']>=1
    response=client.post(f"/api/models/{target['id']}/semantics/reuse",json={'source_model_id':source['id']})
    assert response.status_code==200
    assert next(e for e in response.json()['entries'] if e['id']==key)['meaning']=='本地已审阅定义'


def test_source_revision_change_requires_a_new_preview(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    target=connect(client,engine(name='副本',path=r'F:\Copy.pbix',port=61111,pid=77))
    p=preview(client,target,source).json()
    app.state.semantics.edit(source['id'],object_id('column','Sales','Code'),SemanticEdit(meaning='刚修改的业务说明',confirmed=True))
    response=client.post(f"/api/models/{target['id']}/semantics/reuse",json={'source_model_id':source['id'],'source_updated_at':p['source_updated_at']})
    assert response.status_code==409
    assert app.state.semantics.get(target['id']).get('reuse') is None


def test_deleted_source_cannot_be_associated_but_its_existing_copies_survive(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    target=connect(client,engine(port=61111,pid=77))
    before=copy.deepcopy(app.state.semantics.get(target['id']))
    assert delete(client,source).status_code==200
    after=app.state.semantics.get(target['id'])
    assert after['entries']==before['entries'] and after['reuse']['source_deleted']
    assert source['id'] not in {r['model_id'] for r in library(client)['dictionaries']}
    assert preview(client,target,source).status_code==404
    assert client.post(f"/api/models/{target['id']}/semantics/reuse",json={'source_model_id':source['id']}).status_code==404
    # Opening a deleted snapshot only creates fresh inventory, not the deleted definitions.
    app.state.semantics.get(source['id'])
    assert app.state.semantics.get(target['id'])['reuse']['source_deleted']
    assert source['id'] not in {r['model_id'] for r in library(client)['dictionaries']}


def test_delete_keeps_models_conversations_and_other_dictionary_revisions(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    target=connect(client,engine(port=61111,pid=77))
    conv=client.post('/api/conversations',json={'model_id':source['id'],'mode':'dax'}).json()
    row=next(r for r in library(client)['dictionaries'] if r['model_id']==source['id'])
    response=delete(client,source,updated_at=row['updated_at'],current_model_id=target['id'])
    assert response.status_code==200
    assert app.state.db.one('SELECT id FROM models WHERE id=?',(source['id'],))
    assert app.state.db.one('SELECT id FROM conversations WHERE id=?',(conv['id'],))
    assert app.state.db.one('SELECT model_id FROM model_semantics WHERE model_id=?',(target['id'],))
    assert not app.state.db.one('SELECT model_id FROM model_semantics WHERE model_id=?',(source['id'],))
    assert delete(client,source).status_code==404


@pytest.mark.parametrize('job',['objects','business','conversation'])
def test_active_dictionary_cannot_be_deleted_or_reassociated(setup,job):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    target=connect(client,engine(port=61111,pid=77))
    if job=='objects':app.state.semantics.jobs[target['id']]=True
    elif job=='business':app.state.semantics.business.jobs[target['id']]=True
    else:
        conv=client.post('/api/conversations',json={'model_id':target['id'],'mode':'dax'}).json()
        lock=threading.Lock();lock.acquire();app.state.chat.locks[conv['id']]=lock
    try:
        assert delete(client,target).status_code==409
        assert rename(client,target,'任务进行中禁止重命名').status_code==409
        assert client.post(f"/api/models/{target['id']}/semantics/reuse",json={'source_model_id':source['id']}).status_code==409
        catalog=library(client,target)
        assert catalog['target_busy'] and next(r for r in catalog['dictionaries'] if r['is_current'])['busy']
    finally:
        app.state.semantics.jobs.pop(target['id'],None);app.state.semantics.business.jobs.pop(target['id'],None)
        if job=='conversation':lock.release()


def test_current_dictionary_and_newer_revision_are_protected_from_stale_deletion(setup,monkeypatch):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    row=library(client)['dictionaries'][0]
    assert delete(client,source,current_model_id=source['id']).status_code==409
    app.state.semantics.business.edit(source['id'],OverviewEdit(description='新补充的业务总览',confirmed=True))
    assert delete(client,source,updated_at=row['updated_at']).status_code==409
    assert app.state.db.one('SELECT model_id FROM model_semantics WHERE model_id=?',(source['id'],))
    current=library(client)['dictionaries'][0]
    execute=app.state.db.execute
    def concurrent_update(sql,params=()):
        if sql.startswith('DELETE FROM model_semantics'):
            execute('UPDATE model_semantics SET updated_at=? WHERE model_id=?',('concurrent-revision',source['id']))
        return execute(sql,params)
    monkeypatch.setattr(app.state.db,'execute',concurrent_update)
    assert delete(client,source,updated_at=current['updated_at']).status_code==409
    assert app.state.db.one('SELECT model_id FROM model_semantics WHERE model_id=?',(source['id'],))


def test_connected_model_reports_differences_until_affected_business_records_are_reviewed(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    app.state.semantics.business.edit(source['id'],OverviewEdit(description='原业务总览',confirmed=True))
    changed=context();changed.measures=[Measure('Actual','Sales',"SUM('Sales'[Amount]) * 2")]
    target=connect(client,engine(port=61111,pid=77,ctx=changed))
    assert target['semantics']['can_answer']
    assert target['semantics']['dictionary_notice']['status']=='needs_update'
    assert target['semantics']['dictionary_notice']['pending_objects']>=1
    service=app.state.semantics
    for entry in service.get(target['id'])['entries']:
        if entry['state']=='unresolved':service.edit(target['id'],entry['id'],SemanticEdit(meaning='已核对当前对象',confirmed=True))
    notice=service.get(target['id'])['dictionary_notice']
    assert notice['pending_objects']==0 and notice['pending_business']==1 and notice['status']=='needs_update'
    p=service.business.edit(target['id'],OverviewEdit(description='已按新模型复核业务总览',confirmed=True))
    assert p['dictionary_notice']['status']=='current'


def test_preview_uses_dictionary_source_snapshot_when_source_model_was_changed_in_place(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    changed=context();changed.table_queries['Sales']='let Source = 2 in Source'
    # Model writes can update native context before the old dictionary is read/aligned again.
    app.state.db.execute('UPDATE models SET context=?,fingerprint=? WHERE id=?',
                         (encode(asdict(changed)),fingerprint(changed),source['id']))
    target=connect(client,engine(name='副本',path=r'F:\Copy.pbix',port=61111,pid=77,ctx=changed))
    p=preview(client,target,source).json()
    items={item['name']:item for item in p['changes']['items']}
    assert 'Power Query 或计算表来源变化' in items['Sales']['reasons']
    assert items['Actual']['change']=='modified'


def test_metadata_failure_remains_explicit_in_preview_and_after_association(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    state['fail_metadata']=True
    target=connect(client,engine(name='副本',path=r'F:\Copy.pbix',port=61111,pid=77))
    assert preview(client,target,source).json()['metadata_status']!='read'
    response=client.post(f"/api/models/{target['id']}/semantics/reuse",json={'source_model_id':source['id']})
    assert response.status_code==200 and response.json()['dictionary_notice']['status']=='unverified'


def test_custom_dictionary_name_is_saved_without_changing_native_identity_or_definitions(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    before=app.state.semantics.get(source['id'])
    old=library(client)['dictionaries'][0]
    response=rename(client,source,'  工厂费用业务词典  ',updated_at=old['updated_at'])
    assert response.status_code==200 and response.json()['name']=='工厂费用业务词典'
    row=library(client)['dictionaries'][0]
    assert row['name']=='工厂费用业务词典' and row['report_name']=='费用分析'
    assert row['group']==old['group'] and row['saved_at']==old['saved_at']
    assert row['updated_at']!=old['updated_at']
    after=app.state.semantics.get(source['id'])
    assert after['dictionary_name']=='工厂费用业务词典'
    assert after['entries']==before['entries'] and after['business_dictionary']==before['business_dictionary']
    native=app.state.powerbi.get(source['id'])
    assert native['context']==source['context'] and native['report']==source['report']
    restarted=type(app.state.semantics)(app.state.db,app.state.powerbi)
    assert restarted.get(source['id'])['dictionary_name']=='工厂费用业务词典'


def test_custom_names_appear_in_preview_and_survive_association_reopen_and_schema_change(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    assert rename(client,source,'费用分析业务词典').status_code==200
    target=connect(client,engine(name='费用副本',path=r'F:\Copy.pbix',port=61111,pid=77))
    assert app.state.semantics.get(target['id']).get('reuse') is None
    p=preview(client,target,source).json()
    assert p['source_name']=='费用分析业务词典'
    response=client.post(f"/api/models/{target['id']}/semantics/reuse",json={'source_model_id':source['id'],'source_updated_at':p['source_updated_at']})
    assert response.status_code==200
    assert response.json()['dictionary_name']=='费用分析业务词典'
    assert response.json()['reuse']['source_name']=='费用分析业务词典'
    changed=context();changed.tables['Sales'].append(Column('Department','string','Sales'))
    new=connect(client,engine(name='费用副本',path=r'F:\Copy.pbix',port=62222,pid=88,ctx=changed))
    assert app.state.semantics.get(new['id'])['dictionary_name']=='费用分析业务词典'
    assert new['report']['name']=='费用副本'


def test_explicit_destination_dictionary_name_wins_over_historical_association(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source);rename(client,source,'来源词典')
    target=connect(client,engine(name='副本',path=r'F:\Copy.pbix',port=61111,pid=77));built(target)
    assert rename(client,target,'本地自己命名的词典').status_code==200
    p=client.post(f"/api/models/{target['id']}/semantics/reuse",json={'source_model_id':source['id']}).json()
    assert p['dictionary_name']=='本地自己命名的词典' and p['reuse']['source_name']=='来源词典'
    reopened=connect(client,engine(name='副本',path=r'F:\Copy.pbix',port=62222,pid=88))
    p=client.post(f"/api/models/{reopened['id']}/semantics/reuse",json={'source_model_id':source['id']}).json()
    assert p['dictionary_name']=='本地自己命名的词典'


def test_renaming_old_revision_does_not_resurrect_older_business_definitions(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    newer=connect(client,engine(port=61111,pid=77))
    key=object_id('measure','Sales','Actual')
    app.state.semantics.edit(newer['id'],key,SemanticEdit(meaning='较新版本已修正的实际金额定义',confirmed=True))
    assert rename(client,old,'给旧词典取个新名字').status_code==200
    new=connect(client,engine(port=62222,pid=88))
    p=app.state.semantics.get(new['id'])
    assert p['reuse']['source_model_id']==newer['id']
    assert next(e for e in p['entries'] if e['id']==key)['meaning']=='较新版本已修正的实际金额定义'


@pytest.mark.parametrize('name',['','   ','x'*161,'业务\n词典','业务\x00词典'])
def test_dictionary_name_validation(setup,name):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    assert rename(client,source,name).status_code==422


def test_name_revision_conflicts_and_deleted_dictionaries_do_not_overwrite_newer_data(setup):
    app,client,state,engine,built=setup
    source=connect(client,engine());built(source)
    old=library(client)['dictionaries'][0]
    assert rename(client,source,'第一次命名',updated_at=old['updated_at']).status_code==200
    assert rename(client,source,'过期表单的命名',updated_at=old['updated_at']).status_code==409
    assert library(client)['dictionaries'][0]['name']=='第一次命名'
    assert delete(client,source).status_code==200
    assert rename(client,source,'已经不存在').status_code==404
