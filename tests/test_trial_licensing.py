import json
import sys
import threading
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from backend import licensing
from backend.app import create_app
from backend.powerbi import context_from_dict
from scripts import license_admin, license_helper

ISSUED = 1_800_000_000
GUID = "cb647bca-6326-499b-96f1-a649c5a891a8"


@pytest.fixture
def trial(monkeypatch, tmp_path):
    private = Ed25519PrivateKey.generate()
    code = licensing.machine_code_from_guid(GUID)
    key = license_admin.issue_license(private, code, license_type="trial", issued_at=ISSUED)
    clock = {"wall": float(ISSUED), "monotonic": 1000.0}
    monkeypatch.setattr(licensing, "time", SimpleNamespace(
        time=lambda: clock["wall"], monotonic=lambda: clock["monotonic"]))
    monkeypatch.setitem(sys.modules, "_license_trust",
                        SimpleNamespace(PUBLIC_KEY=private.public_key().public_bytes_raw()))
    monkeypatch.setattr(licensing, "machine_code", lambda: code)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PowerBIAssistant.exe"))
    (tmp_path / "license.key").write_text(key, encoding="utf-8-sig")
    return SimpleNamespace(private=private, code=code, key=key, clock=clock, path=tmp_path)


def test_same_machine_can_have_both_types_and_old_permanent_keys_are_unchanged(trial):
    permanent = license_admin.issue_license(trial.private, trial.code)
    fingerprint = licensing.decode_code(trial.code, "PBI2M", 16)
    legacy = licensing.encode_code("PBI2K", fingerprint + trial.private.sign(licensing.SIGNING_CONTEXT + fingerprint))
    assert permanent == legacy
    info = licensing.verify_license(permanent, trial.code, trial.private.public_key(), now=ISSUED + 100 * 365 * 86400)
    assert info.expires_at is None and info.summary == "永久授权"
    info = licensing.verify_license(trial.key, trial.code, trial.private.public_key())
    assert info.issued_at == ISSUED and info.expires_at == ISSUED + 604800
    assert "7 天试用" in info.summary and "UTC" in info.summary
    assert trial.key.startswith("PBI2T-") and len(trial.key) < 180
    licensing.verify_license(" \n" + trial.key.lower().replace("-", " -\n"), trial.code, trial.private.public_key())


@pytest.mark.parametrize("offset,allowed", [(-0.001, False), (0, True), (604799.999, True), (604800, False), (604801, False)])
def test_exact_seven_day_boundary(trial, offset, allowed):
    if allowed:
        licensing.verify_license(trial.key, trial.code, trial.private.public_key(), now=ISSUED + offset)
    else:
        with pytest.raises(licensing.LicenseError, match="签发时间|已到期"):
            licensing.verify_license(trial.key, trial.code, trial.private.public_key(), now=ISSUED + offset)


@pytest.mark.parametrize("index", [0, 15, 16, 23, 24, 87])
def test_tampering_machine_issue_time_or_signature_is_rejected(trial, index):
    raw = bytearray(licensing.decode_code(trial.key, "PBI2T", 88))
    raw[index] ^= 1
    key = licensing.encode_code("PBI2T", bytes(raw))
    with pytest.raises(licensing.LicenseError, match="无效"):
        licensing.verify_license(key, trial.code, trial.private.public_key())


def test_trial_cannot_be_converted_to_permanent_or_used_on_another_machine(trial):
    with pytest.raises(licensing.LicenseError):
        licensing.verify_license(trial.key.replace("PBI2T", "PBI2K"), trial.code, trial.private.public_key())
    raw = licensing.decode_code(trial.key, "PBI2T", 88)
    stripped = licensing.encode_code("PBI2K", raw[:16] + raw[-64:])
    with pytest.raises(licensing.LicenseError, match="无效"):
        licensing.verify_license(stripped, trial.code, trial.private.public_key())
    other = licensing.machine_code_from_guid("f79584ea-5c0b-4590-9bb4-292eb8550fba")
    with pytest.raises(licensing.LicenseError, match="不属于本机"):
        licensing.verify_license(trial.key, other, trial.private.public_key())
    with pytest.raises(licensing.LicenseError, match="无效"):
        licensing.verify_license(trial.key, trial.code, Ed25519PrivateKey.generate().public_key())


@pytest.mark.parametrize("stamp", [0, 2**64 - 1])
def test_signed_invalid_timestamp_is_rejected_without_overflow(trial, stamp):
    payload = licensing.decode_code(trial.code, "PBI2M", 16) + stamp.to_bytes(8, "big")
    key = licensing.encode_code("PBI2T", payload + trial.private.sign(licensing.TRIAL_SIGNING_CONTEXT + payload))
    with pytest.raises(licensing.LicenseError, match="签发时间无效"):
        licensing.verify_license(key, trial.code, trial.private.public_key())


