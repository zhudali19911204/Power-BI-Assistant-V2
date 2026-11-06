"""
DAX measure artifact + static validation.

A `DaxMeasureArtifact` holds one generated measure (its DAX text and a suggested name). Its `validate()`
implements the *static* half of the project's "generate -> static -> live -> repair" loop: it parses the
expression's object references and checks each against the real `ModelContext`. A reference to a
non-existent column is a hard error (it compiles and looks right but fails at runtime — exactly the class
of bug grounding is meant to stop); a reference to an unknown measure is a warning, because the parse
can't always tell a genuine typo from the measure's own name or a base measure defined elsewhere.

Static checks can only ever prove a measure *might* be right. Live `EVALUATE` verification (M6) is what
sets `run_verified=True`; until then this artifact always reports `run_verified=False`, per the project's
hard rule never to present statically-checked DAX as if it had been executed.
"""

from __future__ import annotations

import re

from ..core.artifact import Artifact, ValidationResult
from ..context.base import ModelContext

# A qualified column reference: 'Table Name'[Column] or Table[Column].
_COLUMN_REF = re.compile(r"(?:'((?:[^']|'')+)'|([A-Za-z_][\w]*))\s*\[((?:[^\]]|\]\])+)\]")
# Any bracketed token, used to find bare measure references [Measure] that are NOT column refs.
_BRACKET = re.compile(r"\[([^\]]+)\]")


def _code_only(content: str) -> str:
    """Mask strings and comments, while preserving quoted table and bracketed identifiers."""
    parts = []
    i = 0
    while i < len(content):
        if content.startswith(("//", "--"), i):
            end = content.find("\n", i)
            end = len(content) if end < 0 else end
            parts.append(" " * (end - i))
            i = end
        elif content.startswith("/*", i):
            end = content.find("*/", i + 2)
            end = len(content) if end < 0 else end + 2
            parts.append(" " * (end - i))
            i = end
        elif content[i] in ("'", '[', '"'):
            start, quote = i, content[i]
            closing = ']' if quote == '[' else quote
            i += 1
            while i < len(content):
                if content[i] == closing:
                    if content[i:i+2] == closing * 2:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            parts.append(" " * (i-start) if quote == '"' else content[start:i])
        else:
            parts.append(content[i])
            i += 1
    return "".join(parts)


class DaxMeasureArtifact(Artifact):
    kind = "dax_measure"

    def __init__(self, content: str, name: str = "") -> None:
        super().__init__(content)
        self.name = name

    def validate(self, ctx: ModelContext) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        code = _code_only(self.content)

        # --- column references: every Table[Column] must exist in the model ---
        column_spans: list[tuple[int, int]] = []
        for m in _COLUMN_REF.finditer(code):
            column_spans.append(m.span())
            table = (m.group(1) or m.group(2)).replace("''", "'")
            column = m.group(3).strip().replace("]]", "]")
            if not ctx.has_table(table):
                errors.append(f"表 '{table}' 不在模型中（引用 '{table}'[{column}]）")
            elif table in ctx.calculated_tables:
                # Static parse can't expose a calculated table's columns — don't claim it's wrong.
                warnings.append(
                    f"'{table}' 是计算表，无法静态校验其列 '{column}'（需实跑确认）"
                )
            elif not ctx.has_column(table, column) and not any(x.table == table and x.name == column for x in ctx.measures):
                errors.append(f"列 '{table}'[{column}] 不在模型中")

        # --- bare measure references [X] that aren't part of a Table[Column] ---
        for m in _BRACKET.finditer(code):
            if any(s <= m.start() and m.end() <= e for s, e in column_spans):
                continue  # this bracket is the [Column] half of a column ref, already handled
            name = m.group(1).strip()
            if name == self.name:
                continue  # the measure referring to itself by name (e.g. "Name = ..." LHS)
            if not ctx.has_measure(name):
                warnings.append(f"度量值 [{name}] 不在模型中（若为本次新建/基础度量可忽略）")

        # --- cheap structural check: balanced parentheses ---
        structure = re.sub(r"'(?:[^']|'')*'|\[(?:[^\]]|\]\])*\]", "", code)
        if structure.count("(") != structure.count(")"):
            errors.append("括号不平衡")

        ok = not errors
        return ValidationResult(
            ok=ok,
            errors=errors,
            warnings=warnings,
            run_verified=False,  # static only — live EVALUATE (M6) is what flips this to True
        )
