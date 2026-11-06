"""Local Desktop adapter, reusing the reference project's live model reader."""
import hashlib
import json
import threading
from dataclasses import asdict

from fastapi import HTTPException

from .database import encode, now, uid
from .engine.context.base import Column, Measure, ModelContext, Relationship
from .engine.context.live_source import LiveDesktopSource, find_instances
from .reports import reports_for_instances


def context_from_dict(raw):
    return ModelContext(
        tables={n: [Column(**c) for c in cols] for n, cols in raw["tables"].items()},
        measures=[Measure(**m) for m in raw.get("measures", [])],
        relationships=[Relationship(**r) for r in raw.get("relationships", [])],
        **{k: raw.get(k, [] if k == "date_tables" else {}) for k in (
            "date_tables", "calculated_tables", "table_queries", "shared_expressions", "query_folders")})


def fingerprint(ctx):
    return hashlib.sha256(json.dumps(asdict(ctx), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class PowerBIService:
    def __init__(self, db):
        self.db = db
        self.lock = threading.RLock()
        self.live_ids = set()

    def get(self, model_id):
        row = self.db.one("SELECT * FROM models WHERE id=?", (model_id,))
        if not row:
            raise HTTPException(404, "模型不存在")
        row["context"] = json.loads(row["context"])
        row["live"] = model_id in self.live_ids
        report=self.db.one('SELECT payload FROM model_reports WHERE model_id=?',(model_id,))
        row['report']=json.loads(report['payload']) if report else {}
        if getattr(self,'semantics',None):row['semantics']=self.semantics.summary(model_id,row['fingerprint'])
        return row

    def save(self, name, source, port, ctx):
        model_id = uid()
        self.db.execute("INSERT INTO models VALUES (?,?,?,?,?,?,?)", (
            model_id, name, source, port, fingerprint(ctx), encode(asdict(ctx)), now()))
        if source == "desktop":
            # Reconnecting the same unchanged engine also restores conversations tied to its
            # earlier snapshots. A newly created snapshot ID must not disable their evaluation.
            matches=self.db.all("SELECT id FROM models WHERE source='desktop' AND port=? AND fingerprint=?",
                                (port,fingerprint(ctx)))
            self.live_ids.update(row['id'] for row in matches)
        return self.get(model_id)

    def connect(self, port, report_name=''):
        instances=find_instances()
        instance=next((i for i in instances if i.port==port),None)
        if not instance:
            raise HTTPException(409, "该端口不是当前 Power BI Desktop 实例，请重新扫描")
        try:
            with self.lock:
                ctx = LiveDesktopSource(port).load()
            report=reports_for_instances([instance]).get(getattr(instance,'pid',None),{})
            if not report.get('name') and report_name.strip():
                report={'name':report_name.strip(),'path':'','source':'manual'}
            model=self.save(report.get('name') or f"Desktop · {port}", "desktop", port, ctx)
            if report:
                self.db.execute('INSERT OR REPLACE INTO model_reports VALUES (?,?,?)',
                                (model['id'],encode(report),now()))
            return self.get(model['id'])
        except Exception as exc:
            raise HTTPException(422, f"读取模型失败：{str(exc).splitlines()[0][:240]}") from exc

    def require_live(self, model_id, *, unchanged=False):
        model = self.get(model_id)
        if model["source"] != "desktop":
            raise HTTPException(409, "请重新连接本机 Desktop 后实跑，模型快照不能执行代码")
        if model["port"] not in {i.port for i in find_instances()}:
            self.live_ids.discard(model_id)
            raise HTTPException(409, "Desktop 连接已断开，请重新扫描连接")
        try:
            current = LiveDesktopSource(model["port"]).load()
        except Exception as exc:
            self.live_ids.discard(model_id)
            raise HTTPException(409, '无法读取当前 Desktop 模型，请重新连接：'+str(exc).splitlines()[0][:180]) from exc
        # Persisted snapshots are restored only after re-reading the actual engine and matching
        # its schema. The port alone is insufficient: Desktop may reuse a different model's port.
        if (unchanged or not model['live']) and fingerprint(current) != model["fingerprint"]:
            self.live_ids.discard(model_id)
            raise HTTPException(409, "模型结构已变化，请同步模型并开启新对话")
        self.live_ids.add(model_id)
        model['live']=True
        return model, current

    def update_after_write(self, model_id, port):
        ctx = LiveDesktopSource(port).load()
        self.db.execute("UPDATE models SET context=?,fingerprint=? WHERE id=?",
                         (encode(asdict(ctx)), fingerprint(ctx), model_id))

    def demo(self):
        row = self.db.one("SELECT id FROM models WHERE source='demo' LIMIT 1")
        if row:
            return self.get(row["id"])
        tables = {
            "销售明细": [("订单日期", "datetime"), ("产品ID", "int64"), ("客户ID", "int64"),
                       ("销售额", "decimal"), ("数量", "int64"), ("成本", "decimal")],
            "日期": [("日期", "datetime"), ("年份", "int64"), ("月份", "int64")],
            "产品": [("产品ID", "int64"), ("产品名称", "string"), ("类别", "string")],
            "客户": [("客户ID", "int64"), ("客户名称", "string"), ("地区", "string")],
        }
        ctx = ModelContext(
            tables={t: [Column(n, d, t) for n, d in cols] for t, cols in tables.items()},
            measures=[Measure("总销售额", "销售明细", "SUM('销售明细'[销售额])")],
            relationships=[Relationship("销售明细", fc, t, tc) for fc, t, tc in (
                ("订单日期", "日期", "日期"), ("产品ID", "产品", "产品ID"), ("客户ID", "客户", "客户ID"))],
            date_tables=["日期"],
            table_queries={"销售明细": 'let\n    Source = #table({"销售额", "数量"}, {{100, 2}, {200, 4}})\nin\n    Source'})
        return self.save("零售分析 · 示例模型", "demo", None, ctx)
