"""
Model context abstraction.

`ModelContext` is the grounding fact base for every capability: the real tables, columns, relationships
and measures of the Power BI model. Generation prompts are built from `serialize_for_prompt()`, and
static validation checks references against `has_table` / `has_column` / `has_measure`. Keeping this as
plain dataclasses (no dependency on how it was loaded) lets different `ContextSource`s — a .pbix file, a
live Desktop connection, a PBIP folder — all produce the same shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Column:
    name: str
    dtype: str
    table: str
    cardinality: int | None = None  # distinct-count hint, when available (drives perf advice)


@dataclass
class Relationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cross_filter: str = "single"  # "single" | "both"
    is_active: bool = True


@dataclass
class Measure:
    name: str
    table: str
    expression: str


@dataclass
class ModelContext:
    """A compact, source-independent representation of a Power BI / Tabular model."""

    tables: dict[str, list[Column]] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    date_tables: list[str] = field(default_factory=list)  # tables marked as Date tables
    # Calculated tables (name -> defining DAX). Static .pbix parsing often can't expose their
    # columns, so they're tracked separately and honestly flagged rather than silently dropped —
    # otherwise a measure referencing one would be wrongly judged "non-existent".
    calculated_tables: dict[str, str] = field(default_factory=dict)

    # --- Power Query (M) grounding (phase 2) ---
    # Each loaded table's defining M, from its import partition's QueryDefinition (table name -> M).
    # Calculated tables have no M and are absent here. This is the starting point the M assistant
    # rewrites/cleans on top of.
    table_queries: dict[str, str] = field(default_factory=dict)
    # Named M expressions that don't load to a table: parameters and shared/staging queries
    # (name -> M). Cleaning M often references these, so they're part of the grounding facts.
    shared_expressions: dict[str, str] = field(default_factory=dict)
    # Power Query "query group" folder each loaded query sits in (query name -> folder path);
    # absent / "" means ungrouped (root). Drives the sidebar's Power Query browser.
    query_folders: dict[str, str] = field(default_factory=dict)

    # --- grounding queries (used by static validation) ---
    def has_table(self, table: str) -> bool:
        return table in self.tables or table in self.calculated_tables

    def has_column(self, table: str, column: str) -> bool:
        return any(c.name == column for c in self.tables.get(table, []))

    def has_measure(self, name: str) -> bool:
        return any(m.name == name for m in self.measures)

    def has_query(self, name: str) -> bool:
        """True if `name` is a referenceable M query (a loaded table query or a shared expression)."""
        return name in self.table_queries or name in self.shared_expressions

    def query_names(self) -> list[str]:
        """All referenceable M query names (loaded-table queries first, then shared/parameter ones)."""
        return list(self.table_queries) + list(self.shared_expressions)

    # --- prompt serialization ---
    def serialize_for_prompt(self, focus: list[str] | None = None) -> str:
        """Render the model as compact text for an LLM prompt.

        When `focus` is given, include only those tables plus any directly related to them — this keeps
        large models within token budget while preserving the relationships needed to reason correctly.
        """
        table_names = list(self.tables)
        if focus:
            keep = set(focus)
            for r in self.relationships:
                if r.from_table in keep or r.to_table in keep:
                    keep.update({r.from_table, r.to_table})
            table_names = [t for t in table_names if t in keep]

        lines: list[str] = ["TABLES:"]
        for t in table_names:
            marker = "  (marked Date table)" if t in self.date_tables else ""
            lines.append(f"  '{t}'{marker}:")
            for c in self.tables[t]:
                card = f", ~{c.cardinality} distinct" if c.cardinality is not None else ""
                lines.append(f"    '{t}'[{c.name}] ({c.dtype}{card})")

        rels = [
            r for r in self.relationships
            if not focus or (r.from_table in table_names and r.to_table in table_names)
        ]
        if rels:
            lines.append("RELATIONSHIPS:")
            for r in rels:
                arrow = "<->" if r.cross_filter == "both" else "->"
                active = "" if r.is_active else "  [inactive]"
                lines.append(
                    f"  '{r.from_table}'[{r.from_column}] {arrow} '{r.to_table}'[{r.to_column}]{active}"
                )

        if self.calculated_tables:
            lines.append("CALCULATED TABLES (defined by DAX; columns not exposed by static parse):")
            for name in self.calculated_tables:
                lines.append(f"  '{name}'  (calculated)")

        measures = [m for m in self.measures if not focus or m.table in table_names]
        if measures:
            lines.append("MEASURES:")
            for m in measures:
                lines.append(f"  [{m.name}] = {m.expression}")

        return "\n".join(lines)

    def serialize_query_for_prompt(self, name: str) -> str:
        """Render the M grounding for cleaning query `name`: its current M, its output columns, and the
        other queries/parameters it may reference. Used to build a grounded Power Query (M) prompt."""
        lines: list[str] = []
        current = self.table_queries.get(name) or self.shared_expressions.get(name)
        if current is not None:
            lines.append(f"CURRENT QUERY '{name}' (M):")
            lines.append(current.strip())
        if name in self.tables and self.tables[name]:
            lines.append(f"\nOUTPUT COLUMNS of '{name}':")
            for c in self.tables[name]:
                lines.append(f"  [{c.name}] ({c.dtype})")
        others = [q for q in self.query_names() if q != name]
        if others:
            lines.append("\nOTHER REFERENCEABLE QUERIES / PARAMETERS (reference as #\"Name\"):")
            for q in others:
                kind = "parameter/shared" if q in self.shared_expressions else "table query"
                lines.append(f"  '{q}'  ({kind})")
        return "\n".join(lines)


class ContextSource(ABC):
    """Loads a `ModelContext` from somewhere (a .pbix file, a live Desktop, a PBIP folder)."""

    @abstractmethod
    def load(self) -> ModelContext:
        ...
