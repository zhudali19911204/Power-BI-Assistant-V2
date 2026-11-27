"""Turn saved query evidence into a streamed, source-attributed answer."""

import time

from fastapi import HTTPException

from .analysis_budget import evidence_context
from .cancellation import GenerationCancelled
from .clarifications import card_markdown
from .database import encode
from .engine.llm.base import ChatMessage
from .engine.llm.diagnostics import GenerationReport
from .language import OPEN_QUESTION_LABELS, wording


class EmptySummary(HTTPException):
    """The provider completed without producing conclusion text."""


def summarize(run, planner, queries):
    successful = [record for record in run.evidence if record["ok"] and record.get("run_verified")]
    if not successful:
        text = wording(
            "### 查询未完成\n\n本轮未取得可用于分析的数据，无法据此给出数值结论。请查看下方具体失败原因；格式错误或查询错误不代表模型中没有数据。",
            "### Query incomplete\n\nNo usable data was returned. See the query errors below before drawing numerical conclusions.",
        )
        run.display += text
        yield encode({"type": "delta", "text": text}) + "\n"
    elif (
        run.commands and len(successful) == len(run.commands) and all(q["row_count"] == 0 for q in successful)
    ):
        labels = wording("、", ", ").join("[" + q["label"] + "]" for q in successful)
        text = wording(
            f"本次固定查询 {labels} 均执行成功，在当前参数下返回 0 行。可在下方核对查询及参数。",
            f"The saved queries {labels} all ran successfully and returned 0 rows for these parameters. See each query and its values below.",
        )
        run.display += text
        yield encode({"type": "delta", "text": text}) + "\n"
    elif queries.pending_card and planner.immediate_question:
        # The card becomes actionable when this reply completes. Keep the
        # actual Q results available without blocking it on a summary call.
        labels = wording("、", ", ").join("[" + q["label"] + "]" for q in successful)
        note = wording(
            "\n\n已完成 " + labels + "，可在下方查看结果。确认筛选字段后继续分析。",
            "\n\nResults for " + labels + " are available below. Confirm the filter field to continue.",
        )
        run.display += note
        yield encode({"type": "delta", "text": note}) + "\n"
    else:
        try:
            yield from stream_conclusion(run, planner, queries, successful)
        except GenerationCancelled:
            raise
        except Exception as exc:
            if not (planner.format_detail or run.checkpoint):
                raise
            sources = wording("、", ", ").join(
                f"[{q['label']}] {q['title']} ({q['row_count']})" for q in successful
            )
            text = (
                wording(
                    "\n\n模型总结未完成，以下查询已取得实际结果（括号内为保留行数）：",
                    "\n\nThe summary could not finish. Saved results (row counts): ",
                )
                + sources
            )
            if queries.unanswered:
                text += wording("。待完成：", ". Remaining: ") + wording("、", ", ").join(queries.unanswered)
            run.display += text
            yield encode({"type": "delta", "text": text}) + "\n"
            if not isinstance(exc, EmptySummary):
                raise
    if not successful and queries.stop_reason:
        note = "\n\n" + queries.stop_reason + wording("。", ".")
        run.display += note
        yield encode({"type": "delta", "text": note}) + "\n"
    if queries.pending_card:
        note = "\n\n" + card_markdown(queries.pending_card)
        run.display += note
        yield encode({"type": "delta", "text": note}) + "\n"


