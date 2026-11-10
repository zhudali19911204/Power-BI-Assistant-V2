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
            self.client_options = opts
            return self

    monkeypatch.setattr('backend.settings.OpenAICompatProvider', Provider)
    app.state.settings.provider(second['id'])
    assert calls[-1] == ('second-key', 'deepseek', 'https://inference.example/v1',
                        {'enable_thinking': None, 'reasoning_level': 'low'})
    app.state.settings.provider()
    assert calls[-1][0] == 'private-qwen-key' and calls[-1][3]['enable_thinking'] is False
    client.post(f"/api/llm/profiles/{second['id']}/activate", json={})
    app.state.settings.provider()
    assert calls[-1][0] == 'second-key'


def test_profile_timeouts_persist_independently_and_control_the_sdk(client, app, tmp_path, monkeypatch):
    first = add(client)
    second = add(client, 'slow-model', 'second-key', idle_timeout=180, generation_timeout=1800)
    assert first['idle_timeout'] == 90 and first['generation_timeout'] == 900
    assert second['idle_timeout'] == 180 and second['generation_timeout'] == 1800
    assert create_app(tmp_path).state.settings.profiles()[1]['generation_timeout'] == 1800
    # Old clients editing just the model must not reset previously saved timeouts.
    edited = client.put(f"/api/llm/profiles/{second['id']}", json={
        'model': 'slow-renamed', 'base_url': 'http://192.168.1.100:8000/v1'}).json()
    assert edited['idle_timeout'] == 180 and edited['generation_timeout'] == 1800

    class Provider:
        def __init__(self, *args, **kwargs): self._client = self
        def with_options(self, **kwargs): self.options = kwargs; return self

    monkeypatch.setattr('backend.settings.OpenAICompatProvider', Provider)
    provider = app.state.settings.provider(second['id'])
    assert provider.idle_timeout == 180 and provider.generation_timeout == 1800
    assert provider.options['timeout'].read == 185
    assert provider.options['max_retries'] == 0
    assert app.state.settings.public()['generation_timeout'] == 900


def test_legacy_profile_without_timeout_fields_uses_new_defaults(client, app):
    saved = add(client)
    cfg = config(app, saved['id'])
    cfg.pop('idle_timeout'); cfg.pop('generation_timeout')
    app.state.db.execute('UPDATE llm_profiles SET config=? WHERE id=?', (encode(cfg), saved['id']))
    public = app.state.settings.profiles()[0]
    assert public['idle_timeout'] == 90 and public['generation_timeout'] == 900


def test_output_budget_is_independent_persistent_and_retained_by_old_clients(client, app, tmp_path, monkeypatch):
    first = add(client)
    second = add(client, 'reasoning-model', 'separate-key', max_tokens=32000)
    assert first['max_tokens'] == 16000 and second['max_tokens'] == 32000
    assert create_app(tmp_path).state.settings.profiles()[1]['max_tokens'] == 32000
    updated = client.put(f"/api/llm/profiles/{second['id']}", json={
        'model': 'renamed', 'base_url': 'https://inference.example/v1'}).json()
    assert updated['max_tokens'] == 32000

    class Provider:
        def __init__(self, *args, **kwargs): self._client = self
        def with_options(self, **kwargs): return self
    monkeypatch.setattr('backend.settings.OpenAICompatProvider', Provider)
    selected = app.state.settings.provider(second['id'])
    assert selected.max_tokens == 32000 and selected.profile_id == second['id']
    assert app.state.settings.public()['max_tokens'] == 16000
    cfg = config(app, first['id']); cfg.pop('max_tokens')
    app.state.db.execute('UPDATE llm_profiles SET config=? WHERE id=?', (encode(cfg), first['id']))
    assert app.state.settings.profiles()[0]['max_tokens'] == 16000


@pytest.mark.parametrize('budget', [0, 255, 131073, 1000.5, 'invalid'])
def test_invalid_output_budget_keeps_saved_profile(client, budget):
    first = add(client, max_tokens=32000)
    response = client.put(f"/api/llm/profiles/{first['id']}", json={'model': 'qwen', 'max_tokens': budget})
    assert response.status_code == 422
    assert client.get('/api/llm/profiles').json()[0]['max_tokens'] == 32000