@pytest.mark.parametrize("stamp", [-1, 0, True, 2.5, 2**64])
def test_issuer_rejects_invalid_trial_timestamp(trial, stamp):
    with pytest.raises(licensing.LicenseError):
        license_admin.issue_license(trial.private, trial.code, license_type="trial", issued_at=stamp)


def test_trial_clock_rollback_in_open_app_cannot_extend_or_restore_expiry(trial):
    guard = licensing.LicenseGuard(licensing.require_runtime_license())
    trial.clock.update(wall=ISSUED - 100, monotonic=1000 + 604799)
    guard.check()
    trial.clock["monotonic"] += 1
    with pytest.raises(licensing.LicenseError, match="已到期"):
        guard.check()
    trial.clock.update(wall=ISSUED, monotonic=1000)
    with pytest.raises(licensing.LicenseError, match="已到期"):
        guard.check()


def test_expired_key_cannot_start_or_reset_by_reinstalling_or_changing_data_dir(trial, monkeypatch):
    trial.clock["wall"] = ISSUED + 604800
    for folder in ("first", "reinstalled"):
        monkeypatch.setenv("PBI_DATA_DIR", str(trial.path / folder))
        with pytest.raises(licensing.LicenseError, match="已到期"):
            create_app()
        assert not (trial.path / folder).exists()
    permanent = license_admin.issue_license(trial.private, trial.code)
    (trial.path / "license.key").write_text(permanent, encoding="utf-8")
    app = create_app(trial.path / "upgraded")
    with TestClient(app) as client:
        assert client.get("/api/health").json()["license"]["type"] == "permanent"


def test_open_app_blocks_reads_writes_and_health_at_expiration(trial):
    app = create_app(trial.path / "data")
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["license"]["type"] == "trial"
        assert health["license"]["expires_at"] - health["license"]["issued_at"] == 604800
        assert client.post("/api/models/demo", json={}).status_code == 200
        trial.clock["wall"] = ISSUED + 604800
        for method, path in (("GET", "/api/models"), ("GET", "/api/health"), ("POST", "/api/models/demo")):
            response = client.request(method, path, json={} if method == "POST" else None)
            assert response.status_code == 403 and "已到期" in response.json()["detail"]
            assert response.headers["Cache-Control"] == "no-store"
        assert client.get("/").status_code == 200
        assert len(app.state.db.all("SELECT * FROM models")) == 1


@pytest.mark.parametrize("mode", ["dax", "mquery"])
def test_stream_expiration_preserves_text_closes_transport_and_stops_future_output(trial, monkeypatch, mode):
    app = create_app(trial.path / "data")
    closed = threading.Event()

    class Provider:
        def __init__(self):
            self._client = self

        def close(self):
            closed.set()

        def stream(self, *args, **kwargs):
            yield "试用期内的正文"
            assert closed.wait(5)
            yield "到期后的内容不应被接收"

    # Expire only after the actual background chat has persisted a received delta.
    original = app.state.db.execute

    def execute(query, parameters=()):
        result = original(query, parameters)
        if query.startswith("UPDATE messages SET content=") and parameters and parameters[0] == "试用期内的正文":
            trial.clock["wall"] = ISSUED + 604800
        return result

    monkeypatch.setattr(app.state.db, "execute", execute)
    monkeypatch.setattr(app.state.settings, "provider", Provider)
    with TestClient(app) as client:
        model = client.post("/api/models/demo", json={}).json()
        query_name = next(iter(model["context"]["table_queries"])) if mode == "mquery" else ""
        conv = client.post("/api/conversations", json={
            "model_id": model["id"], "mode": mode, "query_name": query_name}).json()
        response = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "解释模型"})
    assert closed.wait(2)
    events = [json.loads(line) for line in response.text.splitlines()]
    assert any(event["type"] == "error" and "已到期" in event["text"] for event in events)
    assert not any(event["type"] == "done" for event in events)
    saved = app.state.chat.messages(conv["id"])[-1]
    assert saved["status"] == "error" and "试用期内的正文" in saved["content"]
    assert "到期后的内容" not in saved["content"] and not saved["artifacts"]
    assert not app.state.chat.generation_status(conv["id"])["running"]


