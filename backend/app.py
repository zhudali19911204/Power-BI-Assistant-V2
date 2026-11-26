"""Loopback-only HTTP API, shared by the native desktop window and development preview."""
import json
import os
import sys
import csv
import io
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
from urllib.parse import quote, unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .chat import ChatService
from .analysis_templates import AnalysisTemplates, TemplateCreate, TemplateEdit, TemplateRun, TemplateScan
from .database import Database, encode, now, uid
from .engine.context.live_source import find_instances
from .engine.context.live_writer import LiveDesktopWriter
from .engine.dax.live_eval import LiveDesktopEvaluator
from .engine.llm.base import user
from .powerbi import PowerBIService, context_from_dict
from .schemas import ApplyInput, CancelGenerationInput, ConnectInput, ContextPreference, ConversationInput, LanguagePreference, LayoutInput, LLMProfileInput, MessageInput, ReasoningInput, SettingsInput
from .settings import SettingsService
from .fields import FieldPreviewInput
from .reports import reports_for_instances
from .semantics import SemanticBuildInput, SemanticEdit, SemanticReuseInput, SemanticService
from .business_dictionary import BusinessEdit, OverviewEdit
from .dictionary_library import DictionaryNameEdit
from . import semantic_transfer
from .streaming import BackgroundStream
from .validation import validate
from .images import MAX_UPLOAD_BYTES
from .licensing import LicenseError, LicenseGuard, require_runtime_license
from .releases import APP_VERSION, UpdateError
from .cancellation import authorization_scope
from .language import Language, language_scope, wording

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
    templates = AnalysisTemplates(db, powerbi, chat, settings)
    def dictionary_in_use(model_id,*,include_sync=True):
        for conversation_id,lock in list(chat.locks.items()):
            if conversation_id in chat.updating_context:continue
            if lock.locked() and (include_sync or conversation_id not in chat.syncing):
                row=db.one('SELECT model_id FROM conversations WHERE id=?',(conversation_id,))
                if row and row['model_id']==model_id:return True
        return False
    semantics.in_use=dictionary_in_use
    def snapshot_in_use(model_id):
        return model_id in semantics.jobs or dictionary_in_use(model_id,include_sync=False)
    powerbi.snapshot_in_use=snapshot_in_use
    app = FastAPI(title="Power BI Assistant", version=APP_VERSION, docs_url="/api/docs", redoc_url=None)
    app.state.license_info = license_info
    app.state.updates = None  # Enabled by the installed native window once it is ready.
    pending_writes = 0
    restarting = False
    app.state.db, app.state.settings, app.state.powerbi, app.state.chat = db, settings, powerbi, chat
    app.state.semantics=semantics
    app.state.images=chat.images
    app.state.analysis_templates=templates
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
        nonlocal pending_writes
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
            semantic_upload=(request.method=='POST' and request.url.path.startswith('/api/models/') and
                             request.url.path.endswith(('/semantics/import','/semantics/import-preview')))
            if not image_upload and not semantic_upload and len(await request.body()) > 4 * 1024 * 1024:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
        try:
            if request.url.path.startswith("/api/"):
                check_authorization()
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        else:
            writing = (request.method in ("POST", "PUT", "PATCH", "DELETE")
                       and not request.url.path.startswith("/api/updates/"))
            if writing and restarting:
                return JSONResponse({"detail": "助手正在重启更新，请稍候。"}, status_code=503)
            if writing:
                pending_writes += 1
            try:
                language = 'en' if request.headers.get('X-UI-Language') == 'en' else 'zh-CN'
                with authorization_scope(check_authorization if license_guard else None), language_scope(language):
                    response = await call_next(request)
            finally:
                if writing:
                    pending_writes -= 1
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
        return {"status": "ok", "version": APP_VERSION, "storage": "sqlite", "platform": "desktop",
                "license": {"type": "trial" if license_info.issued_at is not None else "permanent",
                            "issued_at": license_info.issued_at, "expires_at": license_info.expires_at,
                            "summary": license_info.summary} if license_info else {"type": "development"}}

    def desktop_updates():
        if app.state.updates is None:
            raise HTTPException(409, "在线更新仅适用于已安装的桌面窗口。")
        return app.state.updates

    @app.get("/api/updates")
    def update_status():
        return {"enabled": app.state.updates is not None, "version": APP_VERSION}

    @app.post("/api/updates/check")
    def update_check():
        try:
            return {"release": desktop_updates().check()}
        except UpdateError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/updates/{digest}/download")
    def update_download(digest: str):
        try:
            desktop_updates().download(digest)
        except UpdateError as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "无法保存安装包，请检查临时目录的空间和权限。") from exc
        return {"ready": True}

    @app.post("/api/updates/{digest}/install")
    async def update_install(digest: str):
        nonlocal restarting
        updater = desktop_updates()
        # Admission and the handoff flag run on the same event loop as local_security.
        if (restarting or pending_writes or semantics.jobs
                or any(lock.locked() for lock in list(chat.locks.values()))):
            raise HTTPException(409, "助手正在处理任务，请在任务结束后更新。")
        restarting = True
        started = False
        try:
            await run_in_threadpool(updater.launch, digest)
            started = True
        except UpdateError as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "无法启动已下载的安装包，请重新下载后再试。") from exc
        finally:
            if not started:
                restarting = False
        return JSONResponse({"restarting": True}, background=BackgroundTask(updater.close_window))

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

    @app.get('/api/ui/language')
    def get_language():
        return db.one('SELECT language FROM ui_language WHERE id=1') or {'language': 'zh-CN'}

    @app.put('/api/ui/language')
    def save_language(body: LanguagePreference):
        db.execute('INSERT OR REPLACE INTO ui_language (id,language) VALUES (1,?)', (body.language,))
        return body

    @app.get('/api/ui/layout')
    def get_layout():
        row = db.one('SELECT context_width,sidebar_width FROM ui_layout WHERE id=1')
        return row if row else LayoutInput().model_dump()

    @app.put('/api/ui/layout')
    def save_layout(body: LayoutInput):
        # Desktop uses a random port and private WebView storage on each launch.
        # Persist UI preferences independently of model service credentials.
        with db.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT context_width,sidebar_width FROM ui_layout WHERE id=1').fetchone()
            values = (dict(row) if row else LayoutInput().model_dump()) | body.model_dump(exclude_unset=True)
            connection.execute('INSERT OR REPLACE INTO ui_layout (id,context_width,sidebar_width) VALUES (1,?,?)',
                               (values['context_width'], values['sidebar_width']))
        return values

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
        with powerbi.lock:
            model=powerbi.connect(body.port,body.report_name)
            semantics.on_connect(model,settings)
            return powerbi.get(model['id'])

    @app.post("/api/models/demo")
    def demo():
        return powerbi.demo()

    @app.get("/api/models")
    def models():
        return [powerbi.get(r["id"]) for r in db.all("SELECT id FROM models WHERE context!='null' ORDER BY created_at DESC LIMIT 50")]

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

    @app.get('/api/models/{model_id}/semantics/export')
    def export_semantics(model_id:str):
        document=semantic_transfer.export_file(semantics,model_id)
        name=document['dictionary_name'] or document['model_name'] or '模型'
        name=''.join('_' if c in '<>:"/\\|?*' or ord(c)<32 else c for c in name)[:100]
        filename=quote(name+'-语义层.json',safe='')
        return JSONResponse(document,headers={'Content-Disposition':f"attachment; filename=\"semantic-layer.json\"; filename*=UTF-8''{filename}"})

    async def load_semantics_file(model_id,request,expected=None):
        data=bytearray()
        async for chunk in request.stream():
            if len(data)+len(chunk)>semantic_transfer.MAX_IMPORT_BYTES:
                raise HTTPException(413,'语义层文件超过 64 MB')
            data.extend(chunk)
        def load():
            return semantic_transfer.import_file(semantics,model_id,semantic_transfer.read_file(bytes(data)),expected=expected)
        return await run_in_threadpool(load)

    @app.post('/api/models/{model_id}/semantics/import-preview')
    async def preview_semantics_file(model_id:str,request:Request):
        return await load_semantics_file(model_id,request)

    @app.post('/api/models/{model_id}/semantics/import')
    async def import_semantics(model_id:str,request:Request,updated_at:str):
        return await load_semantics_file(model_id,request,updated_at)

    @app.post('/api/models/{model_id}/semantics/build')
    def build_semantics(model_id:str,body:SemanticBuildInput):
        return semantics.start(model_id,settings,body.llm_profile_id,body.mode)

    @app.post('/api/models/{model_id}/semantics/reuse')
    def reuse_semantics(model_id:str,body:SemanticReuseInput):
        return semantics.reuse(model_id,body.source_model_id,body.source_updated_at)

    @app.get('/api/semantic-dictionaries')
    def semantic_dictionaries(current_model_id:str|None=None):
        return semantics.library.catalog(current_model_id)

    @app.get('/api/models/{model_id}/semantics/reuse-preview')
    def preview_semantic_reuse(model_id:str,source_model_id:str):
        return semantics.library.preview(model_id,source_model_id)

    @app.delete('/api/semantic-dictionaries/{dictionary_id}')
    def delete_semantic_dictionary(dictionary_id:str,current_model_id:str|None=None,updated_at:str|None=None):
        return semantics.library.delete(dictionary_id,current_model_id=current_model_id,updated_at=updated_at)

    @app.put('/api/semantic-dictionaries/{dictionary_id}/name')
    def rename_semantic_dictionary(dictionary_id:str,body:DictionaryNameEdit):
        return semantics.library.rename(dictionary_id,body)

    @app.put('/api/models/{model_id}/semantics/business/overview')
    def edit_business_overview(model_id:str,body:OverviewEdit):
        return semantics.business.edit(model_id,body)

    @app.post('/api/models/{model_id}/semantics/business/items')
    def create_business_item(model_id:str,body:BusinessEdit):
        return semantics.business.edit(model_id,body)

    @app.put('/api/models/{model_id}/semantics/business/items/{item_id}')
    def edit_business_item(model_id:str,item_id:str,body:BusinessEdit):
        return semantics.business.edit(model_id,body,item_id)

    @app.delete('/api/models/{model_id}/semantics/business/items/{item_id}')
    def delete_business_item(model_id:str,item_id:str,revision:int|None=None):
        return semantics.business.delete(model_id,item_id,revision)

    @app.put('/api/models/{model_id}/semantics/{entry_id}')
    def edit_semantics(model_id:str,entry_id:str,body:SemanticEdit):
        return semantics.edit(model_id,entry_id,body)

    @app.post("/api/models/{model_id}/refresh")
    def refresh(model_id: str):
        with powerbi.lock:
            model=powerbi.refresh(model_id)
            semantics.on_connect(model,settings)
            return powerbi.get(model['id'])

    @app.get('/api/analysis-templates')
    def list_analysis_templates():
        return templates.catalog()

    @app.get('/api/messages/{message_id}/analysis-template')
    def analysis_template_seed(message_id: str):
        return templates.seed(message_id)

    @app.post('/api/analysis-templates', status_code=201)
    def create_analysis_template(body: TemplateCreate):
        return templates.save(body)

    @app.post('/api/analysis-templates/parameters/recognize')
    def recognize_template_parameters(body: TemplateScan):
        return templates.scan(body)

    @app.get('/api/analysis-templates/{template_id}')
    def get_analysis_template(template_id: str):
        return templates.get(template_id)

    @app.put('/api/analysis-templates/{template_id}')
    def edit_analysis_template(template_id: str, body: TemplateEdit):
        return templates.save(body, template_id)

    @app.delete('/api/analysis-templates/{template_id}')
    def delete_analysis_template(template_id: str, revision: int):
        return templates.delete(template_id, revision)

    @app.get('/api/analysis-templates/{template_id}/runs')
    def analysis_template_runs(template_id: str):
        return templates.runs(template_id)

    @app.post('/api/analysis-templates/{template_id}/runs')
    def run_analysis_template(template_id: str, body: TemplateRun):
        return templates.run(template_id, body)

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
        with db.connection() as conn:
            conn.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?)", (
                conv_id, body.title, body.mode, body.model_id, body.query_name, stamp, stamp))
            if not body.auto_compress:
                conn.execute('INSERT INTO conversation_contexts (conversation_id,automatic) VALUES (?,0)', (conv_id,))
        return chat.conversation(conv_id)

    @app.get('/api/conversations/{conversation_id}/context')
    def conversation_context(conversation_id: str):
        return chat.history.window(conversation_id).usage

    @app.put('/api/conversations/{conversation_id}/context')
    def context_preference(conversation_id: str, body: ContextPreference):
        return chat.update_context(conversation_id, automatic=body.automatic)

    @app.post('/api/conversations/{conversation_id}/context/compress')
    def compress_context(conversation_id: str):
        return chat.update_context(conversation_id, compress=True)

    @app.get("/api/conversations/{conversation_id}/messages")
    def messages(conversation_id: str):
        return chat.messages(conversation_id)

    @app.get('/api/conversations/{conversation_id}')
    def get_conversation(conversation_id: str):
        return chat.conversation(conversation_id)

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
    def export(conversation_id: str, language: Language = 'zh-CN'):
        with language_scope(language):
            return export_markdown(conversation_id)

    def export_markdown(conversation_id: str):
        conv = chat.conversation(conversation_id)
        text = "# " + conv["title"] + "\n\n"
        for message in chat.messages(conversation_id):
            role = wording('你', 'You') if message['role'] == 'user' else wording('助手', 'Assistant')
            text += f"## {role}\n\n"
            if message.get('model_stale'):text+=wording('> 此回复生成于模型同步前，内容和查询结果作为历史保留。\n\n',
                                                       '> This reply and its results predate the last model sync.\n\n')
            text += message['content']+'\n\n'
            for picture in message.get('images',[]):
                row=chat.images.get(picture['id'])
                import base64
                text+=wording('![附图](data:', '![Attachment](data:')+row['mime_type']+';base64,'+base64.b64encode(row['data']).decode('ascii')+')\n\n'
            for artifact in message["artifacts"]:
                vr = artifact["validation"]
                check = wording('通过','passed') if vr['ok'] else wording('未通过','failed')
                live = wording('已实跑','tested in Desktop') if vr['run_verified'] else wording('未经实跑','no live run')
                text += wording('验证：{}；{}','Validation: {} · {}').format(check,live)+'\n\n'
            for record in message.get('field_previews',[]):
                text+=wording('### 字段取值检查 ', '### Field value check ')+f"{record['label']} · {record['table']}[{record['column']}]\n\n"
                if not record['ok']:
                    text+=wording('检查未完成：','Check incomplete: ')+record['error']+'\n\n';continue
                profile=record['profile']
                end = wording('。\n\n', '.\n\n')
                scope = profile['scope']
                if scope == '当前用户可读取的全模型范围，未应用报表页面筛选':
                    scope = wording(scope, 'Entire accessible model, excluding report page filters')
                if 'confirmed_value' in record:text+=wording('用户已确认 ', 'Confirmed by user: ')+encode(record['confirmed_value'])+end
                if 'adopted_value' in record:text+=wording('本次采用 ', 'Used in this run: ')+encode(record['adopted_value'])+end
                if record.get('scope_term'):
                    text+=wording('核对口径 {}，精确搜索 {}','Scope term: {}. Exact search: {}').format(encode(record['scope_term']),encode(record['search_terms']))+end
                    matches=[entry['value'] for entry in profile['values']]
                    text+=(wording('实际命中 ', 'Matches: ')+encode(matches) if matches else wording('候选字段未命中','No matching values'))+end
                    text+=scope+wording('。批量查询见 ', '. Batch query: ')+record['query_label']+end
                    for query in profile['dax_queries']:text+='```dax\n'+query+'\n```\n\n'
                    continue
                stats=profile['statistics']
                text+=scope+end
                text+=wording('总行数：{row_count}；不同取值（含空值）：{distinct_count}；空值行数：{blank_count}；空字符串：{empty_string_count}',
                              'Rows: {row_count}. Distinct values including blanks: {distinct_count}. Blank rows: {blank_count}. Empty strings: {empty_string_count}').format(**stats)+end
                text+=wording('取值样例：','Sample values: ')+encode(profile['samples'])+'\n\n'
                if profile['search_terms']:text+=wording('搜索词：','Search terms: ')+encode(profile['search_terms'])+'\n\n'
                text+=wording('| 取值 | 出现行数 |','| Value | Row count |')+'\n| --- | --- |\n'
                for entry in profile['values']:
                    value=encode(entry['value']).replace('|','\\|').replace('<','&lt;')
                    suffix=wording('（值已截断）',' (truncated)') if entry['value_truncated'] else ''
                    text+=f"| {value}{suffix} | {entry['count']} |\n"
                text+='\n'+(wording('结果已截断，不能代表全部取值。\n\n','Results were truncated. More values may exist.\n\n') if profile['truncated'] else '')
                for query in profile['dax_queries']:text+='```dax\n'+query+'\n```\n\n'
            for result in message.get('analysis',[]):
                text+=f"### {result['label']} · {result['title']}"+wording('（第 {} 次执行）',' (attempt {})').format(result['attempt'])+'\n\n'
                if recovery:=result.get('recovery'):
                    text+=wording('替代 {for_query}。{missing}。{basis}', 'Alternative to {for_query}. {missing}. {basis}').format(**recovery)+'\n\n'
                for note in result.get('convention_exceptions', {}).values():
                    text+=wording('分析约定「{title}」　{reason}', 'Analysis convention “{title}”: {reason}').format(**note)+'\n\n'
                text+='```dax\n'+result['dax']+'\n```\n\n'
                if not result['ok']:
                    text+=wording('查询未成功：','Query failed: ')+result['error']+'\n\n';continue
                limit=wording('结果已截断','truncated') if result['truncated'] else wording('返回结果未截断','complete')
                text+=wording('实际结果：保留 {} 行；{}。','Query results: {} rows saved ({})').format(result['row_count'],limit)+'\n\n'
                columns=result['columns']
                def cell(value):return str(wording('空值','Blank') if value is None else value).replace('|','\\|').replace('\n',' ')
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

    @app.get("/help/user-guide.md", include_in_schema=False)
    def user_guide():
        return FileResponse(ROOT / "docs" / "USER_GUIDE.md", media_type="text/markdown; charset=utf-8")

    @app.get("/help/user-guide.en.md", include_in_schema=False)
    def english_user_guide():
        return FileResponse(ROOT / "docs" / "USER_GUIDE.en.md", media_type="text/markdown; charset=utf-8")

    @app.get("/")
    def index():
        # Bootstrap the private WebView with its saved preference before mounting any UI.
        language = get_language()['language']
        page = (ROOT / "frontend" / "index.html").read_text(encoding='utf-8')
        return HTMLResponse(page.replace('<html lang="zh-CN">', f'<html lang="{language}">', 1),
                            headers={'Cache-Control': 'no-store'})

    return app
