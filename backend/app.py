"""Loopback-only HTTP API, shared by the native desktop window and development preview."""
import json
import os
import sys
import csv
import io
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .chat import ChatService
from .database import Database, encode, now, uid
from .engine.context.live_source import find_instances
from .engine.context.live_writer import LiveDesktopWriter
from .engine.dax.live_eval import LiveDesktopEvaluator
from .engine.llm.base import user
from .powerbi import PowerBIService, context_from_dict
from .schemas import ApplyInput, CancelGenerationInput, ConnectInput, ConversationInput, LLMProfileInput, MessageInput, ReasoningInput, SettingsInput
from .settings import SettingsService
from .fields import FieldPreviewInput
from .reports import reports_for_instances
from .semantics import SemanticBuildInput, SemanticEdit, SemanticReuseInput, SemanticService
from .streaming import BackgroundStream
from .validation import validate
from .images import MAX_UPLOAD_BYTES
from .licensing import LicenseError, LicenseGuard, require_runtime_license
from .cancellation import authorization_scope

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def data_directory():
    if os.getenv("PBI_DATA_DIR"):
        return Path(os.environ["PBI_DATA_DIR"]).resolve()
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PowerBIAssistant"
    return ROOT / "data"


def create_app(directory=None):
    license_info = require_runtime_license()
    license_guard = LicenseGuard(license_info) if license_info else None
    directory = Path(directory) if directory else data_directory()
    db = Database(directory / "assistant.sqlite3")
    settings = SettingsService(db, directory)
    powerbi = PowerBIService(db)
    semantics=SemanticService(db,powerbi)
    powerbi.semantics=semantics
    chat = ChatService(db, powerbi)
    app = FastAPI(title="Power BI Assistant", version="2.0.2", docs_url="/api/docs", redoc_url=None)
    app.state.license_info = license_info
    app.state.db, app.state.settings, app.state.powerbi, app.state.chat = db, settings, powerbi, chat
    app.state.semantics=semantics
    app.state.images=chat.images
    chat.images.remove_expired_pending()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    def check_authorization():
        if license_guard:
            try:
                license_guard.check()
            except LicenseError as exc:
                raise HTTPException(403, str(exc)) from exc

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        # No CORS: websites on other origins cannot read local model data or issue JSON writes.
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "不允许跨站访问本地助手"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "不允许跨站访问本地助手"}, status_code=403)
        if request.method in ("POST", "PUT", "PATCH"):
            image_upload=request.method=='POST' and request.url.path=='/api/images'
            if not image_upload and "application/json" not in request.headers.get("content-type", ""):
                return JSONResponse({"detail": "请求必须使用 JSON"}, status_code=415)
            if not image_upload and len(await request.body()) > 4 * 1024 * 1024:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
        try:
            if request.url.path.startswith("/api/"):
                check_authorization()
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        else:
            with authorization_scope(check_authorization if license_guard else None):
                response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "2.0.2", "storage": "sqlite", "platform": "desktop",
                "license": {"type": "trial" if license_info.issued_at is not None else "permanent",
                            "issued_at": license_info.issued_at, "expires_at": license_info.expires_at,
                            "summary": license_info.summary} if license_info else {"type": "development"}}

    @app.post('/api/images',status_code=201)
    async def upload_image(request:Request):
        data=bytearray()
        async for chunk in request.stream():
            if len(data)+len(chunk)>MAX_UPLOAD_BYTES:raise HTTPException(413,'单张图片不能超过 8 MB，请缩小截图范围')
            data.extend(chunk)
        return await run_in_threadpool(chat.images.upload,bytes(data),unquote(request.headers.get('x-image-name','截图')),
                                       request.headers.get('content-type','').split(';',1)[0].lower())

    @app.get('/api/images/{image_id}')
    def image_attachment(image_id:str):
        row=chat.images.get(image_id)
        return Response(row['data'],media_type=row['mime_type'],headers={'Cache-Control':'no-store'})

    @app.delete('/api/images/{image_id}')
    def delete_image(image_id:str):
        chat.images.delete_pending(image_id)
        return {'ok':True}

    @app.post('/api/desktop/screenshot')
    def start_screenshot():
        if not hasattr(os,'startfile'):raise HTTPException(409,'当前系统无法打开截图工具，请自行截图后粘贴图片')
        try:os.startfile('ms-screenclip:')
        except OSError as exc:raise HTTPException(409,'未能打开 Windows 截图工具，请使用 Win+Shift+S 后粘贴图片') from exc
        return {'ok':True,'detail':'截图后回到输入框按 Ctrl+V 添加图片'}

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

    @app.patch('/api/llm/profiles/{profile_id}/reasoning')
    def update_llm_reasoning(profile_id: str, body: ReasoningInput):
        return settings.save_reasoning(profile_id, body)

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
        engines=find_instances()
        reports=reports_for_instances(engines)
        return [{"port":i.port,"pid":i.pid,"report":reports.get(i.pid,{})} for i in engines]

    @app.post("/api/models/connect")
    def connect(body: ConnectInput):
        model=powerbi.connect(body.port,body.report_name)
        semantics.on_connect(model,settings)
        return powerbi.get(model['id'])

    @app.post("/api/models/demo")
    def demo():
        return powerbi.demo()

    @app.get("/api/models")
    def models():
        return [powerbi.get(r["id"]) for r in db.all("SELECT id FROM models ORDER BY created_at DESC LIMIT 50")]

    @app.get("/api/models/{model_id}")
    def model(model_id: str):
        return powerbi.get(model_id)

    @app.get('/api/models/{model_id}/connection')
    def model_connection(model_id: str):
        return powerbi.reconnector.poll(model_id)

    @app.post('/api/models/{model_id}/fields/preview')
    def preview_field(model_id: str, body: FieldPreviewInput):
        return chat.analysis.fields.preview(model_id, body)

    @app.get('/api/models/{model_id}/semantics')
    def get_semantics(model_id:str):
        return semantics.get(model_id)

    @app.post('/api/models/{model_id}/semantics/build')
    def build_semantics(model_id:str,body:SemanticBuildInput):
        return semantics.start(model_id,settings,body.llm_profile_id)

    @app.post('/api/models/{model_id}/semantics/reuse')
    def reuse_semantics(model_id:str,body:SemanticReuseInput):
        return semantics.reuse(model_id,body.source_model_id)

    @app.put('/api/models/{model_id}/semantics/{entry_id}')
    def edit_semantics(model_id:str,entry_id:str,body:SemanticEdit):
        return semantics.edit(model_id,entry_id,body)

    @app.post("/api/models/{model_id}/refresh")
    def refresh(model_id: str):
        model=powerbi.refresh(model_id)
        semantics.on_connect(model,settings)
        return powerbi.get(model['id'])

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

    @app.post('/api/conversations/{conversation_id}/refresh-model')
    def refresh_conversation_model(conversation_id:str):
        return chat.refresh_model(conversation_id,settings)

    @app.get('/api/conversations/{conversation_id}/generation')
    def generation_status(conversation_id: str):
        chat.conversation(conversation_id)
        return chat.generation_status(conversation_id)

    @app.post('/api/conversations/{conversation_id}/cancel')
    def cancel_generation(conversation_id: str, body: CancelGenerationInput):
        return chat.cancel(conversation_id, body.generation_id)

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
            text += f"## {'你' if message['role'] == 'user' else '助手'}\n\n"
            if message.get('model_stale'):text+='> 此回复生成于模型同步前，内容和查询结果作为历史保留。\n\n'
            text += message['content']+'\n\n'
            for picture in message.get('images',[]):
                row=chat.images.get(picture['id'])
                import base64
                text+='![附图](data:'+row['mime_type']+';base64,'+base64.b64encode(row['data']).decode('ascii')+')\n\n'
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
        if obj["kind"] not in ("dax_measure", "dax_table", "dax_column"):
            raise HTTPException(409, "Power Query 请复制到高级编辑器，不支持外部写回")
        if obj['kind'] not in ('dax_measure','dax_column') and (body.name is not None or body.home_table is not None):
            raise HTTPException(422,'名称与存放表设置仅适用于度量值和计算列')
        if obj['kind']=='dax_column' and body.overwrite:
            raise HTTPException(409,'计算列仅支持新建，不覆盖已有字段，请修改列名')
        prior = json.loads(obj["validation"])
        if not prior.get("ok") or not prior.get("run_verified"):
            raise HTTPException(409, "只有实跑验证通过的代码才能写入模型")
        with powerbi.lock:
            conversation=db.one('SELECT c.model_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=?',
                                (obj['message_id'],))
            model, ctx = powerbi.require_live(conversation['model_id'], unchanged=True)
            target={**obj,'name':body.name if body.name is not None else obj['name'],
                    'home_table':body.home_table if body.home_table is not None else obj['home_table']}
            if obj['kind']=='dax_column':
                if target['home_table'] not in ctx.tables:
                    raise HTTPException(409,'指定的所属表已不存在，请同步模型后重新选择')
                if any(c.name.casefold()==target['name'].casefold() for c in ctx.tables[target['home_table']]):
                    raise HTTPException(409,'所属表中已有同名字段，计算列仅支持新建，请修改列名')
                if any(m.table==target['home_table'] and m.name.casefold()==target['name'].casefold() for m in ctx.measures):
                    raise HTTPException(409,'所属表中已有同名度量值，请修改列名')
            if obj['kind']=='dax_measure':
                if not ctx.has_table(target['home_table']):
                    raise HTTPException(409,'指定的存放表已不存在，请同步模型后重新选择')
                existing=[m for m in ctx.measures if m.name.casefold()==target['name'].casefold()]
                if any(m.table!=target['home_table'] for m in existing):
                    table=next(m.table for m in existing if m.table!=target['home_table'])
                    raise HTTPException(409,f'同名度量值已在表“{table}”中，请修改名称，或选择该表并明确允许覆盖')
                if existing and not body.overwrite:
                    raise HTTPException(409,'所选表中已有同名度量值，请修改名称或勾选允许覆盖')
                if any(c.name.casefold()==target['name'].casefold() for c in ctx.tables.get(target['home_table'],[])):
                    raise HTTPException(409,'所选表中已有同名字段，请为度量值使用其他名称')
            evaluator = LiveDesktopEvaluator(model["port"])
            fresh = validate(ctx, [target], target["home_table"], evaluator, prior.get("calibration", []))[0]
            if not fresh["ok"] or not fresh["run_verified"]:
                raise HTTPException(409, "写入前重新验证失败，请重新生成：" + "; ".join(fresh["errors"]))
            writer = LiveDesktopWriter(model["port"])
            if obj["kind"] == "dax_table":
                if ctx.has_table(obj["name"]):
                    raise HTTPException(409, "同名表已存在，请使用新表名；不支持覆盖已有表")
                result = writer.write_calculated_table(obj["name"], obj["expression"], overwrite=False)
            elif obj['kind']=='dax_column':
                result=writer.write_calculated_column(target['home_table'],target['name'],target['expression'])
            else:
                result = writer.write_measure(target["home_table"], target["name"], target["expression"], overwrite=body.overwrite)
            payload={**asdict(result),'kind':obj['kind'],'name':target['name'],'home_table':target['home_table']}
            db.audit("write_model", payload, artifact_id)
            if not result.ok:
                raise HTTPException(409, result.detail)
            applied_at=now()
            db.execute("UPDATE artifacts SET name=?,home_table=?,validation=?,applied_at=?,model_id=? WHERE id=?",
                       (target['name'],target['home_table'],encode(fresh),applied_at,model['id'],artifact_id))
            payload.update(applied_at=applied_at,validation=fresh)
            try:
                powerbi.update_after_write(model["id"], model["port"])
            except Exception:
                return {**payload, "detail": result.detail + "；模型上下文同步失败，请手动同步"}
            return payload

    app.mount("/assets", StaticFiles(directory=ROOT / "frontend"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "frontend" / "index.html")

    return app
