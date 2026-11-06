"""
Parsing the LLM's DAX response.

The generate/optimize prompts ask the model for a fenced ```dax code block plus prose. This module pulls
the measure code (and a suggested name) back out of that markdown so the rest of the pipeline gets a clean
artifact. Kept dependency-free and pure so it's trivially unit-testable against canned responses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A fenced code block; prefer one tagged ```dax but accept any fenced block as a fallback.
_DAX_FENCE = re.compile(r"```dax\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_ANY_FENCE = re.compile(r"```[\w]*\s*\n(.*?)```", re.DOTALL)
# A measure assignment LHS: `Measure Name = ...` (name has no brackets/parens), optionally `MEASURE`-style.
_NAME_ASSIGN = re.compile(r"^\s*([^=\[\]()\n]+?)\s*=", re.MULTILINE)
# Noise lines models sometimes put inside a code block: divider rules (==== / ---- / ****) and markdown
# headers (## ). These are not DAX and must be stripped before the expression is run.
_NOISE_LINE = re.compile(r"^\s*(?:[=\-*_#]{3,}.*|#{1,6}\s.*)$")
# DAX comments: block /* ... */ and line // ... / -- ... . These MUST be stripped before splitting,
# because a comment line containing '=' (e.g. `// Total Change = Price + Volume`) would otherwise be
# mis-read as a measure-assignment boundary and shatter one measure into garbage fragments.
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"(?://|--)[^\n]*")


def _strip_comments(code: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", code))


def _strip_noise(code: str) -> str:
    code = _strip_comments(code)
    return "\n".join(ln for ln in code.splitlines() if not _NOISE_LINE.match(ln)).strip()


@dataclass
class ParsedMeasure:
    name: str          # suggested measure name, or "" if none could be extracted
    code: str          # the DAX code block, verbatim (may include the "Name =" prefix)
    raw: str           # the full LLM response (kept for the explanation panel)


def parse_measure_response(text: str) -> ParsedMeasure:
    """Extract the DAX code block and a suggested measure name from an LLM markdown response.

    In chat mode the reply mixes reasoning, dividers and the measure, so we prefer a fenced block that
    actually *looks* like DAX (has a column/measure ref and an `=`), and never fall back to the whole
    prose — otherwise non-DAX text (e.g. `===== 基础度量 =====`) leaks into the expression and the engine
    rejects it.
    """
    blocks = [b.strip() for b in _DAX_FENCE.findall(text)] or [b.strip() for b in _ANY_FENCE.findall(text)]
    code = ""
    for b in blocks:
        if "[" in b and "=" in b:  # a measure references a column/measure and assigns
            code = b
            break
    if not code and blocks:
        code = blocks[0]

    name = ""
    assign = _NAME_ASSIGN.search(_strip_noise(code))
    if assign:
        candidate = assign.group(1).strip().strip("'\"")
        # guard against false positives like a stray "VAR x =" being read as a name
        if candidate and not candidate.upper().startswith(("VAR ", "RETURN")):
            name = candidate

    return ParsedMeasure(name=name, code=code, raw=text)


def _split_name_expr(block: str) -> tuple[str, str]:
    """Split ONE block into (name, expression) at the first top-level measure-assignment `=`, **keeping
    the model's `//`/`--`/`/* */` comments in the expression** (so they get written into the model).

    Scans char-by-char skipping strings/comments and tracking paren depth, so the `=` inside a comment,
    a string, a `<=`/`>=`, or nested `SWITCH ( TRUE (), x = … )` is not mistaken for the assignment. The
    name (LHS) IS comment-stripped/trimmed; the expression (RHS) is returned verbatim.
    """
    i, n, depth, in_str = 0, len(block), 0, False
    while i < n:
        ch = block[i]
        if in_str:
            if ch == '"':
                in_str = False
            i += 1
        elif ch == '"':
            in_str = True
            i += 1
        elif block.startswith(("//", "--"), i):
            nl = block.find("\n", i)
            i = n if nl == -1 else nl
        elif block.startswith("/*", i):
            end = block.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch == "(":
            depth += 1
            i += 1
        elif ch == ")":
            depth = max(0, depth - 1)
            i += 1
        elif ch == "=" and depth == 0 and (i == 0 or block[i - 1] not in "<>=!") and block[i + 1 : i + 2] != "=":
            name = _strip_comments(block[:i]).strip().strip("'\"")
            if name.upper().startswith(("VAR ", "RETURN")):
                return "", block.strip()
            return name, block[i + 1 :].strip()
        else:
            i += 1
    return "", block.strip()


def parse_dax_blocks(text: str) -> list[tuple[str, str]]:
    """Collect EVERY fenced ```dax block and parse each as ONE object → [(name, expression)].

    One block = one measure/calculated table (the prompt asks the model for exactly one object per block),
    so we do NOT split within a block on `Name =`. Divider/header noise lines are dropped, but DAX
    comments are PRESERVED in the expression (they are written into the model). A block with no top-level
    `Name =` keeps name "" (caller can default it).
    """
    blocks = [b.strip() for b in _DAX_FENCE.findall(text)] or [b.strip() for b in _ANY_FENCE.findall(text)]
    out: list[tuple[str, str]] = []
    for b in blocks:
        # drop only divider/header noise (===== , ## ...) — keep // -- /* */ comments
        kept = "\n".join(ln for ln in b.splitlines() if not _NOISE_LINE.match(ln)).strip()
        name, expr = _split_name_expr(kept)
        if expr:
            out.append((name, expr))
    return out


def has_dax_block(text: str) -> bool:
    """True if the text contains a fenced code block (i.e. the model returned a measure, not prose).

    Used by calibrated generation to tell a revised measure from a clarifying question."""
    return bool(_DAX_FENCE.search(text) or _ANY_FENCE.search(text))


def _paren_depths(text: str) -> list[int]:
    """Paren depth BEFORE each character index (ignoring parens inside "..." string literals).

    Used to tell a real measure boundary (`Name =` at top level) from a comparison inside a function
    like `SWITCH ( TRUE (), Sel = "Price", ... )`, whose `Sel =` lines sit at depth >= 1."""
    depths = [0] * (len(text) + 1)
    depth = 0
    in_str = False
    for i, ch in enumerate(text):
        depths[i] = depth
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
    depths[len(text)] = depth
    return depths


def _measure_bounds(cleaned: str) -> list[re.Match[str]]:
    """`Name =` matches that are real measure boundaries: at paren-depth 0 and not a VAR/RETURN line."""
    depths = _paren_depths(cleaned)
    return [
        m for m in _NAME_ASSIGN.finditer(cleaned)
        if depths[m.start()] == 0
        and not m.group(1).strip().upper().startswith(("VAR ", "RETURN"))
    ]


def split_measures(code: str) -> list[tuple[str, str]]:
    """Split a (possibly multi-measure) DAX block into [(name, expression)].

    Each measure runs from its top-level `Name = ` line to the next one; `VAR`/`RETURN` lines and any
    `expr = value` lines nested inside a function (e.g. `SWITCH ( TRUE (), x = 1, … )`) belong to the
    current measure, not a new one. This is what lets the app handle a model that emits a small measure
    library (base + derived) — each measure can then be validated and written individually.
    """
    cleaned = _strip_noise(code)
    bounds = _measure_bounds(cleaned)
    measures: list[tuple[str, str]] = []
    for i, m in enumerate(bounds):
        name = m.group(1).strip().strip("'\"")
        end = bounds[i + 1].start() if i + 1 < len(bounds) else len(cleaned)
        expr = cleaned[m.end():end].strip()
        if name and expr:
            measures.append((name, expr))
    return measures


def measure_expression(code: str, name: str) -> str:
    """Return just the measure body (RHS): everything after the first `Name = ` assignment.

    Strips divider/header/comment noise first, then takes the first top-level (paren-depth 0) assignment,
    so leading prose, `==== 基础度量 ====` dividers, and nested `x = …` comparisons are not mistaken for
    it. Live evaluation needs the expression alone (it supplies its own probe name), so `Total = SUM ( … )`
    becomes `SUM ( … )`.
    """
    cleaned = _strip_noise(code)
    bounds = _measure_bounds(cleaned)
    return cleaned[bounds[0].end():].strip() if bounds else cleaned
