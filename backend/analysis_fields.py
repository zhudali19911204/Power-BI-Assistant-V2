"""Field evidence, exact scope checks and clarification cards for one analysis turn."""

from fastapi import HTTPException

from .analysis_protocol import AnalysisPlan
from .cancellation import GenerationCancelled, check_cancelled
from .clarifications import ClarificationQuestion, make_card, save_card
from .database import encode, now, uid
from .field_discovery import (
    cached_check,
    check_key,
    clarification_field_sources,
    field_stop_reason,
    prior_literal_check,
)
from .fields import FieldPreviewInput
from .language import wording
from .scope_checks import (
    annotate_card,
    candidate_probes,
    next_scope_batch,
    question_scope,
    read_scope_batch,
    scope_evidence,
    scope_question,
    scope_read_batches,
    select_scope,
)


class AnalysisFields:
    def __init__(self, run):
        self.run = run
        self.records = []
        self.prior = []
        self.reused = {}
        self.terms = {}
        self.settled = set()
        self.unchecked = {}
        self.member_lists = {}
        self.redirected_searches = set()
        self.dictionary = None
        self.bindings = []
        self.rounds = 0

    def grouped_scopes(self):
        groups = scope_evidence(self.records)
        for key, term in self.terms.items():
            group = groups.setdefault(key, {"term": term, "matches": [], "misses": [], "incomplete": []})
            group["incomplete"].extend(
                {"table": t, "column": c, "reason": why}
                for (t, c), why in self.unchecked.get(key, {}).items()
            )
        return groups

    def save_analysis_card(self, question, inherited=()):
        run = self.run
        groups = scope_evidence(inherited) if inherited else self.grouped_scopes()
        if isinstance(question, ClarificationQuestion) and question.topic in ("字段归属", "Field mapping"):
            focused = question_scope(question.question, groups)
            if focused:
                question = scope_question(
                    focused,
                    wording(
                        "请按本次问题选择字段归属。已核对的匹配值列在选项中。",
                        "Choose the field for this question. The options show verified matches.",
                    ),
                )
        card = annotate_card(make_card(question, run.ctx), groups)
        sources = clarification_field_sources(run.model, [*self.prior, *self.records])
        binding = card.get("scope_binding")
        if sources and binding:
            records = inherited or self.records
            topic = binding["literal"].casefold()
            pending = {
                key
                for key, g in groups.items()
                if key != topic and not g["incomplete"] and len(g["matches"]) != 1
            }
            sources["pending_ids"] = [
                r["id"]
                for r in records
                if (r.get("scope_term") or r.get("related_term", "")).casefold() in pending
            ]
            if inherited:
                sources["choice_ids"] = {
                    option: r["id"]
                    for option, c in binding["choices"].items()
                    for r in inherited
                    if (r.get("scope_term") or r.get("related_term", "")).casefold() == topic
                    and (r["label"], r["table"], r["column"]) == (c["field"], c["table"], c["column"])
                }
        save_card(run.db, run.message_id, {**card, "_field_sources": sources} if sources else card)
        return card

    def scope_fallback(self, current, reason):
        groups = self.grouped_scopes()
        requested = [c.term.casefold() for c in current.scope_checks]
        keys = list(dict.fromkeys([*requested, *groups]))
        pending = [groups[k] for k in keys if k in groups and k not in self.settled]
        group = next((g for g in pending if len(g["matches"]) != 1 or g["incomplete"]), None)
        if not pending:
            pending = [groups[k] for k in requested if k in groups]
        if not pending:
            return None
        return AnalysisPlan(scope=current.scope, clarification=scope_question(group or pending[0], reason))

    def check_scopes(self, current):
        run = self.run
        if self.dictionary is None and run.chat.semantics:
            self.dictionary = run.chat.semantics.get(run.model["id"])
        hints = (
            run.chat.semantics.values.preview_hints_for(run.model, [s.term for s in current.scope_checks])
            if run.chat.semantics
            else {}
        )
        probes = candidate_probes(current.scope_checks, run.ctx, self.bindings, hints, self.dictionary)
        planned, skipped = next_scope_batch(probes, self.records)
        for p, reason in skipped:
            self.unchecked.setdefault(p.term.casefold(), {})[p.table, p.column] = reason
        if not planned:
            return wording(
                "已有检查未能确定这一项，继续重复或扩大查值缺少新的依据。",
                "The field checks were inconclusive. More information is needed to choose another field.",
            )
        for p in planned:
            self.unchecked.setdefault(p.term.casefold(), {})[p.table, p.column] = "尚未读取"
        self.rounds += 1
        for batch in scope_read_batches(planned):
            check_cancelled()
            if not run.budget.can_check_field():
                return wording(
                    "剩余时间不足以完成所有候选字段，已保留已核对的结果。",
                    "Time ran out before all candidates could be checked. The completed checks are saved.",
                )
            for p in batch:
                self.unchecked[p.term.casefold()].pop((p.table, p.column), None)
            reason = yield from self.read_candidates(batch)
            if reason:
                return reason
        return ""

    def read_candidates(self, planned):
        run = self.run
        yield run.status(f"正在批量核对 {len({p.term for p in planned})} 个口径 · {len(planned)} 项候选字段")
        began = run.budget.clock()
        failure = None
        query = ""
        try:
            results, query = yield from run.read(
                lambda: read_scope_batch(run.service.powerbi, run.model["id"], planned),
                timeout=run.remaining(30),
            )
        except GenerationCancelled:
            raise
        except Exception as exc:
            failure = exc
            error = str(exc.detail if isinstance(exc, HTTPException) else exc).splitlines()[0][:240]
            results = [{"ok": False, "profile": None, "error": error} for p in planned]
        elapsed = run.budget.clock() - began
        run.budget.observe("field", elapsed)
        batch = []
        owner = ""
        for p, result in zip(planned, results):
            label = f"F{len(self.records) + len(batch) + 1}"
            r = {
                **result,
                "id": uid(),
                "label": label,
                "model_id": run.model["id"],
                "table": p.table,
                "column": p.column,
                "scope_term": p.term,
                "search_terms": list(p.literals),
                "round": self.rounds,
                "checked_at": now(),
            }
            if p.basis:
                r["reason"] = p.basis
            if p.term.casefold() in self.member_lists:
                r["scope_members"] = self.member_lists[p.term.casefold()]
            if r["ok"] and not owner:
                owner = label
                r["profile"]["dax_queries"] = [query]
                r["batch_seconds"] = round(elapsed, 3)
            batch.append(r)
            self.settled.discard(p.term.casefold())
        for r in batch:
            r["query_label"] = owner
        with run.db.connection() as db:
            db.executemany(
                "INSERT INTO field_observations VALUES (?,?,?,?)",
                [(r["id"], run.message_id, encode(r), r["checked_at"]) for r in batch],
            )
            if run.chat.semantics:
                run.chat.semantics.values.remember_exact(db, run.model, batch)
        self.records.extend(batch)
        yield encode({"type": "field_previews", "records": self.records}) + "\n"
        if isinstance(failure, HTTPException) and failure.status_code == 409:
            raise failure
        if isinstance(failure, HTTPException) and failure.status_code == 504:
            return "批量核对超时，已保留此前取得的口径依据。"
        return ""

    def interpret_scopes(self, current):
        run = self.run
        groups = self.grouped_scopes()
        accepted = []
        for key, group in groups.items():
            if key in self.settled:
                continue
            match, question = select_scope(
                group, current, run.ctx, run.body.content, self.bindings, self.dictionary or {}
            )
            if question:
                return AnalysisPlan(scope=current.scope, clarification=question)
            if match:
                accepted.append(match)
        if accepted:
            selected = {m["field"]: m["value"] for m in accepted}
            with run.db.connection() as db:
                for r in self.records:
                    if r["label"] in selected:
                        r["adopted_value"] = selected[r["label"]]
                        db.execute("UPDATE field_observations SET result=? WHERE id=?", (encode(r), r["id"]))
            yield encode({"type": "field_previews", "records": self.records}) + "\n"
        self.settled.update(groups)
        return current

    def profile_records(self):
        return [r for r in self.records if not r.get("scope_term")]

    def field_feedback(self):
        profiles = [
            {
                **r,
                "profile": {k: v for k, v in r["profile"].items() if k != "dax_queries"}
                if r["profile"]
                else None,
            }
            for r in self.records
            if not r.get("scope_term")
        ]
        feedback = {"scope_evidence": list(self.grouped_scopes().values()), "field_observations": profiles}
        if self.reused:
            feedback["prior_field_profiles"] = [
                {
                    "table": r["table"],
                    "column": r["column"],
                    "profile": {k: v for k, v in r["profile"].items() if k != "dax_queries"},
                }
                for r in self.reused.values()
            ]
        return feedback

    def pending_checks(self, current, user_text):
        run = self.run
        explicit_keys = {check_key(check) for check in current.field_checks}
        checks = current.field_checks + run.service.implicit_checks(current, run.ctx, user_text)
        pending = []
        newly_reused = False
        for check in checks:
            key = check_key(check)
            if key not in explicit_keys and any((r["table"], r["column"]) == key[:2] for r in self.records):
                continue
            if cached_check(check, self.records) is not None or any(k == key for k, _ in pending):
                continue
            # A caller asking for samples or statistics needs a covering profile.
            # An inferred equality only needs the exact literal in this column.
            prior = None
            if check.related_term.casefold() not in self.terms:
                if key not in explicit_keys and check.search_terms:
                    prior = prior_literal_check(check, self.prior)
                else:
                    prior = cached_check(check, self.prior)
            if prior is None:
                pending.append((key, check))
            elif prior["id"] not in self.reused:
                self.reused[prior["id"]] = prior
                newly_reused = True
                yield run.status(f"沿用口径确认前 {prior['label']} 的取值 · {check.table}[{check.column}]")
        return checks, pending, newly_reused

    def read_profiles(self, pending):
        run = self.run
        reason = ""
        self.rounds += 1
        for key, check in pending:
            if cached_check(check, self.records) is not None:
                continue
            reason = field_stop_reason(check, self.profile_records(), run.budget)
            if reason:
                break
            field_timeout = run.remaining(30)
            label = f"F{len(self.records) + 1}"
            began = run.budget.clock()
            yield run.status(f"正在检查字段实际取值 {label} · {check.table}[{check.column}]")
            record = {
                "id": uid(),
                "label": label,
                "model_id": run.model["id"],
                "table": check.table,
                "column": check.column,
                "reason": check.reason,
                "search_terms": check.search_terms,
                "round": self.rounds,
                "ok": False,
                "profile": None,
                "error": "",
                "checked_at": now(),
            }
            if check.related_term:
                record["related_term"] = check.related_term
                self.settled.discard(check.related_term.casefold())
            try:
                body_preview = FieldPreviewInput(table=check.table, column=check.column, limit=20)

                def read_profile(preview=body_preview, terms=check.search_terms):
                    return run.service.fields.preview(
                        run.model["id"], preview, search_terms=terms, automatic=True
                    )

                record.update(ok=True, profile=(yield from run.read(read_profile, timeout=field_timeout)))
            except GenerationCancelled:
                raise
            except Exception as exc:
                record["error"] = str(exc.detail if isinstance(exc, HTTPException) else exc).splitlines()[0][
                    :240
                ]
                if isinstance(exc, HTTPException) and exc.status_code == 409:
                    record["connection_error"] = True
                if isinstance(exc, HTTPException) and exc.status_code == 504:
                    reason = wording(
                        "字段取值检查超时，已停止补查并保留已有依据",
                        "The field check timed out. Earlier findings are saved",
                    )
            run.budget.observe("field", run.budget.clock() - began)
            run.db.execute(
                "INSERT INTO field_observations VALUES (?,?,?,?)",
                (record["id"], run.message_id, encode(record), record["checked_at"]),
            )
            self.records.append(record)
            yield encode({"type": "field_previews", "records": self.records}) + "\n"
            if record.get("connection_error"):
                raise HTTPException(409, record["error"])
            if reason:
                break
        return reason
