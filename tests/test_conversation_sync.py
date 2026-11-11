import json
import threading
from dataclasses import asdict

import pytest
from fastapi import HTTPException

from backend.app import create_app
from backend.database import encode, now, uid
from backend.engine.context.base import Column
from backend.engine.core.artifact import ValidationResult
from backend.engine.context.live_writer import WriteResult
from tests.test_report_reuse import setup, connect, context
from tests.test_analysis import Provider, plan, result


def conversation(client,model,mode='dax'):
    return client.post('/api/conversations',json={'model_id':model['id'],'mode':mode,'title':'继续原对话'}).json()


def message(app,cid,role='assistant',content='同步前的回复'):
    mid=uid()
    app.state.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(mid,cid,role,content,'complete',now()))
    return mid


def sync(client,cid):
    response=client.post(f'/api/conversations/{cid}/refresh-model',json={})
    assert response.status_code==200,response.text
    return response.json()


def test_sync_keeps_conversation_messages_artifacts_results_images_and_other_conversation(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    conv=conversation(client,old);other=conversation(client,old)
    user=message(app,conv['id'],'user','之前的需求');answer=message(app,conv['id'])
    aid,qid,picture=uid(),uid(),uid()
    app.state.db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)',
        (aid,answer,old['id'],'dax_measure','Generated','1','Sales',encode(asdict(ValidationResult(ok=True,run_verified=True))),None,now()))
    app.state.db.execute('INSERT INTO analysis_queries VALUES (?,?,?,?,?,?,?)',
        (qid,answer,old['id'],'旧查询','EVALUATE ROW("Value",1)',encode({**result(),'label':'Q1','title':'旧查询',
            'attempt':0,'dax':'EVALUATE ROW("Value",1)','executed_at':now()}),now()))
    app.state.db.execute('INSERT INTO message_images VALUES (?,?,?,?,?,?,?,?,?,?)',
        (picture,user,'截图.png','image/png',2,2,4,b'test',now(),0))
    tables=('messages','artifacts','analysis_queries','message_images')
    saved={t:app.state.db.all(f'SELECT * FROM {t}') for t in tables}
    changed=context();changed.tables['Sales'].append(Column('New Field','string','Sales'))
    engine(ctx=changed,port=60001,pid=77)
    refreshed=sync(client,conv['id']);new=refreshed['model']
    assert refreshed['conversation']['id']==conv['id'] and refreshed['conversation']['title']==conv['title']
    assert refreshed['conversation']['model_id']==new['id']!=old['id']
    assert app.state.chat.conversation(other['id'])['model_id']==old['id']
    assert len(app.state.db.all('SELECT * FROM conversations'))==2
    assert all(app.state.db.all(f'SELECT * FROM {t}')==saved[t] for t in tables)
    assert new['port']==60001 and new['semantics']['can_answer'] and new['semantics']['pending']==2
    assert app.state.powerbi.get(old['id'])['context']==old['context']
    rows=client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert not rows[0]['model_stale'] and rows[1]['model_stale']
    assert rows[0]['images'][0]['id']==picture and rows[1]['analysis'][0]['rows']==result()['rows']
    exported=client.get(f"/api/conversations/{conv['id']}/export").text
    assert '同步前' in exported and '同步前的回复' in exported
    restarted=create_app(app.state.db.path.parent)
    assert restarted.state.chat.conversation(conv['id'])['model_id']==new['id']
    assert restarted.state.chat.messages(conv['id'])[1]['model_stale']


@pytest.mark.parametrize('mode',['dax','analysis'])
def test_followup_uses_new_model_and_preserves_prior_history(setup,monkeypatch,mode):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old);conv=conversation(client,old,mode)
    message(app,conv['id'],'user','之前的需求');before=message(app,conv['id'])
    changed=context();changed.tables['Sales'].append(Column('New Field','string','Sales'))
    engine(ctx=changed,port=60001,pid=77)
    refreshed=sync(client,conv['id'])
    provider=Provider(['按新结构继续回答。'] if mode=='dax' else [plan(), '当前重新查询的结果为125。[Q1]'])
    monkeypatch.setattr(app.state.settings,'provider',lambda *a:provider)
    queries=[]
    if mode=='analysis':
        def execute(model,*args,**kwargs):queries.append(model['id']);return result()
        monkeypatch.setattr(app.state.chat.analysis,'execute',execute)
    r=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'继续分析'})
    assert r.status_code==200,r.text
    events=list(map(json.loads,r.text.splitlines()))
    assert not any(e['type']=='error' for e in events),events
    assert next(e for e in events if e['type']=='model')['model_id']==refreshed['model']['id']
    assert 'New Field' in provider.calls[0][0] and '模型同步提示' in provider.calls[0][0]
    assert any(m.content=='同步前的回复' for m in provider.calls[0][1])
    if mode=='analysis':assert queries==[refreshed['model']['id']]
    rows=client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert next(m for m in rows if m['id']==before)['model_stale']
    assert rows[-1]['role']=='assistant' and not rows[-1]['model_stale']