def test_semantic_background_thread_preserves_authorization_context(trial, monkeypatch):
    app = create_app(trial.path / "data")
    ctx = context_from_dict(app.state.powerbi.demo()["context"])
    model = app.state.powerbi.save("语义试用测试", "desktop", 50001, ctx)
    monkeypatch.setattr(app.state.powerbi, "require_live", lambda *a, **k: (model, ctx))
    monkeypatch.setattr("backend.semantics.LiveDesktopSource",
                        lambda *a: SimpleNamespace(semantic_metadata=lambda: {}))
    closed = threading.Event()

    class Provider:
        def __init__(self):
            self._client = self

        def close(self):
            closed.set()

        def stream(self, *args, **kwargs):
            trial.clock["wall"] = ISSUED + 604800
            yield " "
            assert closed.wait(5)

    monkeypatch.setattr(app.state.settings, "provider", Provider)
    with TestClient(app) as client:
        assert client.post(f"/api/models/{model['id']}/semantics/build", json={}).status_code == 200
        assert closed.wait(4)
    saved = app.state.semantics.get(model["id"])
    assert saved["status"] == "error" and "已到期" in saved["error"]
    assert saved["coverage"] == 0


@pytest.mark.parametrize("stage", ["plan", "repair", "summary"])
def test_analysis_expiration_stops_subsequent_work_and_preserves_query_evidence(trial, monkeypatch, stage):
    from tests.test_analysis import plan, result

    app = create_app(trial.path / "data")
    ctx = context_from_dict(app.state.powerbi.demo()["context"])
    model = app.state.powerbi.save("分析试用测试", "desktop", 50001, ctx)
    dictionary = app.state.semantics.ensure(model, ctx)
    dictionary.update(status="ready", coverage=len(dictionary["entries"]))
    app.state.semantics._store(model["id"], dictionary)
    monkeypatch.setattr(app.state.powerbi, "require_live", lambda *a, **k: (model, ctx))
    monkeypatch.setattr(app.state.semantics, "unresolved_code", lambda *a: None)
    calls, queries = [], []

    class Provider:
        def stream(self, *args, **kwargs):
            calls.append(args)
            if (stage == "plan" and len(calls) == 1) or (stage != "plan" and len(calls) == 2):
                trial.clock["wall"] = ISSUED + 604800
                yield "到期后的内容不应被接收"
            else:
                yield plan()

    def execute(*args):
        queries.append(args)
        return result(ok=stage != "repair")

    monkeypatch.setattr(app.state.settings, "provider", Provider)
    monkeypatch.setattr(app.state.chat.analysis, "execute", execute)
    with TestClient(app) as client:
        conv = client.post("/api/conversations", json={"model_id": model["id"], "mode": "analysis"}).json()
        response = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "分析实际总额"})
    assert "已到期" in response.text and "到期后的内容" not in response.text
    assert len(calls) == (1 if stage == "plan" else 2)
    assert len(queries) == (0 if stage == "plan" else 1)
    saved = app.state.chat.messages(conv["id"])[-1]
    assert saved["status"] == "error" and len(saved["analysis"]) == len(queries)
    assert not app.state.chat.generation_status(conv["id"])["running"]


def test_cli_selects_type_prints_expiry_separately_and_out_file_contains_only_key(trial, monkeypatch, capsys):
    monkeypatch.setattr(license_admin, "load_private_key", lambda _: trial.private)
    monkeypatch.setattr(license_admin, "time", SimpleNamespace(time=lambda: ISSUED))
    for kind, prefix in (("permanent", "PBI2K-"), ("trial", "PBI2T-")):
        out = trial.path / f"{kind}.txt"
        args = ["--key-dir", str(trial.path), "issue", "--machine-code", trial.code, "--type", kind, "--out", str(out)]
        assert license_admin.main(args) == 0
        output = capsys.readouterr()
        assert output.out.strip().startswith(prefix) and len(output.out.strip().splitlines()) == 1
        assert out.read_text(encoding="utf-8") == output.out
        assert ("永久授权" if kind == "permanent" else "7 天试用") in output.err
        assert license_admin.main(args) == 1
        capsys.readouterr()


def test_installer_helper_reports_trial_deadline_and_rejects_expired_key(trial, monkeypatch):
    monkeypatch.setattr(license_helper, "machine_code", lambda: trial.code)
    output = trial.path / "result.txt"
    args = ["verify", "--license-file", str(trial.path / "license.key"), "--output", str(output)]
    assert license_helper.main(args) == 0
    assert "7 天试用" in output.read_text(encoding="utf-8")
    trial.clock["wall"] = ISSUED + 604800
    assert license_helper.main(args) == 1
    assert "已到期" in output.read_text(encoding="utf-8")
