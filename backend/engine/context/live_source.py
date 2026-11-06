"""
Load a `ModelContext` from a **live** Power BI Desktop engine (the only source in phase 1).

Static .pbix parsing was dropped because it cannot see calculated tables' columns or relationships, which
makes accurate DAX grounding impossible (proven on real models). Instead we connect to the local Analysis
Services engine that Power BI Desktop runs while a report is open, and read the *fully materialized* model
via TMSCHEMA DMVs — calc tables, every relationship, measures, all of it.

Zero extra install: Power BI ships its own ADOMD client (`Microsoft.PowerBI.AdomdClient.dll`) in its bin
dir; we load it via pythonnet. The engine is `msmdsrv.exe`; we discover its loopback port dynamically
(both change every time Desktop reopens). Windows-only, requires the report open in Desktop — that's the
accepted cost of accuracy (see memory: phase1-is-live-only-static-dropped).
"""

from __future__ import annotations

import datetime
import glob
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from .base import Column, ContextSource, Measure, ModelContext, Relationship

_DLL_NAME = "Microsoft.PowerBI.AdomdClient.dll"
_ADOMD_CONN_TYPE = "Microsoft.AnalysisServices.AdomdClient.AdomdConnection"
_AUTO_DATE = re.compile(r"^(LocalDateTable_|DateTableTemplate_)")

# TMSCHEMA enum mappings, calibrated against a live engine.
_DTYPE = {1: "auto", 2: "string", 6: "int64", 8: "double", 9: "datetime", 10: "decimal", 11: "boolean", 17: "binary"}
_COL_ROWNUMBER = 3  # TMSCHEMA_COLUMNS.Type: 1=Data, 2=Calculated, 3=RowNumber (drop), 4=CalcTableColumn


def _dtype(code: Any) -> str:
    return _DTYPE.get(code, "unknown")

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW — don't flash a console for the powershell/netstat calls


# ------------------------------------------------------------------------------------------------
# Discovery — find open Desktop instances (port) and Power BI's bundled ADOMD DLL.
# ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DesktopInstance:
    """One running Power BI Desktop engine: its loopback port (for connecting) and the exe that hosts it."""

    port: int
    pid: int
    exe_path: str = ""


def _run(args: list[str]) -> str:
    try:
        out = subprocess.run(
            args, capture_output=True, encoding="mbcs", errors="replace",
            creationflags=_NO_WINDOW, timeout=20,
        )
        return out.stdout or ""
    except Exception:  # noqa: BLE001 — discovery is best-effort; absence is reported as "no instances"
        return ""


def _msmdsrv_processes() -> list[tuple[str, str]]:
    """[(pid, exe_path)] for every running msmdsrv.exe (the Desktop analysis-services engine)."""
    ps = "Get-Process msmdsrv -ErrorAction SilentlyContinue | ForEach-Object { $_.Id.ToString() + '|' + $_.Path }"
    out = _run(["powershell", "-NoProfile", "-Command", ps])
    procs: list[tuple[str, str]] = []
    for line in out.splitlines():
        if "|" in line:
            pid, path = line.split("|", 1)
            procs.append((pid.strip(), path.strip()))
    return procs


def _listening_ports_by_pid() -> dict[str, int]:
    """{pid: loopback_port} from netstat for processes listening on 127.0.0.1."""
    out = _run(["netstat", "-ano"])
    ports: dict[str, int] = {}
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) >= 5 and parts[1].startswith("127.0.0.1"):
            pid = parts[-1]
            if pid not in ports:  # keep the first loopback port per pid
                try:
                    ports[pid] = int(parts[1].rsplit(":", 1)[1])
                except ValueError:
                    pass
    return ports


def find_instances() -> list[DesktopInstance]:
    """Discover open Power BI Desktop engines. Empty list = no report open (or non-Windows)."""
    procs = _msmdsrv_processes()
    ports = _listening_ports_by_pid()
    out: list[DesktopInstance] = []
    for pid, path in procs:
        if pid in ports:
            out.append(DesktopInstance(port=ports[pid], pid=int(pid), exe_path=path))
    return out


def find_adomd_dll(instances: list[DesktopInstance] | None = None) -> str | None:
    """Locate Power BI's bundled ADOMD client DLL. Prefers the bin dir of a running engine."""
    candidates: list[str] = []
    for inst in instances or []:
        if inst.exe_path:
            candidates.append(os.path.join(os.path.dirname(inst.exe_path), _DLL_NAME))
    candidates.append(os.path.join(r"C:\Program Files\Microsoft Power BI Desktop\bin", _DLL_NAME))
    candidates += glob.glob(
        os.path.join(r"C:\Program Files\WindowsApps", "Microsoft.MicrosoftPowerBIDesktop*_x64__*", "bin", _DLL_NAME)
    )
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


# ------------------------------------------------------------------------------------------------
# ADOMD session — load the assembly once, run DMV queries returning plain dict rows.
# ------------------------------------------------------------------------------------------------

_conn_type: Any = None