def test_only_current_generating_conversation_blocks_sync(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old);conv=conversation(client,old);other=conversation(client,old)
    lock=threading.Lock();lock.acquire();app.state.chat.locks[conv['id']]=lock
    count=len(app.state.db.all('SELECT * FROM models'))
    try:
        r=client.post(f"/api/conversations/{conv['id']}/refresh-model",json={})
        assert r.status_code==409 and len(app.state.db.all('SELECT * FROM models'))==count
        refreshed=sync(client,other['id'])
        assert refreshed['conversation']['id']==other['id'] and lock.locked()
        assert app.state.chat.conversation(conv['id'])['model_id']==old['id']
    finally:lock.release()


def test_failed_sync_keeps_original_binding_and_releases_operation_lock(setup,monkeypatch):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old);conv=conversation(client,old);mid=message(app,conv['id'])
    def failed(*args):
        status=app.state.chat.generation_status(conv['id'])
        assert status['syncing'] and not status['running']
        raise HTTPException(409,'暂时无法读取模型')
    monkeypatch.setattr(app.state.powerbi,'refresh',failed)
    r=client.post(f"/api/conversations/{conv['id']}/refresh-model",json={})
    assert r.status_code==409
    assert app.state.chat.conversation(conv['id'])['model_id']==old['id']
    assert not app.state.chat.model_sync(conv['id'])
    assert not app.state.chat.locks[conv['id']].locked() and not app.state.chat.syncing
    assert not app.state.chat.messages(conv['id'])[0]['model_stale']


def test_sync_invalidates_only_deleted_or_changed_field_bindings(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old);conv=conversation(client,old)
    for literal,table,column in [('keep','Sales','Code'),('changed','Dim','Code'),('deleted','Sales','Amount'),('ignore',None,None)]:
        app.state.db.execute('INSERT INTO conversation_bindings VALUES (?,?,?,?,?)',(conv['id'],literal,table,column,now()))
    changed=context();changed.tables['Sales']=changed.tables['Sales'][1:]
    state['metadata'][('column','Dim','Code')]={'description':'新的业务定义'}
    engine(ctx=changed,port=60001,pid=77);sync(client,conv['id'])
    assert {b['literal'] for b in app.state.semantics.bindings(conv['id'])}=={'keep','ignore'}


def test_old_artifact_revalidates_and_writes_against_current_conversation_model(setup,monkeypatch):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old);conv=conversation(client,old);mid=message(app,conv['id']);aid=uid()
    app.state.db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)',
        (aid,mid,old['id'],'dax_measure','Generated','1','Sales',encode(asdict(ValidationResult(ok=True,run_verified=True))),None,now()))
    engine(port=60001,pid=77);new=sync(client,conv['id'])['model']
    ports=[]
    class Evaluator:
        def __init__(self,port):ports.append(port)
        def evaluate_with_defines(self,*args):return ValidationResult(ok=True,run_verified=True,sample=1)
    class Writer:
        def __init__(self,port):ports.append(port)
        def write_measure(self,*args,**kwargs):return WriteResult(True,'测试写入成功')
    monkeypatch.setattr('backend.app.LiveDesktopEvaluator',Evaluator)
    monkeypatch.setattr('backend.chat.LiveDesktopEvaluator',Evaluator)
    monkeypatch.setattr('backend.app.LiveDesktopWriter',Writer)
    monkeypatch.setattr(app.state.powerbi,'update_after_write',lambda *a:None)
    response=client.post(f'/api/messages/{mid}/validate',json={})
    assert response.status_code==200 and response.json()['artifacts'][0]['model_id']==new['id']
    # Even an artifact still carrying its generation snapshot must use the current binding.
    app.state.db.execute('UPDATE artifacts SET model_id=? WHERE id=?',(old['id'],aid))
    response=client.post(f'/api/artifacts/{aid}/apply',json={})
    assert response.status_code==200,response.text
    assert ports==[60001,60001,60001]
    assert app.state.db.one('SELECT model_id FROM artifacts WHERE id=?',(aid,))['model_id']==new['id']


def test_old_clarification_cannot_submit_after_sync(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old);conv=conversation(client,old);mid=message(app,conv['id'])
    sync(client,conv['id'])
    r=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'旧口径选择',
        'clarification_reply':{'message_id':mid,'option_id':'A'}})
    assert r.status_code==409 and '旧口径问题' in r.json()['detail']
    assert not app.state.chat.generation_status(conv['id'])['running']


def test_repeated_sync_preserves_ids_and_moves_history_cutoff(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old);conv=conversation(client,old);first=message(app,conv['id'])
    sync(client,conv['id']);second=message(app,conv['id'],content='第一次同步后的回复')
    assert not app.state.chat.messages(conv['id'])[-1]['model_stale']
    sync(client,conv['id']);third=message(app,conv['id'],content='第二次同步后的回复')
    rows=app.state.chat.messages(conv['id'])
    assert [m['id'] for m in rows]==[first,second,third]
    assert [m['model_stale'] for m in rows]==[True,True,False]
    assert len(app.state.db.all('SELECT * FROM conversation_model_syncs'))==2
    assert client.delete(f"/api/conversations/{conv['id']}").status_code==200
    assert not app.state.db.all('SELECT * FROM conversation_model_syncs')
