"""
Write a measure or a calculated table back into the open Power BI Desktop model via TOM.

This is the write half of the live connection (read is `LiveDesktopSource`). It uses the Tabular Object
Model to add/replace a measure on a table, or add/replace a calculated table (e.g. a date table) — exactly
how external tools like Tabular Editor write to Desktop. Changes land in the *running* model; the user
still presses Ctrl+S in Desktop to persist to the .pbix. Only ever called on DAX that passed validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .amo import AMO_DLLS, ensure_amo_dlls

_tom_asm: Any = None  # the loaded Tabular assembly, cached


@dataclass
class WriteResult:
    ok: bool
    detail: str = ""


def _tom() -> Any:
    global _tom_asm
    if _tom_asm is None:
        import clr  # type: ignore[import-untyped]  # noqa: F401
        from System.Reflection import Assembly  # type: ignore[import-not-found]

        amo = ensure_amo_dlls()
        for name in AMO_DLLS:  # load in dependency order
            Assembly.LoadFrom(str(amo / name))
        _tom_asm = Assembly.LoadFrom(str(amo / "Microsoft.AnalysisServices.Tabular.dll"))
    return _tom_asm


def _t(name: str) -> Any:
    typ = _tom().GetType(f"Microsoft.AnalysisServices.Tabular.{name}")
    if typ is None:
        raise RuntimeError(f"无法在 TOM 程序集中找到类型 {name}")
    return typ


class LiveDesktopWriter:
    """Writes measures / calculated tables into the open Desktop model on `port` (TOM)."""

    def __init__(self, port: int) -> None:
        self.port = port

    def _connect(self) -> Any:
        import System  # type: ignore[import-not-found]

        server = System.Activator.CreateInstance(_t("Server"))
        server.Connect(f"Data Source=localhost:{self.port}")
        return server

    def write_measure(
        self, table: str, name: str, expression: str, *, overwrite: bool = True
    ) -> WriteResult:
        """Add or replace `table`[`name`] = `expression` in the live model, then SaveChanges()."""
        try:
            import System  # type: ignore[import-not-found]

            server = self._connect()
        except Exception as e:  # noqa: BLE001 — DLL download/load/connect failure
            return WriteResult(False, f"连接/加载 TOM 失败：{type(e).__name__}: {e}")
        try:
            model = server.Databases[0].Model
            target = next((t for t in model.Tables if t.Name == table), None)
            if target is None:
                return WriteResult(False, f"目标表 '{table}' 不存在")
            existing = next((m for m in target.Measures if m.Name == name), None)
            if existing is not None:
                if not overwrite:
                    return WriteResult(False, f"度量值 '{name}' 已存在")
                existing.Expression = expression
                action = "已更新"
            else:
                measure = System.Activator.CreateInstance(_t("Measure"))
                measure.Name = name
                measure.Expression = expression
                target.Measures.Add(measure)
                action = "已写入"
            model.SaveChanges()
            return WriteResult(True, f"{action}度量值 '{table}'[{name}]（请在 Desktop 中按 Ctrl+S 保存）")
        except Exception as e:  # noqa: BLE001
            return WriteResult(False, f"写入失败：{type(e).__name__}: {str(e).splitlines()[0][:200]}")
        finally:
            _disconnect(server)

    def write_calculated_table(self, name: str, expression: str, *, overwrite: bool = True) -> WriteResult:
        """Add or replace a calculated table `name = expression` (e.g. a date table); engine infers columns."""
        try:
            import System  # type: ignore[import-not-found]

            server = self._connect()
        except Exception as e:  # noqa: BLE001
            return WriteResult(False, f"连接/加载 TOM 失败：{type(e).__name__}: {e}")
        try:
            model = server.Databases[0].Model
            existing = next((t for t in model.Tables if t.Name == name), None)
            if existing is not None:
                if not overwrite:
                    return WriteResult(False, f"表 '{name}' 已存在")
                model.Tables.Remove(existing)
                action = "已替换"
            else:
                action = "已创建"
            table = System.Activator.CreateInstance(_t("Table"))
            table.Name = name
            partition = System.Activator.CreateInstance(_t("Partition"))
            partition.Name = name
            source = System.Activator.CreateInstance(_t("CalculatedPartitionSource"))
            source.Expression = expression
            partition.Source = source
            table.Partitions.Add(partition)
            model.Tables.Add(table)
            model.SaveChanges()
            return WriteResult(True, f"{action}计算表 '{name}'（请在 Desktop 中按 Ctrl+S 保存）")
        except Exception as e:  # noqa: BLE001
            return WriteResult(False, f"写入失败：{type(e).__name__}: {str(e).splitlines()[0][:200]}")
        finally:
            _disconnect(server)


    # NOTE: there is deliberately no `write_m_partition`. Writing Power Query (M) into an OPEN Power BI
    # Desktop model via this external connection is unsupported and crashes Desktop's Mashup query
    # navigator (verified — NullReferenceException in `Microsoft.Mashup.Client.UI...QueriesNavigatorModelBase
    # .IsQueryGroupNode`). Measures and calculated tables above ARE writable (pure model objects, no Mashup
    # UI), but M partitions are not. The M assistant therefore offers the verified M for the user to paste
    # into Power Query's Advanced Editor instead of writing it back. See memory: mquery-refresh-verification.


def _disconnect(server: Any) -> None:
    try:
        server.Disconnect()
    except Exception:  # noqa: BLE001
        pass
