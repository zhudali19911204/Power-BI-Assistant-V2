import base64
import csv
import io
import json

from fastapi import APIRouter, HTTPException, Response

from ..database import encode
from ..language import Language, language_scope, wording


def export_routes(db, chat):
    router = APIRouter(prefix="/api")

    @router.get("/conversations/{conversation_id}/export")
    def export(conversation_id: str, language: Language = "zh-CN"):
        with language_scope(language):
            return export_markdown(conversation_id)

    def export_markdown(conversation_id: str):
        conv = chat.conversation(conversation_id)
        text = "# " + conv["title"] + "\n\n"
        for message in chat.messages(conversation_id):
            role = wording("你", "You") if message["role"] == "user" else wording("助手", "Assistant")
            text += f"## {role}\n\n"
            if message.get("model_stale"):
                text += wording(
                    "> 此回复生成于模型同步前，内容和查询结果作为历史保留。\n\n",
                    "> This reply and its results predate the last model sync.\n\n",
                )
            text += message["content"] + "\n\n"
            for picture in message.get("images", []):
                row = chat.images.get(picture["id"])
                text += (
                    wording("![附图](data:", "![Attachment](data:")
                    + row["mime_type"]
                    + ";base64,"
                    + base64.b64encode(row["data"]).decode("ascii")
                    + ")\n\n"
                )
            for artifact in message["artifacts"]:
                vr = artifact["validation"]
                check = wording("通过", "passed") if vr["ok"] else wording("未通过", "failed")
                live = (
                    wording("已实跑", "tested in Desktop")
                    if vr["run_verified"]
                    else wording("未经实跑", "no live run")
                )
                text += wording("验证：{}；{}", "Validation: {} · {}").format(check, live) + "\n\n"
            for record in message.get("field_previews", []):
                text += (
                    wording("### 字段取值检查 ", "### Field value check ")
                    + f"{record['label']} · {record['table']}[{record['column']}]\n\n"
                )
                if not record["ok"]:
                    text += wording("检查未完成：", "Check incomplete: ") + record["error"] + "\n\n"
                    continue
                profile = record["profile"]
                end = wording("。\n\n", ".\n\n")
                scope = profile["scope"]
                if scope == "当前用户可读取的全模型范围，未应用报表页面筛选":
                    scope = wording(scope, "Entire accessible model, excluding report page filters")
                if "confirmed_value" in record:
                    text += (
                        wording("用户已确认 ", "Confirmed by user: ")
                        + encode(record["confirmed_value"])
                        + end
                    )
                if "adopted_value" in record:
                    text += wording("本次采用 ", "Used in this run: ") + encode(record["adopted_value"]) + end
                if record.get("scope_term"):
                    text += (
                        wording("核对口径 {}，精确搜索 {}", "Scope term: {}. Exact search: {}").format(
                            encode(record["scope_term"]), encode(record["search_terms"])
                        )
                        + end
                    )
                    matches = [entry["value"] for entry in profile["values"]]
                    text += (
                        wording("实际命中 ", "Matches: ") + encode(matches)
                        if matches
                        else wording("候选字段未命中", "No matching values")
                    ) + end
                    text += scope + wording("。批量查询见 ", ". Batch query: ") + record["query_label"] + end
                    for query in profile["dax_queries"]:
                        text += "```dax\n" + query + "\n```\n\n"
                    continue
                stats = profile["statistics"]
                text += scope + end
                text += (
                    wording(
                        "总行数：{row_count}；不同取值（含空值）：{distinct_count}；空值行数：{blank_count}；空字符串：{empty_string_count}",
                        "Rows: {row_count}. Distinct values including blanks: {distinct_count}. Blank rows: {blank_count}. Empty strings: {empty_string_count}",
                    ).format(**stats)
                    + end
                )
                text += wording("取值样例：", "Sample values: ") + encode(profile["samples"]) + "\n\n"
                if profile["search_terms"]:
                    text += wording("搜索词：", "Search terms: ") + encode(profile["search_terms"]) + "\n\n"
                text += wording("| 取值 | 出现行数 |", "| Value | Row count |") + "\n| --- | --- |\n"
                for entry in profile["values"]:
                    value = encode(entry["value"]).replace("|", "\\|").replace("<", "&lt;")
                    suffix = wording("（值已截断）", " (truncated)") if entry["value_truncated"] else ""
                    text += f"| {value}{suffix} | {entry['count']} |\n"
                text += "\n" + (
                    wording(
                        "结果已截断，不能代表全部取值。\n\n",
                        "Results were truncated. More values may exist.\n\n",
                    )
                    if profile["truncated"]
                    else ""
                )
                for query in profile["dax_queries"]:
                    text += "```dax\n" + query + "\n```\n\n"
            for result in message.get("analysis", []):
                text += (
                    f"### {result['label']} · {result['title']}"
                    + wording("（第 {} 次执行）", " (attempt {})").format(result["attempt"])
                    + "\n\n"
                )
                if recovery := result.get("recovery"):
                    text += (
                        wording(
                            "替代 {for_query}。{missing}。{basis}",
                            "Alternative to {for_query}. {missing}. {basis}",
                        ).format(**recovery)
                        + "\n\n"
                    )
                for note in result.get("convention_exceptions", {}).values():
                    text += (
                        wording(
                            "分析约定「{title}」　{reason}", "Analysis convention “{title}”: {reason}"
                        ).format(**note)
                        + "\n\n"
                    )
                text += "```dax\n" + result["dax"] + "\n```\n\n"
                if result.get("parameters"):
                    text += (
                        wording("本次查询参数", "Query parameters")
                        + "\n\n```json\n"
                        + encode(result["parameters"])
                        + "\n```\n\n"
                    )
                if not result["ok"]:
                    text += wording("查询未成功：", "Query failed: ") + result["error"] + "\n\n"
                    continue
                limit = (
                    wording("结果已截断", "truncated")
                    if result["truncated"]
                    else wording("返回结果未截断", "complete")
                )
                text += (
                    wording("实际结果：保留 {} 行；{}。", "Query results: {} rows saved ({})").format(
                        result["row_count"], limit
                    )
                    + "\n\n"
                )
                columns = result["columns"]

                def cell(value):
                    return (
                        str(wording("空值", "Blank") if value is None else value)
                        .replace("|", "\\|")
                        .replace("\n", " ")
                    )

                if columns:
                    text += (
                        "| "
                        + " | ".join(map(cell, columns))
                        + " |\n| "
                        + " | ".join("---" for _ in columns)
                        + " |\n"
                    )
                    for row in result["rows"]:
                        text += "| " + " | ".join(cell(row.get(c)) for c in columns) + " |\n"
                    text += "\n"
        return Response(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="conversation-{conversation_id[:8]}.md"'},
        )

    @router.get("/analysis/{query_id}/csv")
    def export_analysis(query_id: str):
        record = db.one("SELECT result FROM analysis_queries WHERE id=?", (query_id,))
        if not record:
            raise HTTPException(404, "查询结果不存在")
        result = json.loads(record["result"])
        if not result["ok"]:
            raise HTTPException(409, "失败的查询没有可导出的结果")
        target = io.StringIO(newline="")
        writer = csv.writer(target)

        def safe(value):
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                return "'" + value
            return "" if value is None else value

        writer.writerow([safe(c) for c in result["columns"]])
        for row in result["rows"]:
            writer.writerow([safe(row.get(c)) for c in result["columns"]])
        return Response(
            target.getvalue().encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="analysis-{query_id[:8]}.csv"'},
        )

    return router
