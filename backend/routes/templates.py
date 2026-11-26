from fastapi import APIRouter, HTTPException

from ..analysis_templates import QueryPreview, TemplateCreate, TemplateEdit, TemplateRun, TemplateScan
from ..template_queries import review_bindings


def template_routes(templates):
    router = APIRouter(prefix="/api")

    @router.get("/analysis-templates")
    def list_analysis_templates():
        return templates.catalog()

    @router.get("/messages/{message_id}/analysis-template")
    def analysis_template_seed(message_id: str):
        return templates.seed(message_id)

    @router.post("/analysis-templates", status_code=201)
    def create_analysis_template(body: TemplateCreate):
        return templates.save(body)

    @router.post("/analysis-templates/parameters/recognize")
    def recognize_template_parameters(body: TemplateScan):
        return templates.scan(body)

    @router.post("/analysis-templates/queries/preview")
    def preview_template_queries(body: QueryPreview):
        try:
            return review_bindings(
                [q.model_dump() for q in body.queries], body.prompt, body.samples, body.choices
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/analysis-templates/{template_id}")
    def get_analysis_template(template_id: str):
        return templates.get(template_id)

    @router.put("/analysis-templates/{template_id}")
    def edit_analysis_template(template_id: str, body: TemplateEdit):
        return templates.save(body, template_id)

    @router.delete("/analysis-templates/{template_id}")
    def delete_analysis_template(template_id: str, revision: int):
        return templates.delete(template_id, revision)

    @router.get("/analysis-templates/{template_id}/runs")
    def analysis_template_runs(template_id: str):
        return templates.runs(template_id)

    @router.post("/analysis-templates/{template_id}/runs")
    def run_analysis_template(template_id: str, body: TemplateRun):
        return templates.run(template_id, body)

    return router
