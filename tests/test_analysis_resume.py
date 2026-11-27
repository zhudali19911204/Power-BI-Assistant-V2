import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.cancellation import GenerationCancelled
from backend.database import encode
from tests.test_analysis import Provider, plan, result, send
from tests.test_analysis import setup as setup
from tests.test_analysis_protocol import saved, wire
from tests.test_field_discovery import profile
from tests.test_images import upload


def resume(client, conv, message):
    return client.post(
        f"/api/conversations/{conv['id']}/messages/{message['id']}/resume",
        json={"version": message["analysis_resume"]},
    )


def query(title, value):
    return {"title": title, "dax": f'EVALUATE ROW("Amount",{value})'}


@pytest.mark.parametrize("fields", [6, 7])
def test_minimal_plan_preserves_field_evidence_images_and_original_request(setup, monkeypatch, fields):
    columns = [
        ("客户", "地区"),
        ("客户", "客户ID"),
        ("客户", "客户名称"),
        ("产品", "类别"),
        ("产品", "产品ID"),
        ("产品", "产品名称"),
        ("日期", "年份"),
    ][:fields]
    checks = encode({"field_checks": [{"table": t, "column": c} for t, c in columns]})
    invalid = encode({"scope_resolutions": [{"field": "F2", "value": "", "basis": "原值"}]})
    app, client, conv, provider, queries = wire(
        setup, monkeypatch, [checks, invalid, "{", plan(), "125 [Q1]"]
    )
    reads = []

    def preview(_, body, **kw):
        reads.append((body.table, body.column))
        return {**profile(), "table": body.table, "column": body.column}

    monkeypatch.setattr(app.state.chat.analysis.fields, "preview", preview)
    img = upload(client)
    events = send(client, conv, "按截图范围分析金额", image_ids=[img["id"]])
    assert events[-1] == {"type": "done"} and reads == columns and len(queries) == 1
    record = saved(client, conv)
    assert len(record["field_previews"]) == fields and not record.get("analysis_resume")
    assert [g["stage"] for g in record["generations"]] == ["analysis_plan"] * 3 + [
        "analysis_minimal",
        "analysis_summary",
    ]
    turns = provider.calls[3][1]
    assert any(m.content == "按截图范围分析金额" and len(m.images) == 1 for m in turns)
    state = json.loads(turns[-1].content.rsplit("\n", 1)[-1])
    assert [r["label"] for r in state["field_observations"]] == [f"F{i + 1}" for i in range(fields)]


def test_independent_queries_survive_restart_and_resume_only_missing_work(setup, monkeypatch):
    first, second = query("已完成项目", 125), query("分月明细", 11)
    incomplete = encode({"scope": "含税金额", "queries": [first, {"title": second["title"]}]})
    app, client, conv, provider, executed = wire(setup, monkeypatch, [incomplete, incomplete, "", ""])
    img = upload(client)
    assert send(client, conv, "分析总额和分月明细", image_ids=[img["id"]])[-1]["partial"]
    partial = saved(client, conv)
    assert partial["status"] == "partial" and partial["analysis_resume"] and partial["analysis"][0]["rows"]
    assert (
        "模型总结未完成" in partial["content"]
        and "[Q1]" in partial["content"]
        and "分月明细" in partial["content"]
    )
    assert [g["stage"] for g in partial["generations"]].count("analysis_minimal") == 0
    assert executed == [first["dax"]]

    reopened = create_app(app.state.db.path.parent)
    monkeypatch.setattr(reopened.state.powerbi, "require_live", app.state.powerbi.require_live)
    resumed = Provider(
        [
            encode({"queries": [first, {**second, "title": "按月列示", "resolves": ["P1"]}]}),
            "见 [Q1] 和 [Q2]。",
        ]
    )
    monkeypatch.setattr(reopened.state.settings, "provider", lambda: resumed)
    monkeypatch.setattr(
        reopened.state.chat.analysis, "execute", lambda _, q: (executed.append(q), result())[1]
    )
    with TestClient(reopened) as client:
        response = resume(client, conv, saved(client, conv))
        assert response.is_success and json.loads(response.text.splitlines()[-1]) == {"type": "done"}
        messages = client.get(f"/api/conversations/{conv['id']}/messages").json()
        assert (
            len(messages) == 2
            and messages[-1]["id"] == partial["id"]
            and messages[-1]["status"] == "complete"
        )
        assert messages[-1]["analysis"][0] == partial["analysis"][0]
        assert [r["label"] for r in messages[-1]["analysis"]] == ["Q1", "Q2"]
        assert messages[-1]["analysis"][1]["resolved_tasks"] == ["分月明细"]
        assert executed == [first["dax"], second["dax"]] and not messages[-1].get("analysis_resume")
        assert [(g["stage"], g["attempt"]) for g in messages[-1]["generations"]][-2:] == [
            ("analysis_plan", 3),
            ("analysis_summary", 3),
        ]
        assert resume(client, conv, partial).status_code == 409
        assert any(m.images for m in resumed.calls[0][1])
        assert "分月明细" in resumed.calls[0][1][-1].content and "含税金额" in resumed.calls[0][1][-1].content
        assert "queries[1].dax" in resumed.calls[0][1][-1].content


