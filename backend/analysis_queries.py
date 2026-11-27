"""Execute planned DAX batches, record evidence and review recoverable failures."""

from fastapi import HTTPException

from . import analysis_budget as limits
from .analysis_budget import MAX_BATCH_QUERIES, evidence_context, query_key
from .analysis_protocol import AnalysisPlan, PlanResponseError, QueryPlan
from .analysis_recovery import RECOVERY_RULE, recovery_candidates, recovery_problem
from .cancellation import GenerationCancelled, check_cancelled
from .database import encode, now, uid
from .engine.llm.base import ChatMessage
from .language import wording
from .reconnection import transport_disconnected
from .scope_checks import filter_conflict


def saved_batch(commands, scope, offset):
    return AnalysisPlan(
        scope=scope,
        queries=[QueryPlan(title=c.title, dax=c.dax) for c in commands[offset : offset + MAX_BATCH_QUERIES]],
    )


class AnalysisQueries:
    def __init__(self, run, planner, initial_scope):
        self.run = run
        self.planner = planner
        self.initial_scope = initial_scope
        self.executed = {
            query_key(q["dax"])
            for q in run.evidence
            if q["ok"] and q.get("run_verified") and not q.get("parameters")
        }
        self.new_result = False
        self.planned_labels = {}
        self.stop_reason = ""
        self.unanswered = []
        self.pending_card = None
        self.format_failures = []
        self.recovery_used = False

    def persist_query(self, plan, query_id, label, title, query, result, attempt):
        run = self.run
        record = {
            **result,
            "id": query_id,
            "label": label,
            "title": title,
            "dax": query.dax,
            "attempt": attempt,
            "batch": run.batch_count + run.batch_offset,
            "scope": plan.scope or self.initial_scope,
            "created_at": now(),
        }
        if run.commands:
            record["parameters"] = run.commands[run.query_count - 1].parameters
            record["template_dax"] = run.command_definitions[run.query_count - 1]["dax"]
            record["template_title"] = run.command_definitions[run.query_count - 1]["title"]
        if query.convention_exceptions:
            record["convention_exceptions"] = {
                ref: {"title": self.planner.conventions[ref]["title"], "reason": reason}
                for ref, reason in query.convention_exceptions.items()
            }
        if plan.recovery:
            record["recovery"] = plan.recovery.model_dump()
        if result["ok"] and query.resolves:
            record["resolved_tasks"] = [run.resume_items[ref] for ref in query.resolves]
        run.db.execute(
            "INSERT INTO analysis_queries VALUES (?,?,?,?,?,?,?)",
            (
                query_id,
                run.message_id,
                run.model["id"],
                title,
                query.dax,
                encode(record),
                record["created_at"],
            ),
        )
        run.evidence.append(record)
        if result["ok"] and result.get("run_verified"):
            self.new_result = True

    def work_error(self, exc):
        run = self.run
        if isinstance(exc, HTTPException) and exc.status_code == 504 and run.budget.remaining() < 0.1:
            return "查询阶段可用时间已用完，已停止补查并为结论预留时间"
        if isinstance(exc, HTTPException):
            return str(exc.detail)
        if isinstance(exc, TimeoutError):
            return "模型服务请求超时，已停止本次步骤并保留已有结果"
        if isinstance(exc, OSError) or transport_disconnected(exc):
            return "模型服务连接异常，已停止本次步骤并保留已有结果"
        return "后续分析步骤未完成，已保留已有结果，请查看模型服务状态后重试"

    def execute_query(self, plan, label, planned, scheduled, repair_hints):
        run = self.run
        run.query_count += 1
        current_query = planned
        result = None
        for attempt in range(2):
            dax = current_query.dax
            if run.attempt_count >= limits.MAX_QUERY_ATTEMPTS:
                self.stop_reason = wording(
                    "已达到本轮查询执行次数上限（含修复重试）",
                    "Reached the query execution limit for this turn, including retries",
                )
                break
            if not run.budget.can_query():
                self.stop_reason = wording(
                    "剩余时间不足以继续查询，已为结论生成预留时间",
                    "The remaining time is reserved for the summary",
                )
                break
            key = query_key(dax)
            if not run.commands and key in self.executed:
                break
            self.executed.add(key)
            run.attempt_count += 1
            detail = (
                f"（替代 {plan.recovery.for_query}）"
                if plan.recovery
                else "（修复后重试）"
                if attempt
                else ""
            )
            yield run.status(f"正在 Desktop 查询 {label} · {planned.title}" + detail)
            began = run.budget.clock()
            result = None
            try:
                query_parameters = run.commands[run.query_count - 1].parameters if run.commands else {}

                def read(query=dax, params=query_parameters):
                    return (
                        run.service.execute(run.model, query, params)
                        if params
                        else run.service.execute(run.model, query)
                    )

                result = yield from run.read(read, timeout=run.remaining(100))
            except GenerationCancelled:
                raise
            except Exception as exc:
                self.stop_reason = self.work_error(exc)
                result = {
                    "ok": False,
                    "run_verified": False,
                    "columns": [],
                    "rows": [],
                    "row_count": 0,
                    "truncated": False,
                    "warnings": [],
                    "error": self.stop_reason,
                    "seconds": round(run.budget.clock() - began, 3),
                }
            run.budget.observe("query", run.budget.clock() - began)
            self.persist_query(plan, uid(), label, planned.title, current_query, result, attempt + 1)
            yield encode({"type": "analysis", "queries": run.evidence}) + "\n"
            if result.get("connection_error"):
                self.stop_reason = wording(
                    "模型连接或结构发生变化，已停止后续查询",
                    "Queries stopped because the model connection or schema changed",
                )
            if run.commands or self.stop_reason or result["ok"] or attempt or plan.recovery:
                break
            if run.attempt_count >= limits.MAX_QUERY_ATTEMPTS or not run.budget.can_plan():
                self.stop_reason = wording(
                    "剩余额度不足以修复查询，已保留结果并转入总结",
                    "There is no time left to repair the query. The summary uses the saved results",
                )
                break
            yield run.status(f"{label} 查询未通过，正在修复查询")
            repair_instruction = "只修复下面这一个查询，保持原分析口径，按原 JSON 格式返回一个 queries 项。"
            if (
                not self.recovery_used
                and run.batch_count < limits.MAX_BATCHES
                and run.query_count < limits.MAX_QUERIES
                and run.attempt_count + 1 < limits.MAX_QUERY_ATTEMPTS
            ):
                repair_instruction += "若还知道另一种有依据的取数方法，可在 assessment 中写明具体做法并引用真实 [度量] 或 '表'[列]，否则留空。"
            repair_context = {
                "scope": self.initial_scope,
                "query": dax,
                "error": result["error"],
                **self.planner.fields.field_feedback(),
            }
            if current_query.convention_exceptions:
                repair_context["convention_exceptions"] = current_query.convention_exceptions
            repair = self.planner.analysis_messages + [
                ChatMessage("user", repair_instruction + "\n" + encode(repair_context))
            ]
            try:
                fixed = yield from self.planner.request_plan(repair, "analysis_repair", fields_closed=True)
                if fixed.clarification:
                    self.pending_card = self.planner.fields.save_analysis_card(fixed.clarification)
                    yield encode({"type": "clarification", "clarification": self.pending_card}) + "\n"
                    self.stop_reason = wording(
                        "修复查询需要确认业务口径，已保留当前结果",
                        "Repair requires a confirmed business definition. Current results are saved",
                    )
                    break
                conflict = filter_conflict(
                    self.planner.fields.records, self.planner.fields.grouped_scopes(), fixed
                )
                if conflict:
                    raise PlanResponseError(conflict[1])
                current_query = fixed.queries[0]
                if query_key(current_query.dax) in self.executed or query_key(current_query.dax) in scheduled:
                    break
                repair_hints[label] = fixed.assessment
            except GenerationCancelled:
                raise
            except PlanResponseError as exc:
                # A malformed repair must not discard other independent queries.
                note = f"{label}：{exc.detail}"
                self.format_failures.append(note)
                self.planner.pending.append(planned.title)
                record = run.evidence[-1]
                record.setdefault("warnings", []).append(note)
                run.db.execute(
                    "UPDATE analysis_queries SET result=? WHERE id=?", (encode(record), record["id"])
                )
                yield run.status(note)
                break
            except Exception as exc:
                self.stop_reason = self.work_error(exc)
                break
        return result

    def execute_batch(self, plan, batch, scheduled, repair_hints):
        run = self.run
        batch_results = []
        for index, (label, planned) in enumerate(batch):
            if run.attempt_count >= limits.MAX_QUERY_ATTEMPTS:
                self.stop_reason = wording(
                    "已达到本轮查询执行次数上限（含修复重试）",
                    "Reached the query execution limit for this turn, including retries",
                )
                break
            if not run.budget.can_query():
                self.stop_reason = wording(
                    "剩余时间不足以继续查询，已为结论生成预留时间",
                    "The remaining time is reserved for the summary",
                )
                break
            result = yield from self.execute_query(plan, label, planned, scheduled, repair_hints)
            batch_results.append(result)
            if self.stop_reason:
                self.unanswered.extend(p.title for _, p in batch[index + 1 :])
                break
        return batch_results

    def execute(self, plan, saved_scope):
        run = self.run
        while plan.queries:
            check_cancelled()
            self.unanswered = plan.remaining_questions[:]
            run.open_questions = self.unanswered or run.open_questions
            batch = []
            scheduled = set()
            for planned in plan.queries:
                key = query_key(planned.dax)
                if not run.commands and (key in self.executed or key in scheduled):
                    continue
                scheduled.add(key)
                if run.query_count + len(batch) >= limits.MAX_QUERIES:
                    self.unanswered.append(planned.title)
                    continue
                label = f"Q{run.query_offset + run.query_count + len(batch) + 1}"
                self.planned_labels[label] = planned.title
                batch.append((label, planned))
            if not batch:
                self.stop_reason = wording(
                    "后续计划没有新的可执行查询，已停止重复查询",
                    "The follow-up plan contained no new executable queries",
                )
                break
            run.batch_count += 1
            yield run.status(f"正在执行第 {run.batch_count} 批分析查询 · 本批 {len(batch)} 项")
            batch_start = len(run.evidence)
            repair_hints = {}
            batch_results = yield from self.execute_batch(plan, batch, scheduled, repair_hints)
            if self.stop_reason:
                self.unanswered.extend(
                    title
                    for label, title in self.planned_labels.items()
                    if not any(r["label"] == label for r in run.evidence)
                )
                if run.commands:
                    self.unanswered.extend(c.title for c in run.commands[len(self.planned_labels) :])
                break
            if run.commands:
                plan = saved_batch(run.commands, saved_scope, run.query_count)
                continue
            batch_ok = any(r and r["ok"] for r in batch_results)
            needs_review = (
                plan.needs_more
                or bool(self.unanswered)
                or any(r and (not r["ok"] or r["truncated"]) for r in batch_results)
            )
            if not needs_review:
                break
            candidates = (
                {}
                if self.recovery_used
                else recovery_candidates(
                    run.evidence[batch_start:],
                    repair_hints,
                    run.ctx,
                    run.body.content,
                    [r for r in run.evidence if r["ok"] and r["row_count"]],
                )
            )
            if not batch_ok and not candidates:
                self.stop_reason = (
                    wording("替代查询仍未取得成功结果，已结束补救", "The alternative query also failed")
                    if plan.recovery
                    else wording("本批查询未取得成功结果，已停止继续补查", "No query in this batch succeeded")
                )
                break
            if run.query_count >= limits.MAX_QUERIES:
                self.stop_reason = wording(
                    f"已达到本轮 {limits.MAX_QUERIES} 条业务查询上限",
                    f"Reached the limit of {limits.MAX_QUERIES} business queries for this turn",
                )
                break
            if run.batch_count >= limits.MAX_BATCHES:
                self.stop_reason = wording(
                    f"已达到本轮 {limits.MAX_BATCHES} 批查询上限",
                    f"Reached the limit of {limits.MAX_BATCHES} query batches for this turn",
                )
                break
            if run.attempt_count >= limits.MAX_QUERY_ATTEMPTS:
                self.stop_reason = wording(
                    "已达到本轮查询执行次数上限（含修复重试）",
                    "Reached the query execution limit for this turn, including retries",
                )
                break
            if not run.budget.can_plan():
                self.stop_reason = wording(
                    "剩余时间不足以规划并执行补充查询，已为结论生成预留时间",
                    "The remaining time is reserved for summarizing the completed queries",
                )
                break
            review = {
                "locked_scope": self.initial_scope,
                "previous_plan": plan.model_dump(
                    exclude={"queries", "field_checks", "semantic_queries", "scope_checks", "recovery"}
                ),
                "evidence": evidence_context(run.evidence, max_chars=48000, preview_rows=16),
                **self.planner.fields.field_feedback(),
                "remaining_questions": self.unanswered,
            }
            instruction = (
                "以下为本轮已经执行的真实结果。锁定既有口径，不能沿用历史消息中的旧数值。"
                "已有证据足够就停止，有独立问题待查时只规划必要查询。"
            )
            if candidates:
                review["recovery_candidates"] = candidates
                instruction += RECOVERY_RULE
                instruction += (
                    "独立待查问题仍可按常规续查。"
                    if batch_ok
                    else "本步只评估替代取数。如无可行方案就返回空 queries。"
                )
            elif any(r and not r["ok"] for r in batch_results):
                instruction += "已失败的项目没有可用的替代取数机会，保留成功结果，只评估其余独立问题。"
            continuation = self.planner.analysis_messages + [
                ChatMessage("user", instruction + "\n" + encode(review))
            ]
            yield run.status(
                "正在评估已有结果，决定是否需要补充查询"
                if batch_ok
                else "常规修复未成功，正在评估一次替代取数机会"
            )
            try:
                if batch_ok:
                    plan = yield from self.planner.prepare_plan(continuation, "analysis_continue")
                else:
                    self.recovery_used = True
                    plan = yield from self.planner.request_plan(
                        continuation, "analysis_recovery", fields_closed=True
                    )
            except GenerationCancelled:
                raise
            except PlanResponseError as exc:
                self.format_failures.append(exc.detail)
                break
            except Exception as exc:
                self.stop_reason = self.work_error(exc)
                break
            self.unanswered = plan.remaining_questions[:]
            if plan.clarification:
                self.pending_card = self.planner.fields.save_analysis_card(plan.clarification)
                yield encode({"type": "clarification", "clarification": self.pending_card}) + "\n"
                self.stop_reason = wording(
                    "进一步分析需要确认业务口径，已保留当前查询结果",
                    "Further analysis requires a confirmed business definition. Current query results are saved",
                )
                break
            if plan.recovery:
                self.recovery_used = True
                problem = recovery_problem(
                    plan,
                    candidates,
                    run.evidence,
                    run.ctx,
                    [*self.planner.fields.prior, *self.planner.fields.records],
                    run.body.content,
                )
                conflict = filter_conflict(
                    self.planner.fields.records, self.planner.fields.grouped_scopes(), plan
                )
                if problem or conflict:
                    self.stop_reason = wording("已停止替代取数：", "Alternative query stopped: ") + (
                        problem or conflict[1]
                    )
                    break
                source = next(r for r in run.evidence if r["label"] == plan.recovery.for_query)
                plan = plan.model_copy(update={"scope": source["scope"]})
            elif not batch_ok:
                self.stop_reason = wording(
                    "本批查询未取得成功结果，已结束替代取数评估",
                    "No query in this batch succeeded. The alternative query review has ended",
                )
                break
            if not plan.queries and (plan.needs_more or self.unanswered):
                self.stop_reason = wording(
                    "后续分析仍有未解决问题，当前计划未提供可执行查询",
                    "Some questions remain open and the follow-up plan supplied no executable queries",
                )
        self.unanswered = list(dict.fromkeys(self.unanswered))
        self.stop_reason = wording("；", "; ").join(filter(None, [self.stop_reason, *self.format_failures]))
        if self.stop_reason:
            yield run.status(self.stop_reason)
