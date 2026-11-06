"""
Live DAX evaluation — the run-verify half of the validation loop.

Static checks can only prove a measure *might* be right; only execution proves it. `LiveDesktopEvaluator`
defines the candidate measure on the open Desktop engine and runs it with `EVALUATE ROW(...)`, returning
either the real scalar value (success) or the engine's own error message (a *verified* failure). Either
way `run_verified=True`, so the product never presents unexecuted DAX as if it had run.

`evaluate_at_slice` runs the same measure under a specific filter context (a dimension slice) — the basis
of calibrated generation: compare the value at a slice the user knows the correct answer for.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from ..context.live_source import _LiveSession, find_adomd_dll, find_instances
from ..core.artifact import ValidationResult

_PROBE = "__pbi_ai_probe"

# Functions that return a TABLE — if an expression starts with one, it's a calculated table, not a
# scalar measure, and must be validated/written differently.
_TABLE_FUNCS = (
    "CALENDAR", "CALENDARAUTO", "ADDCOLUMNS", "SUMMARIZE", "SUMMARIZECOLUMNS", "SELECTCOLUMNS",
    "FILTER", "ALL", "ALLNOBLANKROW", "ALLSELECTED", "ALLEXCEPT", "VALUES", "DISTINCT", "DATATABLE",
    "GENERATESERIES", "GENERATE", "GENERATEALL", "UNION", "EXCEPT", "INTERSECT", "CROSSJOIN", "NATURALINNERJOIN",
    "NATURALLEFTOUTERJOIN", "TOPN", "GROUPBY", "TREATAS", "ROW", "CALCULATETABLE", "RELATEDTABLE",
)
_TABLE_RE = re.compile(r"^\s*(?:VAR\b.*\bRETURN\s+)?(" + "|".join(_TABLE_FUNCS) + r")\s*\(", re.IGNORECASE | re.DOTALL)


_COMMENTS_RE = re.compile(r"/\*.*?\*/|(?://|--)[^\n]*", re.DOTALL)


def is_table_expression(expression: str) -> bool:
    """Heuristic: does this DAX expression return a TABLE (calculated table) rather than a scalar?
    Comments are stripped first so a leading `// ...` line doesn't hide the table-returning function."""
    return bool(_TABLE_RE.match(_COMMENTS_RE.sub("", expression).strip()))

# A single slice predicate: (table, column, value). Value is a real Python value from the engine.
SliceFilter = tuple[str, str, Any]


def _clean_error(exc: Exception) -> str:
    """The first, most meaningful line of an ADOMD/.NET exception message."""
    text = str(exc).strip()
    return text.splitlines()[0][:300] if text else type(exc).__name__


def _is_syntax_error(vr: ValidationResult) -> bool:
    """A hard parse error (bad DAX) vs a merely-unresolved reference (which siblings can fix)."""
    return any("invalid token" in e.lower() or "syntax error" in e.lower() for e in vr.errors)


def validate_measure_set(
    evaluator: "LiveDesktopEvaluator", home_table: str, measures: list[tuple[str, str]]
) -> list[ValidationResult]:
    """Validate each measure of a set, isolating syntactically-broken ones so they don't fail the others.

    A measure that only fails because it references a sibling is retried with all *non-broken* siblings
    DEFINE-d; a measure with its own syntax error keeps that error and is excluded from others' DEFINE set.
    """
    alone = [evaluator.evaluate_with_defines(home_table, [m], m[0]) for m in measures]
    broken = {measures[i][0] for i, vr in enumerate(alone) if not vr.ok and _is_syntax_error(vr)}
    good = [m for m in measures if m[0] not in broken]

    results: list[ValidationResult] = []
    for i, (name, _expr) in enumerate(measures):
        if alone[i].ok or name in broken:
            results.append(alone[i])  # passed standalone, or genuinely broken — keep its own result
        else:
            results.append(evaluator.evaluate_with_defines(home_table, good, name))  # needed siblings
    return results


def _format_value(value: Any) -> str:
    """Render a Python value as a DAX literal for a filter predicate. Type-based so it matches the
    column: booleans → TRUE()/FALSE(), numbers → bare literal, everything else → a quoted string."""
    if value is None:
        return "BLANK()"
    if isinstance(value, bool):
        return "TRUE()" if value else "FALSE()"
    if isinstance(value, (datetime.datetime, datetime.date)):
        return f"DATE({value.year},{value.month},{value.day})"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return '"' + str(value).replace('"', '""') + '"'


