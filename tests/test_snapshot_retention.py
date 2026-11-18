from fastapi.testclient import TestClient
import json

from backend.app import create_app
from backend.database import encode, now
from backend.engine.context.base import Column
from tests.test_report_reuse import connect, context, setup


def test_switch_reports_releases_structure_and_caches_but_keeps_dictionary_and_identity(setup):
    app,client,state,engine,built=setup
    first=connect(client,engine());original=built(first)
    model_id=first['id']
    app.state.db.execute('INSERT INTO model_value_lookups VALUES (?,?,?,?,?)',
                         (model_id,'123',first['fingerprint'],encode({'matches':[]}),now()))
    demo=client.post('/api/models/demo',json={}).json()
    second=connect(client,engine(name='另一报表',path=r'F:\另一报表.pbix',port=61111,pid=77))
    assert {m['id'] for m in client.get('/api/models').json()}=={second['id'],demo['id']}
    assert app.state.db.one('SELECT context FROM models WHERE id=?',(model_id,))=={'context':'null'}
    assert model_id not in app.state.powerbi.reconnector.states
    assert not app.state.db.all('SELECT * FROM model_value_lookups')
    assert app.state.semantics.get(model_id)['entries']==original['entries']
    assert client.get(f'/api/models/{model_id}/connection').json()['connection']['status']=='released'
    with TestClient(create_app(app.state.db.path.parent)) as restarted:
        assert restarted.get(f'/api/models/{model_id}').json()['snapshot_released']
        restored=connect(restarted,engine(port=62222,pid=88))
        assert restored['id']==model_id and restored['context']==first['context'] and restored['live']
        assert restarted.get(f'/api/models/{model_id}/semantics').json()['entries']==original['entries']
        assert restarted.get(f"/api/models/{second['id']}").json()['snapshot_released']
        changed=context();changed.tables['Sales'].append(Column('New','string','Sales'))
        latest=connect(restarted,engine(port=63001,pid=99,ctx=changed))
        assert latest['semantics']['pending']==2
        assert app.state.db.all("SELECT id FROM models WHERE source='desktop' AND context!='null'")==[{'id':latest['id']}]
        assert not app.state.db.all('PRAGMA foreign_key_check')


def test_failed_or_busy_switch_preserves_last_snapshot(setup):
    app,client,state,engine,built=setup
    first=connect(client,engine());built(first)
    port=engine(name='另一报表',path=r'F:\另一报表.pbix',port=61111,pid=77)
    before=app.state.db.all('SELECT * FROM models')
    app.state.semantics.jobs[first['id']]=True
    try:assert client.post('/api/models/connect',json={'port':port}).status_code==409
    finally:app.state.semantics.jobs.clear()
    del state['contexts'][port]
    assert client.post('/api/models/connect',json={'port':port}).status_code==422
    assert app.state.db.all('SELECT * FROM models')==before
    assert not app.state.db.all('SELECT * FROM released_snapshots')


def test_next_connection_compacts_preexisting_snapshots_without_losing_legacy_dictionary_sources(setup):
    app,client,state,engine,built=setup
    first=connect(client,engine());built(first)
    model_id=first['id']
    app.state.db.execute("UPDATE model_semantics SET payload=json_remove(payload,'$.source_snapshot') WHERE model_id=?",(model_id,))
    extra=app.state.powerbi.save('升级前的快照','desktop',50101,context())
    second=connect(client,engine(name='另一报表',path=r'F:\另一报表.pbix',port=61111,pid=77))
    assert app.state.db.all("SELECT id FROM models WHERE context!='null'")==[{'id':second['id']}]
    assert {r['model_id'] for r in app.state.db.all('SELECT model_id FROM released_snapshots')}=={model_id,extra['id']}
    profile=json.loads(app.state.db.one('SELECT payload FROM model_semantics WHERE model_id=?',(model_id,))['payload'])
    assert profile['source_snapshot']['table_queries']==first['context']['table_queries']
    changed=context();changed.table_queries['Sales']='let Source = 2 in Source'
    again=connect(client,engine(port=62222,pid=88,ctx=changed))
    assert again['id']!=model_id and again['semantics']['pending']>0
    assert app.state.semantics.get(again['id'])['dictionary_id']==model_id