def stream_conclusion(run, planner, queries, successful):
    yield run.status("已取得真实数据，正在形成分析结论")
    summary_evidence = evidence_context([r for r in run.evidence if not r["ok"] or r.get("run_verified")])
    summary = run.messages[:] + [
        ChatMessage(
            "user",
            "以下为应用实际执行的查询结果。仅依据这些 evidence 回答原问题。\n"
            + encode(
                {
                    "scope": queries.initial_scope,
                    "evidence": summary_evidence,
                    **(
                        {"template_parameters": run.template.values}
                        if run.commands and run.template.values
                        else {}
                    ),
                    "analysis_state": {
                        "batches": run.batch_count,
                        "queries": run.query_count,
                        "attempts": run.attempt_count,
                        "stop_reason": queries.stop_reason,
                        "remaining_questions": queries.unanswered,
                        "clarification": queries.pending_card,
                        "minimal_step": planner.minimal_used,
                    },
                }
            ),
        )
    ]
    summary_system, _ = planner.analysis_system(for_summary=True)
    if any(q.get("recovery") for q in successful):
        summary_system += "\n有 recovery 的结果是替代取数。只用实际返回的基础值作比较，注明仍缺的期间或维度，并引用成功查询的编号。"
    last_save = 0
    for summary_attempt in range(2):
        answer = ""
        report = GenerationReport()
        options = {}
        if summary_attempt:
            # The same successful evidence and attached images are reused. This
            # does not execute DAX again or change the saved model configuration.
            summary = summary + [
                ChatMessage(
                    "user",
                    "上次请求没有返回结论正文。请直接根据上面的实际 evidence，用指定的回复语言简短给出关键结论、"
                    "来源编号和仍无法确定的内容，不需要重新查询。不要重复分析口径，不要编造缺失数字。",
                )
            ]
            if getattr(run.provider, "can_reduce_reasoning", False):
                options["reasoning_level"] = "low"
            elif getattr(run.provider, "can_disable_thinking", False):
                options["enable_thinking"] = False
        for event in run.generate("analysis_summary", summary_system, summary, report=report, **options):
            if event["type"] == "delta":
                answer += event["text"]
                run.display += event["text"]
                if len(run.display) > 160000:
                    raise HTTPException(422, "分析回复过长，请缩小问题范围")
                if time.monotonic() - last_save > 1:
                    run.save_partial()
                    last_save = time.monotonic()
                yield encode(event) + "\n"
            elif event["type"] == "heartbeat":
                yield encode(event) + "\n"
            elif event["type"] == "status":
                yield run.status(event["text"])
        if answer.strip():
            if report.interruption():
                raise HTTPException(
                    422,
                    report.interruption()
                    + wording(
                        "；已保留已输出的文字和实际查询结果",
                        " The partial reply and query results are saved.",
                    ),
                )
            break
        retryable = report.finish_reason in (None, "stop", "length")
        if summary_attempt or not retryable:
            retried = (
                wording("；已复用查询结果重试一次", " Retried once using the existing results.")
                if summary_attempt
                else ""
            )
            error_type = EmptySummary if retryable else HTTPException
            raise error_type(
                422,
                report.empty_message()
                + retried
                + wording("；已保留实际查询结果", " The query results are saved."),
            )
        yield run.status(
            report.empty_message()
            + wording(
                "；正在复用查询结果重新生成结论（重试 1/1）",
                " Retrying the summary with the saved results (1/1).",
            )
        )
    # Sources remain visible even when a compatible model omits inline citations.
    sources = wording("、", ", ").join(
        "[" + q["label"] + "] " + " ".join(q["title"].split()) for q in successful
    )
    footer = wording(
        "\n\n---\n\n**本轮数据依据**：" + sources + "。查询和实际结果见下方。",
        "\n\n---\n\n**Data sources**: " + sources + ". See the queries and results below.",
    )
    completed = {q["label"] for q in successful}
    completed.update(q["recovery"]["for_query"] for q in successful if q.get("recovery"))
    missing = [label for label in queries.planned_labels if label not in completed]
    if missing:
        footer += wording(
            " " + "、".join(missing) + " 未取得成功结果。",
            " No successful results for " + ", ".join(missing) + ".",
        )
    if any(q["truncated"] for q in successful):
        footer += wording(
            " 本轮部分结果已截断，相关结论只适用于已保留的数据。",
            " Some results were truncated. Conclusions apply to the saved rows.",
        )
    if any(q["context_truncated"] for q in summary_evidence):
        footer += wording(
            " 部分已保存结果未完整提供给总结模型，结论只依据已提供的行；完整保留结果可在下方查看。",
            " The summary is based on the rows supplied to the model. All saved rows are available below.",
        )
    if queries.stop_reason:
        footer += " " + queries.stop_reason + wording("。", ".")
    if queries.unanswered:
        footer += (
            " "
            + wording(*OPEN_QUESTION_LABELS)
            + wording("；", "; ").join(queries.unanswered)
            + wording("。", ".")
        )
    run.display += footer
    yield encode({"type": "delta", "text": footer}) + "\n"
