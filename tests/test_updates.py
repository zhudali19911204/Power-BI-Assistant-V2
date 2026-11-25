import json
from pathlib import Path
import threading
from unittest.mock import Mock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from backend import updates
from backend.app import create_app
from backend.releases import APP_VERSION, MANIFEST_LIMIT, UpdateError, verified_release, version_number
from scripts.sign_update import sign_installer


@pytest.fixture
def channel(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    installer = tmp_path / "PowerBIAssistant-Setup-2.10.0.exe"
    installer.write_bytes(b"sample installer")
    feed = "https://publisher.test/latest.json"
    url = "https://publisher.test/installer.exe"
    files = {feed: sign_installer(installer, key, url=url, notes="更新说明"), url: installer.read_bytes()}
    service = updates.DesktopUpdates("2.9.9", feed, key.public_key(), tmp_path / "cache",
                                     tmp_path / "custom dir" / "PowerBIAssistant.exe", Mock())
    requests = []
    def respond(request):
        requests.append(str(request.url))
        result = files[str(request.url)]
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, stream=httpx.ByteStream(result))
    client = httpx.Client
    monkeypatch.setattr(updates.httpx, "Client", lambda **kwargs: client(
        transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(updates, "enable_os_trust_store", lambda: True)
    return service, key, files, url, requests


def test_signed_release_numeric_order_and_tampering(channel):
    service, key, files, _, _ = channel
    assert service.check()["notes"] == "更新说明"
    for installed in ("2.10.0.0", "2.10.1", "3.0.0"):
        service.current = version_number(installed)
        assert service.check() is None
        assert service.candidate is None
    envelope = json.loads(files[service.feed])
    for field, value in (("version", "9.0.0"), ("url", "https://other.test/file"),
                         ("sha256", "0" * 64), ("size", 1), ("notes", "changed")):
        altered = {**envelope, "release": {**envelope["release"], field: value}}
        with pytest.raises(UpdateError, match="签名"):
            verified_release(json.dumps(altered).encode(), key.public_key())
    with pytest.raises(UpdateError, match="签名"):
        verified_release(files[service.feed], Ed25519PrivateKey.generate().public_key())


def test_redirects_limits_and_unavailable_feed(channel):
    service, _, files, _, requests = channel
    files["https://cdn.test/manifest"] = files[service.feed]
    files[service.feed] = httpx.Response(302, headers={"Location": "https://cdn.test/manifest"})
    assert service.check()["version"] == "2.10.0"
    files[service.feed] = httpx.Response(302, headers={"Location": "http://cdn.test/file"})
    with pytest.raises(UpdateError, match="HTTPS"):
        service.check()
    assert "http://cdn.test/file" not in requests
    assert service.candidate is None
    files[service.feed] = b"x" * (MANIFEST_LIMIT + 1)
    with pytest.raises(UpdateError, match="大小"):
        service.check()
    files[service.feed] = httpx.Response(404)
    with pytest.raises(UpdateError, match="服务器"):
        service.check()


def test_download_retry_cache_integrity_and_handoff(channel, monkeypatch):
    service, _, files, url, requests = channel
    digest = service.check()["sha256"]
    original = files[url]
    launch = Mock()
    monkeypatch.setattr(updates.subprocess, "Popen", launch)
    with pytest.raises(UpdateError):
        service.download("unselected")
    for bad in (original[:-1], b"x" * len(original), original + b"x"):
        files[url] = bad
        with pytest.raises(UpdateError):
            service.download(digest)
        assert not list(service.cache.iterdir())
    files[url] = original
    service.download(digest)
    cached, = service.cache.iterdir()
    retrieved = requests.count(url)
    service.download(digest)
    assert requests.count(url) == retrieved
    cached.write_bytes(b"y" * len(original))
    with pytest.raises(UpdateError, match="已变更"):
        service.launch(digest)
    launch.assert_not_called()
    service.download(digest)
    service.launch(digest)
    args, kwargs = launch.call_args
    assert Path(args[0][0]) == cached
    assert f"/LICENSEFILE={service.executable.with_name('license.key')}" in args[0]
    assert f"/DIR={service.executable.parent}" in args[0]
    assert any(arg.startswith("/PBIUPDATEPID=") for arg in args[0])
    assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    service.clear_installed_packages()
    assert cached.exists()
    service.current = version_number("2.10.0")
    service.clear_installed_packages()
    assert not cached.exists()


def test_desktop_routes_require_idle_and_verified_installer(channel, tmp_path, monkeypatch):
    service, _, _, _, _ = channel
    app = create_app(tmp_path / "profile")
    with TestClient(app) as client:
        post = lambda path: client.post("/api/updates/" + path, json={})
        assert client.get("/api/updates").json() == {"enabled": False, "version": APP_VERSION}
        assert post("check").status_code == 409
        app.state.updates = service
        digest = post("check").json()["release"]["sha256"]
        apply = digest + "/install"
        assert post(apply).status_code == 503
        assert post(digest + "/download").status_code == 200
        lock = app.state.chat.locks["generating"] = threading.Lock()
        with lock:
            assert post(apply).status_code == 409
        app.state.semantics.jobs["building"] = True
        assert post(apply).status_code == 409
        app.state.semantics.jobs.clear()
        service.close_window.assert_not_called()
        monkeypatch.setattr(updates.subprocess, "Popen", Mock())
        assert post(apply).status_code == 200
        service.close_window.assert_called_once()
        assert post(apply).status_code == 409
        assert client.post("/api/models/demo", json={}).status_code == 503
