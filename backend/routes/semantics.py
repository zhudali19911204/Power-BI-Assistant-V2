from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import semantic_transfer
from ..business_dictionary import BusinessEdit, OverviewEdit
from ..dictionary_library import DictionaryNameEdit
from ..semantics import SemanticBuildInput, SemanticEdit, SemanticReuseInput


def semantic_routes(semantics, settings):
    router = APIRouter()

    @router.get("/models/{model_id}/semantics")
    def get_semantics(model_id: str):
        return semantics.get(model_id)

    @router.get("/models/{model_id}/semantics/export")
    def export_semantics(model_id: str):
        document = semantic_transfer.export_file(semantics, model_id)
        name = document["dictionary_name"] or document["model_name"] or "模型"
        name = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name)[:100]
        filename = quote(name + "-语义层.json", safe="")
        return JSONResponse(
            document,
            headers={
                "Content-Disposition": f"attachment; filename=\"semantic-layer.json\"; filename*=UTF-8''{filename}"
            },
        )

    async def load_semantics_file(model_id, request, expected=None):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > semantic_transfer.MAX_IMPORT_BYTES:
                raise HTTPException(413, "语义层文件超过 64 MB")
            data.extend(chunk)

        def load():
            return semantic_transfer.import_file(
                semantics, model_id, semantic_transfer.read_file(bytes(data)), expected=expected
            )

        return await run_in_threadpool(load)

    @router.post("/models/{model_id}/semantics/import-preview")
    async def preview_semantics_file(model_id: str, request: Request):
        return await load_semantics_file(model_id, request)

    @router.post("/models/{model_id}/semantics/import")
    async def import_semantics(model_id: str, request: Request, updated_at: str):
        return await load_semantics_file(model_id, request, updated_at)

    @router.post("/models/{model_id}/semantics/build")
    def build_semantics(model_id: str, body: SemanticBuildInput):
        return semantics.start(model_id, settings, body.llm_profile_id, body.mode)

    @router.post("/models/{model_id}/semantics/reuse")
    def reuse_semantics(model_id: str, body: SemanticReuseInput):
        return semantics.reuse(model_id, body.source_model_id, body.source_updated_at)

    @router.get("/semantic-dictionaries")
    def semantic_dictionaries(current_model_id: str | None = None):
        return semantics.library.catalog(current_model_id)

    @router.get("/models/{model_id}/semantics/reuse-preview")
    def preview_semantic_reuse(model_id: str, source_model_id: str):
        return semantics.library.preview(model_id, source_model_id)

    @router.delete("/semantic-dictionaries/{dictionary_id}")
    def delete_semantic_dictionary(
        dictionary_id: str, current_model_id: str | None = None, updated_at: str | None = None
    ):
        return semantics.library.delete(
            dictionary_id, current_model_id=current_model_id, updated_at=updated_at
        )

    @router.put("/semantic-dictionaries/{dictionary_id}/name")
    def rename_semantic_dictionary(dictionary_id: str, body: DictionaryNameEdit):
        return semantics.library.rename(dictionary_id, body)

    @router.put("/models/{model_id}/semantics/business/overview")
    def edit_business_overview(model_id: str, body: OverviewEdit):
        return semantics.business.edit(model_id, body)

    @router.post("/models/{model_id}/semantics/business/items")
    def create_business_item(model_id: str, body: BusinessEdit):
        return semantics.business.edit(model_id, body)

    @router.put("/models/{model_id}/semantics/business/items/{item_id}")
    def edit_business_item(model_id: str, item_id: str, body: BusinessEdit):
        return semantics.business.edit(model_id, body, item_id)

    @router.delete("/models/{model_id}/semantics/business/items/{item_id}")
    def delete_business_item(model_id: str, item_id: str, revision: int | None = None):
        return semantics.business.delete(model_id, item_id, revision)

    @router.put("/models/{model_id}/semantics/{entry_id}")
    def edit_semantics(model_id: str, entry_id: str, body: SemanticEdit):
        return semantics.edit(model_id, entry_id, body)

    return router
