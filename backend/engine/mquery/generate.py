"""
Parsing the LLM's Power Query (M) response.

The clean/explain prompts ask the model for a fenced ```powerquery code block (a full `let ... in` query)
plus prose. This module pulls the M code back out of that markdown so the rest of the pipeline gets a clean
artifact. Unlike DAX, an M query has no "Name = ..." assignment to extract — the query's name is the source
query being cleaned (carried separately), so parsing only needs to recover the code. Kept dependency-free
and pure so it's trivially unit-testable against canned responses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A fenced code block tagged as M; Power Query goes by several tags in the wild. Prefer a tagged block,
# fall back to any fenced block.
_M_FENCE = re.compile(r"```(?:powerquery|power-query|pq|mscript|m)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_ANY_FENCE = re.compile(r"```[\w-]*\s*\n(.*?)```", re.DOTALL)
# A block that actually looks like M: has a `let` and an `in`, or at least a step/query reference.
_LOOKS_M = re.compile(r"\blet\b.*\bin\b", re.DOTALL | re.IGNORECASE)


@dataclass
class ParsedM:
    code: str  # the M code block, verbatim (a full `let ... in` query, ideally)
    raw: str   # the full LLM response (kept for the explanation panel)


def _pick_m_block(blocks: list[str]) -> str:
    """Prefer a block that looks like a real `let ... in` query; else the first non-empty block."""
    for b in blocks:
        if _LOOKS_M.search(b):
            return b
    return blocks[0] if blocks else ""


def parse_m_response(text: str) -> ParsedM:
    """Extract the M code block from an LLM markdown response.

    Prefers a fenced block tagged powerquery/m, then any fenced block that looks like a `let ... in` query,
    so prose (reasoning, dividers) never leaks into the code that gets refreshed."""
    tagged = [b.strip() for b in _M_FENCE.findall(text)]
    blocks = tagged or [b.strip() for b in _ANY_FENCE.findall(text)]
    return ParsedM(code=_pick_m_block(blocks), raw=text)


def parse_m_blocks(text: str) -> list[str]:
    """Collect every fenced M code block (verbatim). One block = one query; usually there is exactly one."""
    tagged = [b.strip() for b in _M_FENCE.findall(text)]
    blocks = tagged or [b.strip() for b in _ANY_FENCE.findall(text)]
    return [b for b in blocks if b]


def has_m_block(text: str) -> bool:
    """True if the text contains a fenced code block (i.e. the model returned a query, not just prose)."""
    return bool(_M_FENCE.search(text) or _ANY_FENCE.search(text))
