"""Loopback-only HTTP API, shared by the native desktop window and development preview."""
import json
import os
import sys
import csv
import io
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .chat import ChatService
from .database import Database, encode, now, uid
from .engine.context.live_source import find_instances
from .engine.context.live_writer import LiveDesktopWriter
from .engine.dax.live_eval import LiveDesktopEvaluator
from .engine.llm.base import user
from .powerbi import PowerBIService, context_from_dict
from .schemas import ApplyInput, ConnectInput, ConversationInput, LLMProfileInput, MessageInput, SettingsInput
from .settings import SettingsService
from .fields import FieldPreviewInput
from .streaming import BackgroundStream
from .validation import validate

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def data_directory():
    if os.getenv("PBI_DATA_DIR"):
        return Path(os.environ["PBI_DATA_DIR"]).resolve()
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PowerBIAssistant"
    return ROOT / "data"


def create_app(directory=None):
    directory = Path(directory) if directory else data_directory()
    db = Database(directory / "assistant.sqlite3")
    settings = SettingsService(db, directory)
    powerbi = PowerBIService(db)
    chat = ChatService(db, powerbi)
    app = FastAPI(title="Power BI Assistant", version="2.0.0", docs_url="/api/docs", redoc_url=None)
    app.state.db, app.state.settings, app.state.powerbi, app.state.chat = db, settings, powerbi, chat
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        # No CORS: websites on other origins cannot read local model data or issue JSON writes.
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "不允许跨站访问本地助手"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "不允许跨站访问本地助手"}, status_code=403)
        if request.method in ("POST", "PUT", "PATCH"):
            if "application/json" not in request.headers.get("content-type", ""):
                return JSONResponse({"detail": "请求必须使用 JSON"}, status_code=415)
            if len(await request.body()) > 4 * 1024 * 1024:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "2.0.0", "storage": "sqlite", "platform": "desktop"}

    @app.get("/api/settings")
    def get_settings():
        return settings.public()

    @app.put("/api/settings")
    def save_settings(body: SettingsInput):
        return settings.save(body)

    @app.post("/api/settings/test")
    def test_settings():
        return test_model_service()

    def test_model_service(profile_id=None):
        try:
            # Reasoning-capable servers may spend a small output budget before producing content.
            provider = settings.provider(profile_id) if profile_id else settings.provider()
            result = provider.complete("Reply with OK only.", [user("Connection test")], max_tokens=256)
            if not result.strip():
                raise HTTPException(422, "服务响应为空，请检查模型配置")
            return {"ok": True, "detail": "模型服务连接成功"}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(422, f"连接失败（{type(exc).__name__}），请检查服务器地址、模型名称、Key 和网络") from exc

    @app.get('/api/llm/profiles')
    def llm_profiles():
        return settings.profiles()

    @app.post('/api/llm/profiles', status_code=201)
    def create_llm_profile(body: LLMProfileInput):
        return settings.save_profile(body)

    @app.put('/api/llm/profiles/{profile_id}')
    def update_llm_profile(profile_id: str, body: LLMProfileInput):
        return settings.save_profile(body, profile_id)

    @app.post('/api/llm/profiles/{profile_id}/activate')
    def activate_llm_profile(profile_id: str):
        return settings.activate(profile_id)

    @app.post('/api/llm/profiles/{profile_id}/test')
    def test_llm_profile(profile_id: str):
        return test_model_service(profile_id)

    @app.delete('/api/llm/profiles/{profile_id}')
    def delete_llm_profile(profile_id: str):
        return settings.delete_profile(profile_id)

    @app.get("/api/desktop/instances")
    def instances():
        return [{"port": i.port, "pid": i.pid} for i in find_instances()]

    @app.post("/api/models/connect")
    def connect(body: ConnectInput):
        return powerbi.connect(body.port)

    @app.post("/api/models/demo")
    def demo():
        return powerbi.demo()

    @app.get("/api/models")
    def models():
        return [powerbi.get(r["id"]) for r in db.all("SELECT id FROM models ORDER BY created_at DESC LIMIT 50")]

    @app.get("/api/models/{model_id}")
    def model(model_id: str):
        return powerbi.get(model_id)

    @app.post('/api/models/{model_id}/fields/preview')
    def preview_field(model_id: str, body: FieldPreviewInput):
        return chat.analysis.fields.preview(model_id, body)

    @app.post("/api/models/{model_id}/refresh")
    def refresh(model_id: str):
        row = powerbi.get(model_id)
        if row["source"] != "desktop":
            raise HTTPException(409, "示例模型无需同步")
        return powerbi.connect(row["port"])

    @app.get("/api/conversations")
    def conversations():
        return [row | {'generating': chat.generation_status(row['id'])['running']}
                for row in db.all("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 300")]

    @app.post("/api/conversations", status_code=201)
    def create_conversation(body: ConversationInput):
        model = powerbi.get(body.model_id)
        if body.mode=='analysis' and model['source']!='desktop':
            raise HTTPException(409,'数据分析需要真实 Desktop 模型，请先连接本机报表')
        if body.mode == "mquery" and not context_from_dict(model["context"]).has_query(body.query_name):
            raise HTTPException(422, "请选择模型中存在的 Power Query 查询")
        conv_id, stamp = uid(), now()
        db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?)", (
            conv_id, body.title, body.mode, body.model_id, body.query_name, stamp, stamp))
        return chat.conversation(conv_id)

    @app.get("/api/conversations/{conversation_id}/messages")
    def messages(conversation_id: str):
        return chat.messages(conversation_id)

    @app.get('/api/conversations/{conversation_id}/generation')
    def generation_status(conversation_id: str):
        chat.conversation(conversation_id)
        return chat.generation_status(conversation_id)

    @app.delete("/api/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str):
        chat.conversation(conversation_id)
        lock = chat.locks.get(conversation_id)
        if lock and lock.locked():
            raise HTTPException(409, "请等待生成完成后删除会话")
        db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        return {"ok": True}

    @app.post("/api/conversations/{conversation_id}/messages")
    def send_message(conversation_id: str, body: MessageInput):
        task = BackgroundStream(chat.begin(conversation_id, body, settings))
        return StreamingResponse(task.events(),
                                 media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"})

    @app.post('/api/messages/{message_id}/validate')
    def revalidate_message(message_id: str):
        return chat.revalidate(message_id)

    @app.get("/api/conversations/{conversation_id}/export")
    def export(conversation_id: str):
        conv = chat.conversation(conversation_id)
        text = "# " + conv["title"] + "\n\n"
        for message in chat.messages(conversation_id):
            text += f"## {'你' if message['role'] == 'user' else '助手'}\n\n{message['content']}\n\n"
            for artifact in message["artifacts"]:
                vr = artifact["validation"]
                text += f"验证：{'通过' if vr['ok'] else '未通过'}；{'已实跑' if vr['run_verified'] else '未经实跑'}\n\n"
            for record in message.get('field_previews',[]):
                text+=f"### 字段取值检查 {record['label']} · {record['table']}[{record['column']}]\n\n"
                if not record['ok']:
                    text+='检查未完成：'+record['error']+'\n\n';continue
                profile=record['profile'];stats=profile['statistics']
                text+=profile['scope']+'。\n\n'
                text+=f"总行数：{stats['row_count']}；不同取值（含空值）：{stats['distinct_count']}；空值行数：{stats['blank_count']}；空字符串：{stats['empty_string_count']}。\n\n"
                text+='取值样例：'+encode(profile['samples'])+'\n\n'
                if profile['search_terms']:text+='搜索词：'+encode(profile['search_terms'])+'\n\n'
                text+='| 取值 | 出现行数 |\n| --- | --- |\n'
                for entry in profile['values']:
                    value=encode(entry['value']).replace('|','\\|').replace('<','&lt;')
                    text+=f"| {value}{'（值已截断）' if entry['value_truncated'] else ''} | {entry['count']} |\n"
                text+='\n'+('结果已截断，不能代表全部取值。\n\n' if profile['truncated'] else '')
                for query in profile['dax_queries']:text+='```dax\n'+query+'\n```\n\n'
            for result in message.get('analysis',[]):
                text+=f"### {result['label']} · {result['title']}（第 {result['attempt']} 次执行）\n\n"
                text+='```dax\n'+result['dax']+'\n```\n\n'
                if not result['ok']:
                    text+='查询未成功：'+result['error']+'\n\n';continue
                text+=f"实际结果：保留 {result['row_count']} 行；{'结果已截断' if result['truncated'] else '返回结果未截断'}。\n\n"
                columns=result['columns']
                def cell(value):return str('空值' if value is None else value).replace('|','\\|').replace('\n',' ')
                if columns:
                    text+='| '+' | '.join(map(cell,columns))+' |\n| '+' | '.join('---' for _ in columns)+' |\n'
                    for row in result['rows']:
                        text+='| '+' | '.join(cell(row.get(c)) for c in columns)+' |\n'
                    text+='\n'
        return Response(text, media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="conversation-{conversation_id[:8]}.md"'})

    @app.get('/api/analysis/{query_id}/csv')
    def export_analysis(query_id: str):
        record=db.one('SELECT result FROM analysis_queries WHERE id=?',(query_id,))
        if not record:raise HTTPException(404,'查询结果不存在')
        result=json.loads(record['result'])
        if not result['ok']:raise HTTPException(409,'失败的查询没有可导出的结果')
        target=io.StringIO(newline='');writer=csv.writer(target)
        def safe(value):
            if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):return "'"+value
            return '' if value is None else value
        writer.writerow([safe(c) for c in result['columns']])
        for row in result['rows']:writer.writerow([safe(row.get(c)) for c in result['columns']])
        return Response(target.getvalue().encode('utf-8-sig'),media_type='text/csv; charset=utf-8',headers={
            'Content-Disposition':f'attachment; filename="analysis-{query_id[:8]}.csv"'})

    @app.post("/api/artifacts/{artifact_id}/apply")
    def apply_artifact(artifact_id: str, body: ApplyInput):
        obj = db.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        if not obj:
            raise HTTPException(404, "代码产物不存在")
        if obj["kind"] not in ("dax_measure", "dax_table"):
            raise HTTPException(409, "Power Query 请复制到高级编辑器，不支持外部写回")
        prior = json.loads(obj["validation"])
        if not prior.get("ok") or not prior.get("run_verified"):
            raise HTTPException(409, "只有实跑验证通过的代码才能写入模型")
        with powerbi.lock:
            model, ctx = powerbi.require_live(obj["model_id"], unchanged=True)
            evaluator = LiveDesktopEvaluator(model["port"])
            fresh = validate(ctx, [obj], obj["home_table"], evaluator, prior.get("calibration", []))[0]
            if not fresh["ok"] or not fresh["run_verified"]:
                raise HTTPException(409, "写入前重新验证失败，请重新生成：" + "; ".join(fresh["errors"]))
            writer = LiveDesktopWriter(model["port"])
            if obj["kind"] == "dax_table":
                if ctx.has_table(obj["name"]):
                    raise HTTPException(409, "同名表已存在，请使用新表名；不支持覆盖已有表")
                result = writer.write_calculated_table(obj["name"], obj["expression"], overwrite=False)
            else:
                result = writer.write_measure(obj["home_table"], obj["name"], obj["expression"], overwrite=body.overwrite)
            db.audit("write_model", asdict(result), artifact_id)
            if not result.ok:
                raise HTTPException(409, result.detail)
            db.execute("UPDATE artifacts SET applied_at=? WHERE id=?", (now(), artifact_id))
            try:
                powerbi.update_after_write(model["id"], model["port"])
            except Exception:
                return {"ok": True, "detail": result.detail + "；模型上下文同步失败，请手动同步"}
            return asdict(result)

    app.mount("/assets", StaticFiles(directory=ROOT / "frontend"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "frontend" / "index.html")

    return app
