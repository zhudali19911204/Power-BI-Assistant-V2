import json

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import Database, encode
from backend.settings import SettingsService


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def add(client, model='qwen', key='private-qwen-key', **extra):
    response = client.post('/api/llm/profiles', json={
        'model': model, 'base_url': 'http://192.168.1.100:8000/v1', 'api_key': key, **extra,
    })
    assert response.status_code == 201, response.text
    assert key not in response.text if key else True
    return response.json()


def config(app, profile_id):
    return json.loads(app.state.db.one('SELECT config FROM llm_profiles WHERE id=?', (profile_id,))['config'])


def test_migrate_existing_config_once_without_changing_key_or_thinking(tmp_path):
    db = Database(tmp_path / 'assistant.sqlite3')
    old = SettingsService(db, tmp_path)
    ciphertext = old.cipher.encrypt(b'legacy-private-key').decode()
    cfg = {'provider': 'openai_compat', 'model': 'legacy-qwen',
           'base_url': 'http://10.1.2.3:8080/v1', 'enable_thinking': False, 'encrypted_key': ciphertext}
    db.execute('INSERT INTO settings VALUES (1,?)', (encode(cfg),))
    for _ in range(2):
        migrated = create_app(tmp_path)
        service = migrated.state.settings
        profiles = service.profiles()
        assert len(profiles) == 1
        assert profiles[0]['model'] == 'legacy-qwen' and profiles[0]['active']
        assert service.key(service.raw()) == 'legacy-private-key'
        assert service.raw()['encrypted_key'] == ciphertext
        assert service.raw()['enable_thinking'] is False
        assert 'encrypted_key' not in profiles[0] and 'api_key' not in profiles[0]


def test_independent_profiles_switch_and_survive_restart(client, app, tmp_path):
    first = add(client, enable_thinking=False)
    second = add(client, 'deepseek', 'private-deepseek-key', name='云端模型',
                 base_url='https://inference.example/v1/')
    assert first['active'] and not second['active']
    assert second['enable_thinking'] is None
    assert client.get('/api/settings').json()['active_profile_id'] == first['id']
    result = client.post(f"/api/llm/profiles/{second['id']}/activate", json={}).json()
    assert result['model'] == 'deepseek' and result['base_url'] == 'https://inference.example/v1'
    assert result['name'] == '云端模型'
    assert app.state.settings.key(app.state.settings.raw()) == 'private-deepseek-key'
    restarted = create_app(tmp_path)
    assert restarted.state.settings.public()['active_profile_id'] == second['id']
    client.post(f"/api/llm/profiles/{first['id']}/activate", json={})
    assert app.state.settings.key(app.state.settings.raw()) == 'private-qwen-key'
    assert app.state.settings.raw()['enable_thinking'] is False
    assert len(client.get('/api/llm/profiles').json()) == 2
    for stored in app.state.db.all('SELECT config FROM llm_profiles'):
        assert 'private-qwen-key' not in stored['config']
        assert 'private-deepseek-key' not in stored['config']


def test_edit_preserves_only_its_own_key_and_does_not_switch(client, app):
    first = add(client)
    second = add(client, 'deepseek', 'private-deepseek-key')
    response = client.put(f"/api/llm/profiles/{second['id']}", json={
        'model': 'deepseek-new', 'name': '修改名称', 'base_url': 'https://new.example/v1', 'api_key': '',
    })
    assert response.status_code == 200
    assert response.json()['name'] == '修改名称'
    assert app.state.settings.key(config(app, second['id'])) == 'private-deepseek-key'
    assert client.get('/api/settings').json()['active_profile_id'] == first['id']
    assert app.state.settings.key(app.state.settings.raw()) == 'private-qwen-key'
    client.put(f"/api/llm/profiles/{first['id']}", json={'model': 'qwen-renamed', 'base_url': 'http://localhost/v1'})
    assert client.get('/api/settings').json()['model'] == 'qwen-renamed'