@pytest.mark.parametrize('extra', [
    {'idle_timeout': 0}, {'idle_timeout': 601}, {'generation_timeout': 179},
    {'generation_timeout': 7201}, {'idle_timeout': 300, 'generation_timeout': 180},
])
def test_invalid_timeouts_do_not_change_saved_profiles(client, extra):
    first = add(client)
    response = client.put(f"/api/llm/profiles/{first['id']}", json={'model': 'qwen', **extra})
    assert response.status_code == 422
    profile = client.get('/api/llm/profiles').json()[0]
    assert profile['idle_timeout'] == 90 and profile['generation_timeout'] == 900


def test_environment_defaults_are_imported_as_independent_profile(tmp_path, monkeypatch):
    monkeypatch.setenv('PBI_LLM_MODEL', 'environment-model')
    monkeypatch.setenv('PBI_LLM_BASE_URL', 'https://environment.example/v1')
    monkeypatch.setenv('PBI_LLM_API_KEY', 'environment-key')
    service = create_app(tmp_path).state.settings
    assert len(service.profiles()) == 1
    assert service.key(service.raw()) == 'environment-key'
    monkeypatch.setenv('PBI_LLM_API_KEY', 'changed-env-key')
    assert service.key(service.raw()) == 'environment-key'


def test_reasoning_is_low_by_default_and_card_changes_are_independent(client, app, tmp_path):
    url = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
    first = add(client, 'qwen3.8-max', base_url=url, max_tokens=32000)
    second = add(client, 'glm-5.3', 'separate-key', base_url=url)
    assert first['reasoning_level'] == second['reasoning_level'] == 'low'
    before = config(app, second['id'])
    changed = client.patch(f"/api/llm/profiles/{second['id']}/reasoning",
                           json={'reasoning_level': 'high'})
    assert changed.status_code == 200
    assert changed.json()['reasoning_level'] == 'high'
    assert not changed.json()['active']
    assert client.get('/api/settings').json()['active_profile_id'] == first['id']
    after = config(app, second['id'])
    assert after == {**before, 'reasoning_level': 'high', 'enable_thinking': None}
    assert app.state.settings.key(after) == 'separate-key'
    assert config(app, first['id'])['max_tokens'] == 32000
    edited = client.put(f"/api/llm/profiles/{second['id']}", json={
        'name': '更名', 'model': 'glm-5.3', 'base_url': url}).json()
    assert edited['reasoning_level'] == 'high'
    restarted = create_app(tmp_path).state.settings
    assert next(p for p in restarted.profiles() if p['id'] == second['id'])['reasoning_level'] == 'high'
    client.post(f"/api/llm/profiles/{second['id']}/activate", json={})
    assert client.get('/api/settings').json()['reasoning_level'] == 'high'
    client.patch(f"/api/llm/profiles/{second['id']}/reasoning", json={'reasoning_level': 'low'})
    assert client.get('/api/settings').json()['reasoning_level'] == 'low'


def test_existing_profiles_migrate_once_to_low_without_changing_keys_or_selection(client, app, tmp_path):
    first = add(client, 'qwen3.8-max', enable_thinking=True)
    second = add(client, 'glm-5.3', 'second-key')
    originals = {}
    for profile in (first, second):
        cfg = config(app, profile['id'])
        cfg.pop('reasoning_level')
        originals[profile['id']] = cfg
        app.state.db.execute('UPDATE llm_profiles SET config=? WHERE id=?', (encode(cfg), profile['id']))
    restarted = create_app(tmp_path).state.settings
    for profile in restarted.profiles():
        assert profile['reasoning_level'] == 'low'
        assert config(app, profile['id']) == {**originals[profile['id']], 'reasoning_level': 'low'}
    assert restarted.public()['active_profile_id'] == first['id']
    assert restarted.public()['reasoning_level'] == 'low'


@pytest.mark.parametrize('level', ['medium', '', None, True, 1])
def test_invalid_reasoning_does_not_change_config(client, app, level):
    saved = add(client, 'qwen3.8-max', base_url='https://dashscope.aliyuncs.com/v1')
    before = config(app, saved['id'])
    response = client.patch(f"/api/llm/profiles/{saved['id']}/reasoning", json={'reasoning_level': level})
    assert response.status_code == 422
    assert config(app, saved['id']) == before


def test_unverified_reasoning_rejected_without_touching_profile(client, app):
    saved = add(client, 'unknown-model')
    before = config(app, saved['id'])
    assert not saved['reasoning_control']['supported']
    response = client.patch(f"/api/llm/profiles/{saved['id']}/reasoning", json={'reasoning_level': 'high'})
    assert response.status_code == 409
    assert config(app, saved['id']) == before
    assert client.patch('/api/llm/profiles/missing/reasoning', json={'reasoning_level': 'low'}).status_code == 404


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
