"""Shared validation pipeline, usable on the server and on a colleague's computer."""
import copy
import math
import re
import datetime
from dataclasses import asdict

from .engine.context.base import Measure
from .engine.dax.artifact import DaxMeasureArtifact, DaxCalculatedColumnArtifact
from .engine.dax.generate import parse_dax_objects
from .engine.dax.live_eval import is_table_expression, validate_measure_set
from .engine.mquery.artifact import MScriptArtifact
from .engine.mquery.generate import parse_m_blocks


def candidates(text, mode, query_name=""):
    if mode == "mquery":
        if not re.search(r"```(?:powerquery|power-query|pq|mscript|m)\s*\n", text, re.I):
            return []
        return [{"name": query_name, "expression": code, "kind": "m_script"}
                for code in parse_m_blocks(text)]
    objects=parse_dax_objects(text)
    for i,obj in enumerate(objects):
        obj.setdefault('kind','dax_table' if is_table_expression(obj['expression']) else 'dax_measure')
        if not obj['name']:obj['name']=f'候选度量_{i+1}'
    return objects


def validate(ctx, objects, home_table, evaluator=None, points=None):
    context = copy.deepcopy(ctx)
    for obj in objects:
        if obj["kind"] == "dax_measure":
            context.measures.append(Measure(obj["name"], home_table, obj["expression"]))
    results = []
    for obj in objects:
        cls = MScriptArtifact if obj["kind"] == "m_script" else DaxMeasureArtifact
        artifact=(DaxCalculatedColumnArtifact(obj['expression'],obj['name'],obj.get('home_table',''))
                  if obj['kind']=='dax_column' else cls(obj['expression'],obj['name']))
        vr = asdict(artifact.validate(context))
        vr["calibration"] = []
        if not evaluator and vr["ok"]:
            vr["warnings"].append("仅通过静态检查，未经 Desktop 实跑验证")
        results.append(vr)
    if evaluator:
        indices = [i for i, o in enumerate(objects) if o["kind"] == "dax_measure" and results[i]["ok"]]
        defs = [(objects[i]["name"], objects[i]["expression"]) for i in indices]
        live = validate_measure_set(evaluator, home_table, defs) if defs else []
        for i, vr in zip(indices, live):
            results[i].update(asdict(vr), warnings=results[i]["warnings"] + vr.warnings)
        for i, obj in enumerate(objects):
            if obj["kind"] == "dax_table" and results[i]["ok"]:
                results[i].update(asdict(evaluator.evaluate_table_expr(obj["expression"])))
            if obj['kind']=='dax_column' and results[i]['ok']:
                static_warnings=results[i]['warnings']
                live=evaluator.evaluate_column_expr(obj['expression'],obj['home_table'])
                results[i].update(asdict(live),warnings=static_warnings+live.warnings)
                results[i]['verification_scope']='row_sample'
            # A response can include supporting measures; known results refer to the final measure.
            if obj["kind"] != "dax_measure" or not results[i]["ok"] or (indices and i != indices[-1]):
                continue
            for point in points or []:
                filters = [(f["table"], f["column"], f["value"]) for f in point["filters"]]
                valid_filter = all(ctx.has_column(t, c) for t, c, _ in filters)
                if not valid_filter:
                    results[i]["ok"] = False
                    results[i]["errors"].append("校准筛选字段不在模型中")
                    continue
                try:
                    filters = [(t, c, datetime.datetime.fromisoformat(v) if isinstance(v, str)
                                and any(col.name == c and col.dtype == "datetime" for col in ctx.tables[t]) else v)
                               for t, c, v in filters]
                except ValueError:
                    results[i]["ok"] = False
                    results[i]["errors"].append("日期筛选值请使用 ISO 格式，例如 2025-01-01")
                    continue
                vr = evaluator.evaluate_with_defines_at_slice(home_table, defs, obj["name"], filters)
                value = vr.sample
                numeric = isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
                matched = bool(vr.ok and numeric and abs(value - point["expected"]) <= point["tolerance"])
                results[i]["calibration"].append({**point, "actual": value, "matched": matched,
                                                    "errors": vr.errors})
                if not matched:
                    results[i]["ok"] = False
                    results[i]["errors"].append(f"校准不匹配：期望 {point['expected']}，实际 {value}")
    return results