def _connection_type(dll: str) -> Any:
    global _conn_type
    if _conn_type is None:
        import clr  # type: ignore[import-untyped]  # noqa: F401  # pythonnet; lazy import
        from System.Reflection import Assembly  # type: ignore[import-not-found]

        asm = Assembly.LoadFrom(dll)
        _conn_type = asm.GetType(_ADOMD_CONN_TYPE)
        if _conn_type is None:
            raise RuntimeError(f"在 {os.path.basename(dll)} 中找不到 {_ADOMD_CONN_TYPE}")
    return _conn_type


class _LiveSession:
    """A single open ADOMD connection to a Desktop engine port; use as a context manager."""

    def __init__(self, port: int, dll: str) -> None:
        t = _connection_type(dll)  # imports clr + loads the assembly first
        import System  # type: ignore[import-not-found]  # only importable after clr is loaded

        connstr = f"Data Source=localhost:{port}"
        self._conn = System.Activator.CreateInstance(t, System.Array[System.Object]([connstr]))

    def __enter__(self) -> "_LiveSession":
        self._conn.Open()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self._conn.Close()
        except Exception:  # noqa: BLE001 — best-effort close
            pass

    def query(self, dax: str, max_rows: int | None = None, timeout_seconds: int = 60) -> list[dict[str, Any]]:
        cmd = self._conn.CreateCommand()
        cmd.CommandTimeout = timeout_seconds
        cmd.CommandText = dax
        rdr = cmd.ExecuteReader()
        try:
            n = rdr.FieldCount
            names = [rdr.GetName(i) for i in range(n)]
            rows: list[dict[str, Any]] = []
            while rdr.Read():
                rows.append({names[i]: _to_py(None if rdr.IsDBNull(i) else rdr.GetValue(i)) for i in range(n)})
                if max_rows is not None and len(rows)>=max_rows:
                    break
            return rows
        finally:
            rdr.Close()


def _safe_query(session: "_LiveSession", dax: str) -> list[dict[str, Any]]:
    """Run a DMV query, returning [] if the engine rejects it (older engines may lack some DMVs)."""
    try:
        return session.query(dax)
    except Exception:  # noqa: BLE001 — optional grounding; absence degrades gracefully
        return []


def _to_py(value: Any) -> Any:
    """Coerce a value returned by ADOMD to a native Python type. pythonnet auto-converts primitives but
    leaves System.DateTime / System.Decimal as .NET objects, which can't be pickled into session_state."""
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    try:
        import System  # type: ignore[import-not-found]

        if isinstance(value, System.DateTime):
            return datetime.datetime(
                value.Year, value.Month, value.Day, value.Hour, value.Minute, value.Second
            )
        if isinstance(value, System.Decimal):
            return float(System.Decimal.ToDouble(value))
    except Exception:  # noqa: BLE001 — fall back to a string for any other .NET type
        pass
    return str(value)


# ------------------------------------------------------------------------------------------------
# Pure builder — turn DMV rows into a ModelContext. Separated from .NET so it's unit-testable.
# ------------------------------------------------------------------------------------------------

_PARTITION_M_TYPE = (1, 4)  # TMSCHEMA_PARTITIONS.Type: 1=Query, 4=M carry an M QueryDefinition (2=Calculated)


def build_model_context(
    tables: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    measures: list[dict[str, Any]],
    partitions: list[dict[str, Any]] | None = None,
    expressions: list[dict[str, Any]] | None = None,
    query_groups: list[dict[str, Any]] | None = None,
) -> ModelContext:
    """Build a ModelContext from raw TMSCHEMA DMV rows (auto-date tables and RowNumber columns dropped)."""
    tname = {r["ID"]: r["Name"] for r in tables}
    is_auto = {tid: bool(_AUTO_DATE.match(nm)) for tid, nm in tname.items()}
    date_tables = [r["Name"] for r in tables if r.get("DataCategory") == "Time" and not is_auto.get(r["ID"])]

    # columns: resolve names for relationship lookup (all), but only attach non-auto, non-rownumber to tables
    cname: dict[Any, str] = {}
    tcols: dict[str, list[Column]] = {
        r["Name"]: [] for r in tables if not is_auto.get(r["ID"])  # ensure every real table appears
    }
    for c in columns:
        if c.get("Type") == _COL_ROWNUMBER:
            continue
        name = c.get("ExplicitName") or c.get("InferredName") or "?"
        cname[c["ID"]] = name
        tid = c["TableID"]
        if is_auto.get(tid):
            continue
        t = tname.get(tid)
        if t is None:
            continue
        tcols.setdefault(t, []).append(Column(name=name, dtype=_dtype(c.get("ExplicitDataType")), table=t))

    rels: list[Relationship] = []
    for r in relationships:
        ft, tt = tname.get(r["FromTableID"]), tname.get(r["ToTableID"])
        if ft is None or tt is None or is_auto.get(r["FromTableID"]) or is_auto.get(r["ToTableID"]):
            continue
        rels.append(
            Relationship(
                from_table=ft, from_column=cname.get(r["FromColumnID"], "?"),
                to_table=tt, to_column=cname.get(r["ToColumnID"], "?"),
                cross_filter="both" if r.get("CrossFilteringBehavior") == 2 else "single",
                is_active=bool(r.get("IsActive", True)),
            )
        )

    meass: list[Measure] = []
    for m in measures:
        t = tname.get(m["TableID"])
        if t is None or is_auto.get(m["TableID"]):
            continue
        meass.append(Measure(name=m["Name"], table=t, expression=str(m.get("Expression", "")).strip()))

    # Power Query (M): each loaded table's defining M (from its M/Query partition), plus named
    # expressions (parameters / shared queries) that don't load to a table.
    gid_folder = {g["ID"]: str(g.get("Folder") or "").strip() for g in (query_groups or [])}
    table_queries: dict[str, str] = {}
    query_folders: dict[str, str] = {}
    for p in partitions or []:
        t = tname.get(p.get("TableID"))
        if t is None or is_auto.get(p.get("TableID")):
            continue
        qd = str(p.get("QueryDefinition") or "").strip()
        if qd and (p.get("Type") in _PARTITION_M_TYPE or qd.lower().startswith("let")):
            if t not in table_queries:  # first M partition per table
                table_queries[t] = qd
                folder = gid_folder.get(p.get("QueryGroupID"), "")
                if folder:
                    query_folders[t] = folder

    shared_expressions: dict[str, str] = {}
    for e in expressions or []:
        nm = e.get("Name")
        expr = str(e.get("Expression") or "").strip()
        if nm and expr:
            shared_expressions[nm] = expr

    # calculated_tables stays empty: live exposes their columns, so they're just regular `tables` now.
    return ModelContext(
        tables=tcols,
        relationships=rels,
        measures=meass,
        date_tables=date_tables,
        table_queries=table_queries,
        shared_expressions=shared_expressions,
        query_folders=query_folders,
    )


