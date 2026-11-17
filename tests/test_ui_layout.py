import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app


def test_layout_survives_restart_port_change_and_model_settings(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://localhost:51001') as client:
        assert client.get('/api/ui/layout').json() == {'context_width': None, 'sidebar_width': None}
        assert client.put('/api/ui/layout', json={'context_width': 412}).json() == {'context_width': 412, 'sidebar_width': None}
        assert client.put('/api/ui/layout', json={'sidebar_width': 330}).json() == {'context_width': 412, 'sidebar_width': 330}
        assert client.put('/api/settings', json={'model': 'local-test', 'api_key': 'test-only-key'}).status_code == 200
        assert client.get('/api/ui/layout').json() == {'context_width': 412, 'sidebar_width': 330}
    with TestClient(create_app(tmp_path), base_url='http://localhost:51002') as client:
        assert client.get('/api/ui/layout').json() == {'context_width': 412, 'sidebar_width': 330}
        assert client.put('/api/ui/layout', json={'context_width': None}).status_code == 200
        assert client.get('/api/ui/layout').json() == {'context_width': None, 'sidebar_width': 330}
        assert client.put('/api/ui/layout', json={'sidebar_width': None}).json() == {'context_width': None, 'sidebar_width': None}
        assert client.get('/api/settings').json()['model'] == 'local-test'
        assert client.get('/api/settings').json()['has_key']


@pytest.mark.parametrize('width', [0, 239, 521, 300.5, '300', True])
def test_invalid_layout_does_not_replace_saved_width(tmp_path, width):
    with TestClient(create_app(tmp_path)) as client:
        assert client.put('/api/ui/layout', json={'context_width': 300}).status_code == 200
        assert client.put('/api/ui/layout', json={'context_width': width}).status_code == 422
        assert client.get('/api/ui/layout').json() == {'context_width': 300, 'sidebar_width': None}


def test_layout_uses_existing_local_request_protection(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.put('/api/ui/layout', json={'context_width': 300},
                          headers={'Origin': 'https://attacker.example'}).status_code == 403
        assert client.put('/api/ui/layout', content='{"context_width":300}',
                          headers={'Content-Type': 'text/plain'}).status_code == 415
        assert client.get('/api/ui/layout').json() == {'context_width': None, 'sidebar_width': None}


@pytest.mark.parametrize('width', [0, 214, 401, 280.5, '280', True])
def test_invalid_sidebar_width_preserves_both_panels(tmp_path, width):
    with TestClient(create_app(tmp_path)) as client:
        original = {'sidebar_width': 300, 'context_width': 400}
        assert client.put('/api/ui/layout', json=original).status_code == 200
        assert client.put('/api/ui/layout', json={'sidebar_width': width}).status_code == 422
        assert client.get('/api/ui/layout').json() == original


def test_upgrade_previous_layout_database_keeps_model_panel_width(tmp_path):
    with sqlite3.connect(tmp_path / 'assistant.sqlite3') as connection:
        connection.execute('CREATE TABLE ui_layout (id INTEGER PRIMARY KEY,context_width INTEGER)')
        connection.execute('INSERT INTO ui_layout VALUES (1,390)')
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/ui/layout').json() == {'context_width': 390, 'sidebar_width': None}
        assert client.put('/api/ui/layout', json={'sidebar_width': 310}).json() == {'context_width': 390, 'sidebar_width': 310}
        assert client.put('/api/ui/layout', json={}).json() == {'context_width': 390, 'sidebar_width': 310}
