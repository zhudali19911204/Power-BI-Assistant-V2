import json
from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from ..database import encode, now
from ..engine.context.live_writer import LiveDesktopWriter
from ..engine.dax.live_eval import LiveDesktopEvaluator
from ..schemas import ApplyInput
from ..validation import validate


def artifact_routes(db, powerbi):
    router = APIRouter(prefix="/api")

    @router.post("/artifacts/{artifact_id}/apply")
    def apply_artifact(artifact_id: str, body: ApplyInput):
        obj = db.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        if not obj:
            raise HTTPException(404, "代码产物不存在")
        if obj["kind"] not in ("dax_measure", "dax_table", "dax_column"):
            raise HTTPException(409, "Power Query 请复制到高级编辑器，不支持外部写回")
        if obj["kind"] not in ("dax_measure", "dax_column") and (
            body.name is not None or body.home_table is not None
        ):
            raise HTTPException(422, "名称与存放表设置仅适用于度量值和计算列")
        if obj["kind"] == "dax_column" and body.overwrite:
            raise HTTPException(409, "计算列仅支持新建，不覆盖已有字段，请修改列名")
        prior = json.loads(obj["validation"])
        if not prior.get("ok") or not prior.get("run_verified"):
            raise HTTPException(409, "只有实跑验证通过的代码才能写入模型")
        with powerbi.lock:
            conversation = db.one(
                "SELECT c.model_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=?",
                (obj["message_id"],),
            )
            model, ctx = powerbi.require_live(conversation["model_id"], unchanged=True)
            target = {
                **obj,
                "name": body.name if body.name is not None else obj["name"],
                "home_table": body.home_table if body.home_table is not None else obj["home_table"],
            }
            if obj["kind"] == "dax_column":
                if target["home_table"] not in ctx.tables:
                    raise HTTPException(409, "指定的所属表已不存在，请同步模型后重新选择")
                if any(
                    c.name.casefold() == target["name"].casefold() for c in ctx.tables[target["home_table"]]
                ):
                    raise HTTPException(409, "所属表中已有同名字段，计算列仅支持新建，请修改列名")
                if any(
                    m.table == target["home_table"] and m.name.casefold() == target["name"].casefold()
                    for m in ctx.measures
                ):
                    raise HTTPException(409, "所属表中已有同名度量值，请修改列名")
            if obj["kind"] == "dax_measure":
                if not ctx.has_table(target["home_table"]):
                    raise HTTPException(409, "指定的存放表已不存在，请同步模型后重新选择")
                existing = [m for m in ctx.measures if m.name.casefold() == target["name"].casefold()]
                if any(m.table != target["home_table"] for m in existing):
                    table = next(m.table for m in existing if m.table != target["home_table"])
                    raise HTTPException(
                        409, f"同名度量值已在表“{table}”中，请修改名称，或选择该表并明确允许覆盖"
                    )
                if existing and not body.overwrite:
                    raise HTTPException(409, "所选表中已有同名度量值，请修改名称或勾选允许覆盖")
                if any(
                    c.name.casefold() == target["name"].casefold()
                    for c in ctx.tables.get(target["home_table"], [])
                ):
                    raise HTTPException(409, "所选表中已有同名字段，请为度量值使用其他名称")
            evaluator = LiveDesktopEvaluator(model["port"])
            fresh = validate(ctx, [target], target["home_table"], evaluator, prior.get("calibration", []))[0]
            if not fresh["ok"] or not fresh["run_verified"]:
                raise HTTPException(409, "写入前重新验证失败，请重新生成：" + "; ".join(fresh["errors"]))
            writer = LiveDesktopWriter(model["port"])
            if obj["kind"] == "dax_table":
                if ctx.has_table(obj["name"]):
                    raise HTTPException(409, "同名表已存在，请使用新表名；不支持覆盖已有表")
                result = writer.write_calculated_table(obj["name"], obj["expression"], overwrite=False)
            elif obj["kind"] == "dax_column":
                result = writer.write_calculated_column(
                    target["home_table"], target["name"], target["expression"]
                )
            else:
                result = writer.write_measure(
                    target["home_table"], target["name"], target["expression"], overwrite=body.overwrite
                )
            payload = {
                **asdict(result),
                "kind": obj["kind"],
                "name": target["name"],
                "home_table": target["home_table"],
            }
            db.audit("write_model", payload, artifact_id)
            if not result.ok:
                raise HTTPException(409, result.detail)
            applied_at = now()
            db.execute(
                "UPDATE artifacts SET name=?,home_table=?,validation=?,applied_at=?,model_id=? WHERE id=?",
                (target["name"], target["home_table"], encode(fresh), applied_at, model["id"], artifact_id),
            )
            payload.update(applied_at=applied_at, validation=fresh)
            try:
                powerbi.update_after_write(model["id"], model["port"])
            except Exception:
                return {**payload, "detail": result.detail + "；模型上下文同步失败，请手动同步"}
            return payload

    return router
