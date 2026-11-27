from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..analysis_checkpoint import load_checkpoint, original_request
from ..database import now, uid
from ..powerbi import context_from_dict
from ..schemas import (
    AnalysisResumeInput,
    CancelGenerationInput,
    ContextPreference,
    ConversationInput,
    MessageInput,
)
from ..streaming import BackgroundStream


def conversation_routes(db, chat, powerbi, settings, templates):
    router = APIRouter(prefix="/api")

    @router.get("/conversations")
    def conversations():
        return [
            row | {"generating": chat.generation_status(row["id"])["running"]}
            for row in db.all("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 300")
        ]

    @router.post("/conversations", status_code=201)
    def create_conversation(body: ConversationInput):
        model = powerbi.get(body.model_id)
        if body.mode == "analysis" and model["source"] != "desktop":
            raise HTTPException(409, "数据分析需要真实 Desktop 模型，请先连接本机报表")
        if body.mode == "mquery" and not context_from_dict(model["context"]).has_query(body.query_name):
            raise HTTPException(422, "请选择模型中存在的 Power Query 查询")
        conv_id, stamp = uid(), now()
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO conversations VALUES (?,?,?,?,?,?,?)",
                (conv_id, body.title, body.mode, body.model_id, body.query_name, stamp, stamp),
            )
            if not body.auto_compress:
                conn.execute(
                    "INSERT INTO conversation_contexts (conversation_id,automatic) VALUES (?,0)", (conv_id,)
                )
        return chat.conversation(conv_id)

    @router.get("/conversations/{conversation_id}/context")
    def conversation_context(conversation_id: str):
        return chat.history.window(conversation_id).usage

    @router.put("/conversations/{conversation_id}/context")
    def context_preference(conversation_id: str, body: ContextPreference):
        return chat.update_context(conversation_id, automatic=body.automatic)

    @router.post("/conversations/{conversation_id}/context/compress")
    def compress_context(conversation_id: str):
        return chat.update_context(conversation_id, compress=True)

    @router.get("/conversations/{conversation_id}/messages")
    def messages(conversation_id: str):
        return chat.messages(conversation_id)

    @router.get("/conversations/{conversation_id}")
    def get_conversation(conversation_id: str):
        return chat.conversation(conversation_id)

    @router.post("/conversations/{conversation_id}/refresh-model")
    def refresh_conversation_model(conversation_id: str):
        return chat.refresh_model(conversation_id, settings)

    @router.get("/conversations/{conversation_id}/generation")
    def generation_status(conversation_id: str):
        chat.conversation(conversation_id)
        return chat.generation_status(conversation_id)

    @router.post("/conversations/{conversation_id}/cancel")
    def cancel_generation(conversation_id: str, body: CancelGenerationInput):
        return chat.cancel(conversation_id, body.generation_id)

    @router.delete("/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str):
        chat.conversation(conversation_id)
        lock = chat.locks.get(conversation_id)
        if lock and lock.locked():
            raise HTTPException(409, "请等待生成完成后删除会话")
        db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        return {"ok": True}

    @router.post("/conversations/{conversation_id}/messages")
    def send_message(conversation_id: str, body: MessageInput):
        source = templates.continue_preparation(conversation_id, body)
        if source is None:
            source = chat.begin(conversation_id, body, settings)
        task = BackgroundStream(source)
        return StreamingResponse(
            task.events(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"}
        )

    @router.post("/conversations/{conversation_id}/messages/{message_id}/resume")
    def resume_analysis(conversation_id: str, message_id: str, body: AnalysisResumeInput):
        saved = load_checkpoint(db, conversation_id, message_id, body.version)
        request = original_request(saved, body)
        source = (
            templates.resume_analysis(conversation_id, request, saved)
            if saved["template"]
            else chat.begin(conversation_id, request, settings, checkpoint=saved)
        )
        return StreamingResponse(
            BackgroundStream(source).events(),
            media_type="application/x-ndjson",
            headers={"X-Accel-Buffering": "no"},
        )

    @router.post("/messages/{message_id}/validate")
    def revalidate_message(message_id: str):
        return chat.revalidate(message_id)

    return router
