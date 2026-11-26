from fastapi import APIRouter

from ..engine.context.live_source import find_instances
from ..fields import FieldPreviewInput
from ..reports import reports_for_instances
from ..schemas import ConnectInput
from .semantics import semantic_routes


def model_routes(db, powerbi, semantics, settings, fields):
    router = APIRouter(prefix="/api")

    @router.get("/desktop/instances")
    def instances():
        engines = find_instances()
        reports = reports_for_instances(engines)
        return [{"port": i.port, "pid": i.pid, "report": reports.get(i.pid, {})} for i in engines]

    @router.post("/models/connect")
    def connect(body: ConnectInput):
        with powerbi.lock:
            model = powerbi.connect(body.port, body.report_name)
            semantics.on_connect(model, settings)
            return powerbi.get(model["id"])

    @router.post("/models/demo")
    def demo():
        return powerbi.demo()

    @router.get("/models")
    def models():
        return [
            powerbi.get(r["id"])
            for r in db.all("SELECT id FROM models WHERE context!='null' ORDER BY created_at DESC LIMIT 50")
        ]

    @router.get("/models/{model_id}")
    def model(model_id: str):
        return powerbi.get(model_id)

    @router.get("/models/{model_id}/connection")
    def model_connection(model_id: str):
        return powerbi.reconnector.poll(model_id)

    @router.post("/models/{model_id}/fields/preview")
    def preview_field(model_id: str, body: FieldPreviewInput):
        return fields.preview(model_id, body)

    router.include_router(semantic_routes(semantics, settings))

    @router.post("/models/{model_id}/refresh")
    def refresh(model_id: str):
        with powerbi.lock:
            model = powerbi.refresh(model_id)
            semantics.on_connect(model, settings)
            return powerbi.get(model["id"])

    return router
