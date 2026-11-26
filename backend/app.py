"""Loopback-only HTTP API, shared by the native desktop window and development preview."""

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .analysis_templates import AnalysisTemplates
from .cancellation import authorization_scope
from .chat import ChatService
from .database import Database
from .language import language_scope
from .licensing import LicenseError, LicenseGuard, require_runtime_license
from .powerbi import PowerBIService
from .releases import APP_VERSION, UpdateError
from .routes.artifacts import artifact_routes
from .routes.conversations import conversation_routes
from .routes.exports import export_routes
from .routes.images import image_routes
from .routes.models import model_routes
from .routes.preferences import preference_routes, saved_language
from .routes.templates import template_routes
from .semantics import SemanticService
from .settings import SettingsService

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
    semantics = SemanticService(db, powerbi)
    powerbi.semantics = semantics
    chat = ChatService(db, powerbi)
    templates = AnalysisTemplates(db, powerbi, chat, settings)

    def dictionary_in_use(model_id, *, include_sync=True):
        for conversation_id, lock in list(chat.locks.items()):
            if conversation_id in chat.updating_context:
                continue
            if lock.locked() and (include_sync or conversation_id not in chat.syncing):
                row = db.one("SELECT model_id FROM conversations WHERE id=?", (conversation_id,))
                if row and row["model_id"] == model_id:
                    return True
        return False

    semantics.in_use = dictionary_in_use

    def snapshot_in_use(model_id):
        return model_id in semantics.jobs or dictionary_in_use(model_id, include_sync=False)

    powerbi.snapshot_in_use = snapshot_in_use
    app = FastAPI(title="Power BI Assistant", version=APP_VERSION, docs_url="/api/docs", redoc_url=None)
    app.state.license_info = license_info
    app.state.updates = None  # Enabled by the installed native window once it is ready.
    pending_writes = 0
    restarting = False
    app.state.db, app.state.settings, app.state.powerbi, app.state.chat = db, settings, powerbi, chat
    app.state.semantics = semantics
    app.state.images = chat.images
    app.state.analysis_templates = templates
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
            image_upload = request.method == "POST" and request.url.path == "/api/images"
            if not image_upload and "application/json" not in request.headers.get("content-type", ""):
                return JSONResponse({"detail": "请求必须使用 JSON"}, status_code=415)
            semantic_upload = (
                request.method == "POST"
                and request.url.path.startswith("/api/models/")
                and request.url.path.endswith(("/semantics/import", "/semantics/import-preview"))
            )
            if not image_upload and not semantic_upload and len(await request.body()) > 4 * 1024 * 1024:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
        try:
            if request.url.path.startswith("/api/"):
                check_authorization()
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        else:
            writing = request.method in (
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
            ) and not request.url.path.startswith("/api/updates/")
            if writing and restarting:
                return JSONResponse({"detail": "助手正在重启更新，请稍候。"}, status_code=503)
            if writing:
                pending_writes += 1
            try:
                language = "en" if request.headers.get("X-UI-Language") == "en" else "zh-CN"
                with (
                    authorization_scope(check_authorization if license_guard else None),
                    language_scope(language),
                ):
                    response = await call_next(request)
            finally:
                if writing:
                    pending_writes -= 1
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "version": APP_VERSION,
            "storage": "sqlite",
            "platform": "desktop",
            "license": {
                "type": "trial" if license_info.issued_at is not None else "permanent",
                "issued_at": license_info.issued_at,
                "expires_at": license_info.expires_at,
                "summary": license_info.summary,
            }
            if license_info
            else {"type": "development"},
        }

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
        if (
            restarting
            or pending_writes
            or semantics.jobs
            or any(lock.locked() for lock in list(chat.locks.values()))
        ):
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

    app.include_router(image_routes(chat.images))
    app.include_router(preference_routes(db, settings))
    app.include_router(model_routes(db, powerbi, semantics, settings, chat.analysis.fields))
    app.include_router(template_routes(templates))
    app.include_router(conversation_routes(db, chat, powerbi, settings, templates))
    app.include_router(export_routes(db, chat))
    app.include_router(artifact_routes(db, powerbi))

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
        language = saved_language(db)["language"]
        page = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            page.replace('<html lang="zh-CN">', f'<html lang="{language}">', 1),
            headers={"Cache-Control": "no-store"},
        )

    return app
