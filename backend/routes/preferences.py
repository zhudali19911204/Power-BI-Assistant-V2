from fastapi import APIRouter, HTTPException

from ..engine.llm.base import user
from ..schemas import LanguagePreference, LayoutInput, LLMProfileInput, ReasoningInput, SettingsInput


def saved_language(db):
    return db.one("SELECT language FROM ui_language WHERE id=1") or {"language": "zh-CN"}


def preference_routes(db, settings):
    router = APIRouter(prefix="/api")

    @router.get("/ui/language")
    def get_language():
        return saved_language(db)

    @router.put("/ui/language")
    def save_language(body: LanguagePreference):
        db.execute("INSERT OR REPLACE INTO ui_language (id,language) VALUES (1,?)", (body.language,))
        return body

    @router.get("/ui/layout")
    def get_layout():
        row = db.one("SELECT context_width,sidebar_width FROM ui_layout WHERE id=1")
        return row if row else LayoutInput().model_dump()

    @router.put("/ui/layout")
    def save_layout(body: LayoutInput):
        # Desktop uses a random port and private WebView storage on each launch.
        # Persist UI preferences independently of model service credentials.
        with db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT context_width,sidebar_width FROM ui_layout WHERE id=1"
            ).fetchone()
            values = (dict(row) if row else LayoutInput().model_dump()) | body.model_dump(exclude_unset=True)
            connection.execute(
                "INSERT OR REPLACE INTO ui_layout (id,context_width,sidebar_width) VALUES (1,?,?)",
                (values["context_width"], values["sidebar_width"]),
            )
        return values

    @router.get("/settings")
    def get_settings():
        return settings.public()

    @router.put("/settings")
    def save_settings(body: SettingsInput):
        return settings.save(body)

    @router.post("/settings/test")
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
            raise HTTPException(
                422, f"连接失败（{type(exc).__name__}），请检查服务器地址、模型名称、Key 和网络"
            ) from exc

    @router.get("/llm/profiles")
    def llm_profiles():
        return settings.profiles()

    @router.post("/llm/profiles", status_code=201)
    def create_llm_profile(body: LLMProfileInput):
        return settings.save_profile(body)

    @router.put("/llm/profiles/{profile_id}")
    def update_llm_profile(profile_id: str, body: LLMProfileInput):
        return settings.save_profile(body, profile_id)

    @router.patch("/llm/profiles/{profile_id}/reasoning")
    def update_llm_reasoning(profile_id: str, body: ReasoningInput):
        return settings.save_reasoning(profile_id, body)

    @router.post("/llm/profiles/{profile_id}/activate")
    def activate_llm_profile(profile_id: str):
        return settings.activate(profile_id)

    @router.post("/llm/profiles/{profile_id}/test")
    def test_llm_profile(profile_id: str):
        return test_model_service(profile_id)

    @router.delete("/llm/profiles/{profile_id}")
    def delete_llm_profile(profile_id: str):
        return settings.delete_profile(profile_id)

    return router
