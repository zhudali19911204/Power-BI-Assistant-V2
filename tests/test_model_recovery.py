from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app import create_app
from backend.database import Database
from backend.engine.context.base import Column,ModelContext
from backend.powerbi import PowerBIService


def context(name='Amount'):
    return ModelContext(tables={'Sales':[Column(name,'double','Sales')]})


def mock_engine(monkeypatch,ctx,port=51413):
    monkeypatch.setattr('backend.powerbi.find_instances',lambda:[SimpleNamespace(port=port)])
    class Source:
        def __init__(self,port):pass
        def load(self):return ctx
    monkeypatch.setattr('backend.powerbi.LiveDesktopSource',Source)


def test_restart_restores_desktop_only_after_real_context_match(tmp_path,monkeypatch):
    db=Database(tmp_path/'test.sqlite3')
    original=PowerBIService(db).save('Desktop','desktop',51413,context())
    restored=PowerBIService(db)
    assert not restored.get(original['id'])['live']
    mock_engine(monkeypatch,context())
    model,current=restored.require_live(original['id'],unchanged=True)
    assert model['live']
    assert restored.get(original['id'])['live']
    assert current.tables['Sales'][0].name=='Amount'


def test_reconnecting_same_engine_restores_unchanged_historical_conversations(tmp_path,monkeypatch):
    service=PowerBIService(Database(tmp_path/'test.sqlite3'))
    old=service.save('old','desktop',51413,context())
    other=service.save('changed','desktop',51413,context('Other'))
    service.live_ids.clear()
    mock_engine(monkeypatch,context())
    new=service.connect(51413)
    assert new['live'] and service.get(old['id'])['live']
    assert not service.get(other['id'])['live']


def test_reused_port_with_different_model_cannot_restore_a_snapshot(tmp_path,monkeypatch):
    service=PowerBIService(Database(tmp_path/'test.sqlite3'))
    old=service.save('old','desktop',51413,context())
    service.live_ids.clear()
    mock_engine(monkeypatch,context('DifferentModelColumn'))
    with pytest.raises(HTTPException,match='模型结构已变化'):
        service.require_live(old['id'],unchanged=True)
    assert not service.get(old['id'])['live']


def test_disconnected_desktop_does_not_silently_skip_execution(tmp_path,monkeypatch):
    app=create_app(tmp_path)
    model=app.state.powerbi.save('Desktop','desktop',51413,context())
    from backend.database import uid,now
    from backend.schemas import MessageInput
    conv_id=uid();stamp=now()
    app.state.db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?)',(
        conv_id,'conversation','dax',model['id'],'',stamp,stamp))
    monkeypatch.setattr('backend.powerbi.find_instances',lambda:[])
    def unexpected_provider():raise AssertionError('No LLM request should start when Desktop is disconnected')
    monkeypatch.setattr(app.state.settings,'provider',unexpected_provider)
    with pytest.raises(HTTPException,match='连接已断开'):
        app.state.chat.begin(conv_id,MessageInput(content='写一个新度量'),app.state.settings)
    assert not app.state.db.all('SELECT * FROM messages')
