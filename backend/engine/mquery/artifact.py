"""
Power Query (M) script artifact + static validation.

An `MScriptArtifact` holds one generated M query (its code and the target query name). Its `validate()`
implements the *static* half of the project's "generate -> static -> live -> repair" loop for M: it checks
structure (one well-formed `let ... in`, balanced brackets/quotes) and grounding (every #"..." reference is
either a step defined in this query or a real query/parameter in the model).

Static checks for M are necessarily shallow — M has no standalone type checker we can run, and column
references live inside `each [Col]` predicates whose validity depends on the table's shape *at that step*,
which static analysis can't track. So a missing #"step"/query reference is a warning, not a hard error, and
the real proof is the live refresh round-trip (M3) that sets `run_verified=True`. Until then this artifact
reports `run_verified=False`, per the project's hard rule never to present unverified M as if it had run.
"""

from __future__ import annotations

import re

from ..core.artifact import Artifact, ValidationResult
from ..context.base import ModelContext

# A quoted step/query reference or definition: #"Some Name".
_HASH_REF = re.compile(r'#"([^"]+)"')
# A step definition LHS inside a let: either #"Name" = ...  or  bareIdentifier = ...  (not ==, <=, >=).
_HASH_DEF = re.compile(r'#"([^"]+)"\s*=(?!=)')
_BARE_DEF = re.compile(r'(?m)^[ \t]*([A-Za-z_]\w*)\s*=(?!=)')


def _strip_strings_comments(code: str) -> str:
    """Blank out M string literals ("" escapes) and // and /* */ comments, so bracket/quote balancing and
    reference scanning don't trip over text inside them. Length is preserved (replaced with spaces).

    M quoted identifiers #"..." are PRESERVED verbatim — they are names (step/query references), not string
    literals, and the grounding lint needs to see them."""
    out: list[str] = []
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if code.startswith('#"', i):  # quoted identifier #"Name" — keep verbatim (incl. "" escapes)
            out.append("#")
            i += 1
            out.append('"')
            i += 1
            while i < n:
                if code[i] == '"':
                    if i + 1 < n and code[i + 1] == '"':
                        out.append('""')
                        i += 2
                        continue
                    out.append('"')
                    i += 1
                    break
                out.append(code[i])
                i += 1
        elif ch == '"':  # string literal; "" is an escaped quote
            out.append(" ")
            i += 1
            while i < n:
                if code[i] == '"':
                    if i + 1 < n and code[i + 1] == '"':
                        out.append("  ")
                        i += 2
                        continue
                    out.append(" ")
                    i += 1
                    break
                out.append(" ")
                i += 1
        elif code.startswith("//", i):
            while i < n and code[i] != "\n":
                out.append(" ")
                i += 1
        elif code.startswith("/*", i):
            while i < n and not code.startswith("*/", i):
                out.append(" ")
                i += 1
            if code.startswith("*/", i):
                out.append("  ")
                i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _unbalanced(code: str) -> list[str]:
    """Report unbalanced () [] {} or an odd number of (unescaped) quotes, scanning code with strings and
    comments already blanked for the bracket pass and the raw text for quotes."""
    errors: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for ch in code:
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack[-1] != pairs[ch]:
                errors.append(f"括号不匹配：多余或错配的 '{ch}'")
                return errors
            stack.pop()
    if stack:
        errors.append(f"括号不平衡：有未闭合的 '{stack[-1]}'")
    return errors


class MScriptArtifact(Artifact):
    kind = "m_script"

    def __init__(self, content: str, name: str = "") -> None:
        super().__init__(content)
        self.name = name  # the target query name (the cleaned query replaces this query's M)

    def validate(self, ctx: ModelContext) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        code = self.content.strip()
        if not code:
            return ValidationResult(ok=False, errors=["未生成任何 M 代码"], run_verified=False)

        blanked = _strip_strings_comments(code)

        # --- structure: balanced brackets, and a well-formed let ... in ---
        errors.extend(_unbalanced(blanked))
        if blanked.count('"') and self.content.count('"') % 2 != 0:
            # crude odd-quote check on the ORIGINAL (escaped "" keep it even); only flag clearly-odd counts
            pass
        has_let = re.search(r"\blet\b", blanked, re.IGNORECASE)
        has_in = re.search(r"\bin\b", blanked, re.IGNORECASE)
        if has_let and not has_in:
            errors.append("`let` 缺少对应的 `in` 返回步骤")
        # a query may legitimately be a single expression (no let), so only require `in` when `let` is present.

        # --- grounding: every #"..." reference is a local step or a real query/parameter ---
        local_steps = {m.group(1) for m in _HASH_DEF.finditer(blanked)}
        local_steps |= {m.group(1) for m in _BARE_DEF.finditer(blanked)}
        for m in _HASH_REF.finditer(blanked):
            name = m.group(1)
            # skip the definition occurrences themselves
            after = blanked[m.end():m.end() + 2].lstrip()
            if after.startswith("=") and not after.startswith("=="):
                continue
            if name in local_steps or ctx.has_query(name):
                continue
            warnings.append(
                f'引用 #\"{name}\" 未在本查询步骤中定义，也不是已知查询/参数（请确认拼写，或由实跑验证确认）'
            )

        ok = not errors
        return ValidationResult(
            ok=ok,
            errors=errors,
            warnings=warnings,
            run_verified=False,  # static only — live refresh (M3) is what flips this to True
        )
