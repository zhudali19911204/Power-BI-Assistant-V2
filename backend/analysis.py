"""Source-backed analysis: plan bounded DAX reads, execute in Desktop, explain actual results."""

import json
import logging
import math
import re
import time
from collections import Counter

from fastapi import HTTPException

from .analysis_budget import ROW_LIMIT, AnalysisBudget
from .analysis_planner import AnalysisPlanner
from .analysis_prompts import SUMMARY_SYSTEM as SUMMARY_SYSTEM
from .analysis_prompts import plan_system as plan_system
from .analysis_protocol import parse_plan as parse_plan
from .analysis_queries import AnalysisQueries
from .analysis_summary import summarize
from .cancellation import GenerationCancelled, cancelled_text, check_cancelled, close_cancelled_provider
from .clarifications import ClarificationQuestion, card_markdown
from .database import encode, now, uid
from .engine.context.live_source import _LiveSession, find_adomd_dll, find_instances
from .fields import FieldCheck, FieldService, field_ref
from .generation import generation_recorder
from .images import multimodal_error
from .language import wording
from .reconnection import transport_disconnected
from .streaming import completion_events, generation_timeout, task_events

logger = logging.getLogger(__name__)
RESULT_BYTES = 24000


def mask_dax(text):
    """Exclude strings, identifiers and comments before inspecting statement keywords."""
    output = []
    index = 0
    while index < len(text):
        start = index
        if text.startswith(("//", "--"), index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("查询包含未闭合的注释")
            index = end + 2
        elif text[index] in ('"', "'", "["):
            closing = "]" if text[index] == "[" else text[index]
            index += 1
            while index < len(text):
                if text[index] == closing:
                    if text[index : index + 2] == closing * 2:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise ValueError("查询包含未闭合的字符串或标识符")
        else:
            output.append(text[index])
            index += 1
            continue
        output.append(" " * (index - start))
    return "".join(output)


def readonly_query(text):
    text = text.strip().rstrip(";").strip()
    if not text or len(text) > 16000:
        raise ValueError("查询为空或过长")
    masked = mask_dax(text)
    if not re.match(r"^\s*(?:DEFINE|EVALUATE)\b", masked, re.I):
        raise ValueError("数据分析只接受 DEFINE / EVALUATE 形式的只读 DAX 查询")
    if len(re.findall(r"\bEVALUATE\b", masked, re.I)) != 1:
        raise ValueError("每次只允许一个 EVALUATE 结果集")
    if len(re.findall(r"\bDEFINE\b", masked, re.I)) > 1 or ";" in masked:
        raise ValueError("不允许批量或复合命令")
    if re.search(
        r"\b(?:CREATE|ALTER|DELETE|DROP|INSERT|UPDATE|TRUNCATE|REFRESH|PROCESS|EXEC|EXECUTE)\b", masked, re.I
    ):
        raise ValueError("分析查询不能包含修改模型或执行外部命令的语句")
    return text


class AnalysisService:
    def __init__(self, chat):
        self.chat = chat
        self.db = chat.db
        self.powerbi = chat.powerbi
        self.fields = FieldService(self.powerbi)

    def records(self, message_id):
        return [
            json.loads(row["result"])
            for row in self.db.all(
                "SELECT result FROM analysis_queries WHERE message_id=? ORDER BY created_at,rowid",
                (message_id,),
            )
        ]

    def field_records(self, message_id):
        return [
            json.loads(row["result"])
            for row in self.db.all(
                "SELECT result FROM field_observations WHERE message_id=? ORDER BY created_at,rowid",
                (message_id,),
            )
        ]

    @staticmethod
    def implicit_checks(plan, ctx, user_text):
        """Check real categorical fields named by a premature clarification or inferred filter."""
        checks = []
        text = (
            encode(plan.clarification.model_dump())
            if isinstance(plan.clarification, ClarificationQuestion)
            else str(plan.clarification or "")
        )
        names = Counter(c.name for cols in ctx.tables.values() for c in cols)
        for table, columns in ctx.tables.items():
            for column in columns:
                if column.dtype not in ("string", "auto", "unknown"):
                    continue
                ref = field_ref(table, column.name)
                plain = table + "[" + column.name.replace("]", "]]") + "]"
                short = "[" + column.name.replace("]", "]]") + "]"
                if plan.clarification and (
                    ref in text or plain in text or (names[column.name] == 1 and short in text)
                ):
                    checks.append(
                        FieldCheck(
                            table=table,
                            column=column.name,
                            reason=wording(
                                "先核对确认问题中引用的分类字段",
                                "Verify the category field referenced by the question",
                            ),
                        )
                    )
                elif plan.queries:
                    pattern = (
                        "(?:" + re.escape(ref) + "|" + re.escape(plain) + r')\s*={1,2}\s*"((?:[^"]|"")+)"'
                    )
                    values = [
                        m.group(1).replace('""', '"')
                        for q in plan.queries
                        for m in re.finditer(pattern, q.dax, re.I)
                    ]
                    inferred = [
                        value
                        for value in values
                        if value.casefold() not in user_text.casefold()
                        and not value.isdigit()
                        and len(value) <= 120
                    ]
                    if inferred:
                        checks.append(
                            FieldCheck(
                                table=table,
                                column=column.name,
                                search_terms=list(dict.fromkeys(inferred))[:3],
                                reason=wording(
                                    "核对推断出的筛选分类值", "Check the inferred category filter"
                                ),
                            )
                        )
        return checks

    def execute(self, model, query, parameters=None):
        result = {
            "ok": False,
            "run_verified": False,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "warnings": [],
            "error": "",
        }
        started = time.monotonic()
        try:
            dax = readonly_query(query)
            for read_attempt in range(2):
                try:
                    with self.powerbi.lock:
                        current, _ = self.powerbi.require_live(model["id"], unchanged=True)
                        dll = find_adomd_dll(find_instances())
                        if not dll:
                            raise HTTPException(409, "未找到 Desktop 的 ADOMD 客户端")
                        with _LiveSession(current["port"], dll) as session:
                            result["run_verified"] = True
                            data = session.query(
                                dax,
                                max_rows=ROW_LIMIT + 1,
                                **({"parameters": parameters} if parameters else {}),
                            )
                    if read_attempt:
                        result["warnings"].append("查询连接中断后已自动重连并重试一次")
                    break
                except GenerationCancelled:
                    raise
                except Exception as exc:
                    check_cancelled()
                    if not transport_disconnected(exc):
                        raise
                    self.powerbi.reconnector.lost(model["id"])
                    if read_attempt:
                        raise
            columns = list(data[0]) if data else []
            if len(columns) > 30:
                raise ValueError("结果超过 30 列，请缩小分析范围")
            result["columns"] = columns
            used = 0
            nonfinite = False
            for row in data[:ROW_LIMIT]:
                clean = {
                    key: (None if isinstance(value, float) and not math.isfinite(value) else value)
                    for key, value in row.items()
                }
                nonfinite |= any(isinstance(v, float) and not math.isfinite(v) for v in row.values())
                size = len(encode(clean).encode("utf-8"))
                if used + size > RESULT_BYTES:
                    result["truncated"] = True
                    break
                result["rows"].append(clean)
                used += size
            if data and not result["rows"]:
                raise ValueError("单行结果过大，请减少返回字段")
            result["truncated"] |= len(data) > ROW_LIMIT
            result["row_count"] = len(result["rows"])
            if nonfinite:
                result["warnings"].append("非有限数值已显示为空，请检查计算口径")
            if result["truncated"]:
                result["warnings"].append("结果已截断，保留的行不能代表全部数据")
            result["ok"] = True
        except Exception as exc:
            result["error"] = str(exc.detail if isinstance(exc, HTTPException) else exc).splitlines()[0][:350]
            result["connection_error"] = isinstance(exc, HTTPException) or transport_disconnected(exc)
        result["seconds"] = round(time.monotonic() - started, 3)
        return result

    def stream(self, conv, model, ctx, body, provider, lock, template=None):
        run = AnalysisRun(self, conv, model, ctx, body, provider, template)
        yield from run.stream(lock)


class AnalysisRun:
    """State shared by the planning, execution and summary stages of one reply."""

    def __init__(self, service, conversation, model, ctx, body, provider, template):
        self.service = service
        self.db = service.db
        self.chat = service.chat
        self.conversation = conversation
        self.model = model
        self.ctx = ctx
        self.body = body
        self.provider = provider
        self.template = template
        self.commands = template.commands if template else []
        self.message_id = uid()
        self.total_timeout = generation_timeout(provider)
        self.budget = AnalysisBudget(self.total_timeout)
        self.call_counts = {}
        self.messages = []
        self.evidence = []
        self.display = ""
        self.query_count = 0
        self.attempt_count = 0
        self.batch_count = 0

    def remaining(self, limit, *, summary=False):
        check_cancelled()
        seconds = self.budget.remaining(summary=summary)
        if seconds <= 0:
            message = (
                f"已达到本轮分析总时限（{int(self.total_timeout)} 秒），已保留文字及查询结果；可在模型服务设置中延长总时限后重试"
                if summary
                else "本轮查询阶段可用时间已用完，已为结论生成预留时间"
            )
            raise HTTPException(504, message)
        self.chat.progress.get(self.message_id, {})["elapsed"] = int(
            self.budget.clock() - self.budget.started
        )
        return min(limit, seconds)

    def status(self, text):
        self.chat.progress[self.message_id] = {
            "stage": text,
            "elapsed": int(self.budget.clock() - self.budget.started),
        }
        return encode({"type": "status", "text": text}) + "\n"

    def save_partial(self):
        self.db.execute("UPDATE messages SET content=? WHERE id=?", (self.display, self.message_id))

    def generate(self, stage, system, turns, *, report=None, **options):
        self.call_counts[stage] = self.call_counts.get(stage, 0) + 1
        began = self.budget.clock()
        try:
            yield from completion_events(
                self.provider,
                system,
                turns,
                timeout=self.remaining(self.total_timeout, summary=stage == "analysis_summary"),
                report=report,
                on_report=generation_recorder(
                    self.db,
                    self.provider,
                    stage=stage,
                    attempt=self.call_counts[stage],
                    message_id=self.message_id,
                    model_id=self.model["id"],
                ),
                **options,
            )
        finally:
            if stage != "analysis_summary":
                self.budget.observe("plan", self.budget.clock() - began)

    def read(self, work, *, timeout):
        result = None
        for event in task_events(work, timeout=timeout):
            if event["type"] == "heartbeat":
                yield encode(event) + "\n"
            else:
                result = event["result"]
        return result

    def open_turn(self):
        window = self.chat.history.window(self.conversation["id"], send=True)
        history = window.retrieval_history()
        user_id = self.chat.images.save_user(self.conversation["id"], self.body.content, self.body.image_ids)
        self.db.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?)",
            (self.message_id, self.conversation["id"], "assistant", "", "streaming", now()),
        )
        self.db.execute(
            "UPDATE conversations SET title=CASE WHEN title='新对话' THEN ? ELSE title END,updated_at=? WHERE id=?",
            (self.body.content[:36], now(), self.conversation["id"]),
        )
        yield encode({"type": "start", "message_id": self.message_id, "context_usage": window.usage}) + "\n"
        if window.usage["compressed_now"]:
            yield self.status("已自动压缩历史上下文，正在结合摘要和最近对话分析")
        yield encode({"type": "model", "model_id": self.model["id"], "live": True}) + "\n"
        self.messages = self.chat.images.llm_messages(window.messages, user_id, self.body.content)
        return window, history

    def stream(self, lock):
        saved = False
        cancelled = False
        error = wording("本次分析已中断，请重新发送", "Analysis was interrupted. Send the question again.")
        self.chat.active_messages[self.conversation["id"]] = self.message_id
        try:
            window, history = yield from self.open_turn()
            planner = AnalysisPlanner(self)
            plan, saved_scope, inherited = yield from planner.initial_plan(window, history)
            if plan.clarification or not plan.queries:
                card = planner.fields.save_analysis_card(plan.clarification, inherited)
                self.display = card_markdown(card)
                yield encode({"type": "clarification", "clarification": card}) + "\n"
                yield encode({"type": "delta", "text": self.display}) + "\n"
            else:
                if plan.scope:
                    self.display = (
                        wording("### 分析口径", "### Analysis scope") + "\n\n" + plan.scope + "\n\n"
                    )
                    yield encode({"type": "delta", "text": self.display}) + "\n"
                    self.save_partial()
                queries = AnalysisQueries(self, planner, plan.scope)
                yield from queries.execute(plan, saved_scope)
                yield from summarize(self, planner, queries)
            check_cancelled()
            fixed_failure = bool(self.commands) and not any(q["ok"] for q in self.evidence)
            self.db.execute(
                "UPDATE messages SET content=?,status=? WHERE id=?",
                (self.display, "error" if fixed_failure else "complete", self.message_id),
            )
            saved = True
            yield encode({"type": "analysis", "queries": self.evidence}) + "\n"
            if fixed_failure:
                yield (
                    encode(
                        {
                            "type": "error",
                            "text": wording(
                                "固定查询未成功，请查看本次查询记录。",
                                "The saved queries failed. See this run’s query records.",
                            ),
                        }
                    )
                    + "\n"
                )
            else:
                yield encode({"type": "done"}) + "\n"
        except GeneratorExit:
            raise
        except GenerationCancelled:
            cancelled = True
            error = cancelled_text()
            yield encode({"type": "cancelled", "text": error}) + "\n"
        except Exception as exc:
            error = (
                exc.detail
                if isinstance(exc, HTTPException)
                else wording(
                    f"分析未完成（{type(exc).__name__}），已保留已取得的查询结果",
                    f"Analysis could not finish ({type(exc).__name__}). Available query results are saved.",
                )
            )
            error = multimodal_error(exc, any(m.images for m in self.messages)) or error
            logger.warning("Analysis %s failed: %s", self.message_id, type(exc).__name__)
            yield encode({"type": "error", "text": error}) + "\n"
        finally:
            if not saved:
                self.db.execute(
                    "UPDATE messages SET content=?,status=? WHERE id=?",
                    (
                        self.display + "\n\n[" + error + "]",
                        "cancelled" if cancelled else "error",
                        self.message_id,
                    ),
                )
            self.chat.progress.pop(self.message_id, None)
            self.chat.active_messages.pop(self.conversation["id"], None)
            lock.release()
            client = getattr(self.provider, "_client", None)
            if cancelled:
                close_cancelled_provider(self.provider)
            if client and not cancelled:
                try:
                    client.close()
                except Exception:
                    pass