# ------------------------------------------------------------------------------------------------
# The ContextSource.
# ------------------------------------------------------------------------------------------------

_Q_TABLES = "SELECT [ID],[Name],[DataCategory] FROM $SYSTEM.TMSCHEMA_TABLES"
_Q_COLUMNS = "SELECT [ID],[TableID],[ExplicitName],[InferredName],[ExplicitDataType],[Type] FROM $SYSTEM.TMSCHEMA_COLUMNS"
_Q_RELS = (
    "SELECT [FromTableID],[FromColumnID],[ToTableID],[ToColumnID],[IsActive],[CrossFilteringBehavior] "
    "FROM $SYSTEM.TMSCHEMA_RELATIONSHIPS"
)
_Q_MEASURES = "SELECT [TableID],[Name],[Expression] FROM $SYSTEM.TMSCHEMA_MEASURES"
_Q_PARTITIONS = "SELECT [TableID],[Name],[QueryDefinition],[Type],[QueryGroupID] FROM $SYSTEM.TMSCHEMA_PARTITIONS"
_Q_EXPRESSIONS = "SELECT [Name],[Kind],[Expression] FROM $SYSTEM.TMSCHEMA_EXPRESSIONS"
_Q_QUERY_GROUPS = "SELECT [ID],[Folder] FROM $SYSTEM.TMSCHEMA_QUERY_GROUPS"


class LiveDesktopSource(ContextSource):
    """Reads the model from an open Power BI Desktop engine on the given loopback port."""

    def __init__(self, port: int, dll: str | None = None) -> None:
        self.port = port
        self.dll = dll or find_adomd_dll(find_instances())

    def load(self) -> ModelContext:
        if not self.dll:
            raise RuntimeError("未找到 Power BI 的 ADOMD 客户端 DLL（请确认 Power BI Desktop 已安装）")
        with _LiveSession(self.port, self.dll) as s:
            tables = s.query(_Q_TABLES)
            columns = s.query(_Q_COLUMNS)
            rels = s.query(_Q_RELS)
            measures = s.query(_Q_MEASURES)
            partitions = _safe_query(s, _Q_PARTITIONS)  # M grounding; tolerate older engines
            expressions = _safe_query(s, _Q_EXPRESSIONS)
            query_groups = _safe_query(s, _Q_QUERY_GROUPS)  # Power Query folder structure
        return build_model_context(tables, columns, rels, measures, partitions, expressions, query_groups)

    def column_values(self, table: str, column: str, top: int = 500) -> list[Any]:
        """Distinct values of a column (for the calibration slice dropdown). Capped via TOPN."""
        if not self.dll:
            return []
        dax = f"EVALUATE TOPN({top}, VALUES('{table}'[{column}]))"
        with _LiveSession(self.port, self.dll) as session:
            rows = session.query(dax)
        return [next(iter(r.values()), None) for r in rows]

    def describe(self) -> str:
        """A short, human label for an instance picker: table count + first few real table names."""
        if not self.dll:
            return f"端口 {self.port}（未找到 ADOMD DLL）"
        with _LiveSession(self.port, self.dll) as s:
            tables = s.query("SELECT [Name] FROM $SYSTEM.TMSCHEMA_TABLES")
        names = [t["Name"] for t in tables if not _AUTO_DATE.match(t["Name"])]
        head = ", ".join(names[:6]) + ("…" if len(names) > 6 else "")
        return f"{len(names)} 表: {head}"
