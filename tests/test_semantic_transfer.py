from backend.database import encode
from backend.engine.context.base import Column
from backend.semantics import object_id
from tests.test_semantics import setup


def path(model):
    return f"/api/models/{model['id']}/semantics"


def populate(client, model):
    base = path(model)
    key = object_id('column', 'Dim Cost Center', 'CC Number')
    assert client.put(base+'/'+key, json={'meaning': '保留前导零的编号', 'aliases': ['CC'], 'confirmed': True}).is_success
    assert client.put(base+'/business/overview', json={'description': '费用总览', 'confirmed': True}).is_success
    for item in [
        {'kind': 'term', 'title': '总部', 'meaning': '法人 0257', 'confirmed': True,
         'filters': [{'object_id': key, 'values': ['0257']}]},
        {'kind': 'convention', 'title': '单位', 'meaning': '默认使用千元'},
    ]:
        assert client.post(base+'/business/items', json=item).is_success
    response = client.get(base+'/export')
    assert response.is_success and 'attachment;' in response.headers['Content-Disposition']
    return response.json()


def preview(client, model, document):
    r = client.post(path(model)+'/import-preview', json=document)
    assert r.is_success, r.text
    return r.json()


def apply(client, model, document, plan):
    return client.post(path(model)+'/import', params={'updated_at': plan['updated_at']}, json=document)


def test_portable_roundtrip_merges_into_current_dictionary_and_reimports_without_duplicates(setup):
    app, client, source, ctx, _ = setup
    document = populate(client, source)
    assert 'source_snapshot' not in document and all('facts' not in e for e in document['entries'])
    dest = app.state.powerbi.save('另一台电脑上的报表', 'desktop', 50002, ctx)
    base = path(dest)
    local = {'kind':'convention','title':'本地约定','meaning':'保留本地定义'}
    client.post(base+'/business/items', json=local)
    original = client.get(base).json()
    p = preview(client, dest, document)
    assert p['compatible'] and p['objects']==1 and p['items']==2 and p['overview']
    assert client.get(base).json()==original
    for _ in range(2):
        r = apply(client,dest,document,p)
        assert r.is_success, r.text
        profile = r.json()['profile']
        assert profile['dictionary_id']==original['dictionary_id']
        entry = next(e for e in profile['entries'] if e['name']=='CC Number')
        assert (entry['meaning'],entry['state'],entry['aliases'])==('保留前导零的编号','confirmed',['CC'])
        data = profile['business_dictionary']
        assert data['overview']==document['overview']
        assert [i['title'] for i in data['items']]==['本地约定','总部','单位']
        assert data['items'][1:]==document['items']
        assert data['items'][1]['filters'][0]['values']==['0257']
        p = preview(client,dest,document)
    assert app.state.semantics.summary(dest['id'])['can_answer']
    restored=client.get(base+'/export').json()
    assert restored['entries']==document['entries'] and restored['overview_pending']==document['overview_pending']


def test_changed_schema_preserves_local_reviews_and_marks_imported_records_stale(setup):
    app, client, source, ctx, _ = setup
    doc = populate(client,source)
    del ctx.tables['Dim Material']
    ctx.tables['Fact'].append(Column('Other','double','Fact'))
    dest=app.state.powerbi.save('结构不同', 'desktop', 50002, ctx)
    base=path(dest)
    p=preview(client,dest,doc)
    assert not p['compatible'] and p['unmatched']==2 and p['review']==4
    profile=apply(client,dest,doc,p).json()['profile']
    entry=next(e for e in profile['entries'] if e['name']=='CC Number')
    assert entry['state']=='unresolved' and not entry['meaning'] and entry['previous_definition']['meaning']=='保留前导零的编号'
    assert profile['business_dictionary']['items'][0]['state']=='unresolved'
    assert profile['business_dictionary']['overview']['state']=='unresolved'
    client.put(base+'/'+entry['id'],json={'meaning':'本地审核结果','confirmed':True})
    client.put(base+'/business/overview',json={'description':'本地总览','confirmed':True})
    p=preview(client,dest,doc)
    assert p['protected']>=2
    after=apply(client,dest,doc,p).json()['profile']
    assert next(e for e in after['entries'] if e['id']==entry['id'])['meaning']=='本地审核结果'
    assert after['business_dictionary']['overview']['description']=='本地总览'


def test_stale_preview_busy_target_and_invalid_file_do_not_overwrite(setup):
    app,client,model,_,_=setup
    doc=populate(client,model)
    p=preview(client,model,doc)
    client.put(path(model)+'/business/overview',json={'description':'刚更新','confirmed':True})
    assert apply(client,model,doc,p).status_code==409
    before=client.get(path(model)).json()
    app.state.semantics.in_use=lambda _:True
    assert apply(client,model,doc,{'updated_at':before['updated_at']}).status_code==409
    app.state.semantics.in_use=lambda _:False
    bad=[{**doc,'version':90},{**doc,'entries':doc['entries']+doc['entries'][:1]},
         {**doc,'items':[dict(doc['items'][0], filters=[{'object_id':object_id('measure','Fact','Actual Amount'),'values':['0257']}])]}]
    for payload in bad:
        assert apply(client,model,payload,{'updated_at':before['updated_at']}).status_code==422
    assert client.get(path(model)).json()==before
    invalid=client.post(path(model)+'/import-preview',content='not json',headers={'content-type':'application/json'})
    assert invalid.status_code==422 and 'JSON' in invalid.json()['detail']
    bom=client.post(path(model)+'/import-preview',content=b'\xef\xbb\xbf'+encode(doc).encode(),headers={'content-type':'application/json'})
    assert bom.status_code==200


def test_large_file_uses_import_limit_and_same_schema_keeps_stale_reference_history(setup,monkeypatch):
    app,client,model,_,_=setup
    doc=populate(client,model)
    stale=doc['items'][0]
    stale.update(state='unresolved',stale_reason='关联字段已移除',affected_object_ids=['removed_column'])
    stale['filters'][0]['object_id']='removed_column'
    body=b' '*(4*1024*1024)+encode(doc).encode()
    url=path(model)+'/import-preview'
    p=client.post(url,content=body,headers={'content-type':'application/json'})
    assert p.is_success,p.text
    assert apply(client,model,doc,p.json()).json()['profile']['business_dictionary']['items'][0]==stale
    monkeypatch.setattr('backend.semantic_transfer.MAX_IMPORT_BYTES',1024)
    assert client.post(url,content=body,headers={'content-type':'application/json'}).status_code==413