def test_new_blank_key_and_explicit_clear_never_borrow_environment_key(client, app, monkeypatch, tmp_path):
    first = add(client)
    monkeypatch.setenv('PBI_LLM_API_KEY', 'environment-key')
    second = add(client, 'keyless', '')
    assert not second['has_key'] and not second['ready']
    client.put(f"/api/llm/profiles/{first['id']}", json={'model': 'qwen', 'clear_key': True})
    assert app.state.settings.key(config(app, first['id'])) == ''
    assert not client.get('/api/settings').json()['has_key']
    client.delete(f"/api/llm/profiles/{first['id']}")
    client.delete(f"/api/llm/profiles/{second['id']}")
    assert not client.get('/api/settings').json()['ready']
    assert not create_app(tmp_path).state.settings.public()['has_key']
    assert client.get('/api/llm/profiles').json() == []


def test_deleting_inactive_keeps_selection_deleting_active_selects_remaining(client):
    first = add(client)
    second = add(client, 'deepseek', 'second-key')
    third = add(client, 'another', 'third-key')
    assert client.delete(f"/api/llm/profiles/{third['id']}").json()['active_profile_id'] == first['id']
    assert client.delete(f"/api/llm/profiles/{first['id']}").json()['active_profile_id'] == second['id']
    assert client.delete(f"/api/llm/profiles/{second['id']}").json()['active_profile_id'] is None


def test_profile_test_targets_requested_service_without_changing_active(client, app, monkeypatch):
    first = add(client)
    second = add(client, 'deepseek', 'second-key')
    called = []

    class Fake:
        def complete(self, *args, **kwargs):
            return 'OK'

    def provider(profile_id=None):
        called.append(profile_id)
        return Fake()

    monkeypatch.setattr(app.state.settings, 'provider', provider)
    assert client.post(f"/api/llm/profiles/{second['id']}/test", json={}).json()['ok']
    assert called == [second['id']]
    assert client.get('/api/settings').json()['active_profile_id'] == first['id']


def test_selected_provider_receives_its_own_credentials(client, app, monkeypatch):
    first = add(client, enable_thinking=False)
    second = add(client, 'deepseek', 'second-key', base_url='https://inference.example/v1')
    calls = []

    class Provider:
        def __init__(self, key, model, base_url, **opts):
            calls.append((key, model, base_url, opts))
            self._client = self

        def with_options(self, **opts):
            return self

    monkeypatch.setattr('backend.settings.OpenAICompatProvider', Provider)
    app.state.settings.provider(second['id'])
    assert calls[-1] == ('second-key', 'deepseek', 'https://inference.example/v1', {'enable_thinking': None})
    app.state.settings.provider()
    assert calls[-1][0] == 'private-qwen-key' and calls[-1][3]['enable_thinking'] is False
    client.post(f"/api/llm/profiles/{second['id']}/activate", json={})
    app.state.settings.provider()
    assert calls[-1][0] == 'second-key'


def test_environment_defaults_are_imported_as_independent_profile(tmp_path, monkeypatch):
    monkeypatch.setenv('PBI_LLM_MODEL', 'environment-model')
    monkeypatch.setenv('PBI_LLM_BASE_URL', 'https://environment.example/v1')
    monkeypatch.setenv('PBI_LLM_API_KEY', 'environment-key')
    service = create_app(tmp_path).state.settings
    assert len(service.profiles()) == 1
    assert service.key(service.raw()) == 'environment-key'
    monkeypatch.setenv('PBI_LLM_API_KEY', 'changed-env-key')
    assert service.key(service.raw()) == 'environment-key'


@pytest.mark.parametrize('body', [
    {'model': ' '}, {'model': 'test', 'base_url': 'file:///tmp/test'},
    {'model': 'test', 'base_url': 'https://user:secret@example.com/v1'},
])
def test_invalid_profile_does_not_affect_existing_configuration(client, body):
    first = add(client)
    assert client.post('/api/llm/profiles', json=body).status_code == 422
    assert len(client.get('/api/llm/profiles').json()) == 1
    assert client.get('/api/settings').json()['active_profile_id'] == first['id']


def test_missing_profile_returns_404_and_does_not_switch(client):
    first = add(client)
    assert client.post('/api/llm/profiles/missing/activate', json={}).status_code == 404
    assert client.post('/api/llm/profiles/missing/test', json={}).status_code == 404
    assert client.delete('/api/llm/profiles/missing').status_code == 404
    assert client.put('/api/llm/profiles/missing', json={'model': 'test'}).status_code == 404
    assert client.get('/api/settings').json()['active_profile_id'] == first['id']
