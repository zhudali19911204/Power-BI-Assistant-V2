"""
Artifact abstraction.

An `Artifact` is something a capability produces — a DAX measure, an M script, a TMDL snippet, a
theme.json. They share one contract: `validate(ctx)` checks the artifact against the real model, and
(later, where supported) `apply(target)` writes it back. The `run_verified` flag on `ValidationResult`
is deliberate: per the project's hard requirement, DAX that has only passed static checks must never be
presented as if it had been executed (see DEVELOPMENT_PLAN.md §5.4 / §13).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..context.base import ModelContext


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sample: Any | None = None  # e.g. the value returned by a live EVALUATE
    run_verified: bool = False  # True ONLY when confirmed by real execution, not static checks


@runtime_checkable
class MeasureEvaluator(Protocol):
    """Runs a DAX measure expression against a real engine and reports what actually happened.

    The contract is deliberately about *execution*: the returned `ValidationResult` carries
    `run_verified=True` whether the measure succeeded (with a `sample` value) or the engine rejected it
    (a verified failure, with the engine's message in `errors`). This is what lets the product close the
    generate -> static -> live -> repair loop and never present unverified DAX as if it had run.
    """

    def evaluate_measure(self, expression: str, home_table: str) -> ValidationResult: ...


@dataclass
class ApplyResult:
    ok: bool
    detail: str = ""


class Artifact(ABC):
    """Base class for everything the assistant generates."""

    kind: str = "artifact"  # subclasses set e.g. "dax_measure", "m_script", "tmdl", "theme_json"

    def __init__(self, content: str) -> None:
        self.content = content

    @abstractmethod
    def validate(self, ctx: ModelContext) -> ValidationResult:
        """Check the artifact against the model (static, and live where available)."""
        ...

    def apply(self, target: Any) -> ApplyResult:
        """Write the artifact back to a target. Not all artifacts/phases support this yet."""
        raise NotImplementedError(f"{type(self).__name__} does not support apply()")