@pytest.mark.parametrize(
    "unsafe",
    [
        {"scope_resolutions": {"field": "F99", "value": "0810", "basis": "分类"}},
        {"scope_resolutions": {}},
        {"field_checks": [{"table": "客户", "column": "地区"}]},
        {"unexpected": 1},
    ],
)
def test_global_issue_never_runs_even_well_formed_query_fragments(setup, monkeypatch, unsafe):
    raw = encode({"queries": [query("总额", 1), {"title": "缺失 DAX"}], **unsafe})
    _, client, conv, provider, executed = wire(setup, monkeypatch, [raw, raw, raw])
    assert send(client, conv)[-1] == {"type": "done", "partial": True}
    assert not executed and len(provider.calls) == 3


def test_long_search_groups_stay_bounded_and_preserve_unsearched_identifiers(setup, monkeypatch):
    terms = [f"{i:04}" for i in range(11)]
    request = {"field_checks": [{"table": "客户", "column": "地区", "search_terms": terms}]}
    app, client, conv, provider, executed = wire(setup, monkeypatch, [encode(request), plan(), "125 [Q1]"])
    reads = []

    def preview(_, body, *, search_terms, automatic):
        reads.append(search_terms)
        return {**profile(), "table": body.table, "column": body.column, "search_terms": search_terms}

    monkeypatch.setattr(app.state.chat.analysis.fields, "preview", preview)
    assert send(client, conv)[-1] == {"type": "done"} and len(executed) == 1
    assert reads == [terms[:3], terms[3:6], terms[6:9]]
    feedback = json.loads(provider.calls[1][1][-1].content.rsplit("\n", 1)[-1])
    assert feedback["unread_field_searches"][0]["search_terms"] == terms[9:]
    assert len(saved(client, conv)["field_previews"]) == 3


def test_resume_checks_confirmed_scope_and_refuses_stale_or_overlapping_clicks(setup, monkeypatch):
    app, client, conv, _, executed = wire(setup, monkeypatch, ["{}"] * 3)
    send(client, conv)
    partial = saved(client, conv)
    app.state.db.execute(
        "INSERT INTO conversation_bindings VALUES (?,?,?,?,?)", (conv["id"], "0810", "客户", "地区", "now")
    )
    assert resume(client, conv, partial).status_code == 409 and not executed
    assert saved(client, conv)["resume_scope_changed"] and not saved(client, conv).get("analysis_resume")
    app.state.db.execute("DELETE FROM conversation_bindings WHERE conversation_id=?", (conv["id"],))
    entered, release = threading.Event(), threading.Event()

    class Waiting(Provider):
        def stream(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            yield from super().stream(*args, **kwargs)

    provider = Waiting([plan(), "125 [Q1]"])
    monkeypatch.setattr(app.state.settings, "provider", lambda: provider)
    with ThreadPoolExecutor(1) as worker:
        running = worker.submit(resume, client, conv, partial)
        try:
            assert entered.wait(5) and resume(client, conv, partial).status_code == 409
        finally:
            release.set()
        assert running.result().is_success
    assert resume(client, conv, partial).status_code == 409 and len(executed) == 1


def test_stop_during_minimal_step_preserves_a_resumable_checkpoint_and_remains_cancelled(setup, monkeypatch):
    app, client, conv = setup

    class CancelMinimal(Provider):
        def stream(self, *args, **kw):
            if len(self.calls) == 2:
                raise GenerationCancelled()
            yield from super().stream(*args, **kw)

    provider = CancelMinimal(["{", "{"])
    monkeypatch.setattr(app.state.settings, "provider", lambda: provider)
    assert send(client, conv)[-1]["type"] == "cancelled"
    record = saved(client, conv)
    assert record["status"] == "cancelled" and record["analysis_resume"] and not record["analysis"]