class LiveDesktopEvaluator:
    """Evaluates a measure expression against an open Power BI Desktop engine on `port`."""

    def __init__(self, port: int, dll: str | None = None) -> None:
        self.port = port
        self.dll = dll or find_adomd_dll(find_instances())

    def evaluate_measure(self, expression: str, home_table: str) -> ValidationResult:
        """Run the measure in an empty filter context (whole model)."""
        return self._run(expression, home_table, f"[{_PROBE}]")

    def evaluate_at_slice(
        self, expression: str, home_table: str, filters: list[SliceFilter]
    ) -> ValidationResult:
        """Run the measure under a specific slice, e.g. CALCULATE([m], Dim[Col]=val, ...)."""
        if filters:
            preds = ", ".join(f"'{t}'[{c}] = {_format_value(v)}" for t, c, v in filters)
            value_expr = f"CALCULATE([{_PROBE}], {preds})"
        else:
            value_expr = f"[{_PROBE}]"
        return self._run(expression, home_table, value_expr)

    def evaluate_with_defines(
        self, home_table: str, defines: list[tuple[str, str]], target_name: str
    ) -> ValidationResult:
        """Evaluate one measure of a set, DEFINE-ing all of them so cross-references (base→derived) resolve.

        `defines` is [(name, expression)] for every measure in the block; `target_name` is the one to read.
        """
        if not self.dll:
            return ValidationResult(
                ok=False, errors=["未找到 Power BI 的 ADOMD 客户端 DLL，无法实跑验证"], run_verified=False
            )
        escaped_table = home_table.replace("'", "''")
        define_lines = "\n".join(f"  MEASURE '{escaped_table}'[{name.replace(']', ']]')}] = {expr}" for name, expr in defines)
        target = target_name.replace("]", "]]")
        dax = f'DEFINE\n{define_lines}\nEVALUATE ROW("value", [{target}])'
        connected = False
        try:
            with _LiveSession(self.port, self.dll) as session:
                connected = True
                rows = session.query(dax)
        except Exception as exc:  # noqa: BLE001
            return ValidationResult(ok=False, errors=[_clean_error(exc)], run_verified=connected)
        value = next(iter(rows[0].values()), None) if rows else None
        return ValidationResult(ok=True, sample=value, run_verified=True)

    def evaluate_with_defines_at_slice(self, home_table, defines, target_name, filters):
        if not self.dll:
            return ValidationResult(ok=False, errors=["未找到 ADOMD DLL"], run_verified=False)
        table = home_table.replace("'", "''")
        lines = "\n".join(f"MEASURE '{table}'[{n.replace(']', ']]')}] = {e}" for n, e in defines)
        predicates = ", ".join(
            "'" + t.replace("'", "''") + "'[" + c.replace("]", "]]") + "] = " + _format_value(v)
            for t, c, v in filters)
        target = "[" + target_name.replace("]", "]]") + "]"
        value_expr = f"CALCULATE({target}, {predicates})" if predicates else target
        connected = False
        try:
            with _LiveSession(self.port, self.dll) as session:
                connected = True
                rows = session.query(f'DEFINE {lines}\nEVALUATE ROW("value", {value_expr})')
            return ValidationResult(ok=True, sample=next(iter(rows[0].values()), None) if rows else None,
                                    run_verified=True)
        except Exception as exc:
            return ValidationResult(ok=False, errors=[_clean_error(exc)], run_verified=connected)

    def evaluate_table_expr(self, expression: str) -> ValidationResult:
        """Validate a calculated-table expression by running `EVALUATE` on it (capped). Reports the
        shape (rows × cols) on success, or the engine's error — `run_verified` either way."""
        if not self.dll:
            return ValidationResult(
                ok=False, errors=["未找到 Power BI 的 ADOMD 客户端 DLL，无法实跑验证"], run_verified=False
            )
        connected = False
        try:
            with _LiveSession(self.port, self.dll) as session:
                connected = True
                rows = session.query(f"EVALUATE TOPN ( 50, {expression} )")
        except Exception as exc:  # noqa: BLE001
            return ValidationResult(ok=False, errors=[_clean_error(exc)], run_verified=connected)
        ncols = len(rows[0]) if rows else 0
        nrows = len(rows)
        shape = f"{nrows}{'+' if nrows >= 50 else ''} 行 × {ncols} 列"
        return ValidationResult(ok=True, sample=shape, run_verified=True)

    def _run(self, expression: str, home_table: str, value_expr: str) -> ValidationResult:
        if not self.dll:
            # No client DLL → we genuinely cannot run it; report honestly as not run-verified.
            return ValidationResult(
                ok=False, errors=["未找到 Power BI 的 ADOMD 客户端 DLL，无法实跑验证"], run_verified=False
            )
        dax = (
            f"DEFINE MEASURE '{home_table.replace(chr(39), chr(39)*2)}'[{_PROBE}] = {expression}\n"
            f'EVALUATE ROW("value", {value_expr})'
        )
        connected = False
        try:
            with _LiveSession(self.port, self.dll) as session:
                connected = True
                rows = session.query(dax)
        except Exception as exc:  # noqa: BLE001 — the engine's rejection IS the result we want to capture
            return ValidationResult(ok=False, errors=[_clean_error(exc)], run_verified=connected)

        value = next(iter(rows[0].values()), None) if rows else None
        return ValidationResult(ok=True, sample=value, run_verified=True)
