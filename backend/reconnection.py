"""Reconnect only to a uniquely identified report with the saved schema."""
import logging
import threading
import time
from dataclasses import asdict

from fastapi import HTTPException

from .cancellation import GenerationCancelled, check_cancelled, current_cancellation
from .database import encode, now
from .reports import same_report, normalize_name

logger = logging.getLogger(__name__)


def transport_disconnected(exc):
    # Query timeouts and DAX errors must not silently rerun an expensive query.
    return (isinstance(exc, ConnectionError) or type(exc).__name__ in {
        'AdomdConnectionException', 'SocketException', 'IOException',
    })


def connection_schema(context):
    # Remove only Column.cardinality hints; a real table/field named "cardinality"
    # is still part of the schema and must never disappear from verification.
    context={**context,'tables':{name:[{k:v for k,v in column.items() if k!='cardinality'}
                                      for column in columns] for name,columns in context.get('tables',{}).items()}}
    def ordered(value):
        if isinstance(value,dict):return {k:ordered(v) for k,v in sorted(value.items())}
        if isinstance(value,list):return sorted((ordered(v) for v in value),key=encode)
        return value
    return encode(ordered(context))


class ConnectionIssue(HTTPException):
    def __init__(self, state, detail):
        super().__init__(409, detail)
        self.state = state


class DesktopReconnector:
    delays = (0, .3, .7)
    healthy_interval = 20
    retry_intervals = (2, 5, 10, 20, 30)

    def __init__(self, service):
        self.service = service
        self.states = {}
        self.due = {}
        self.failures = {}
        self.jobs = set()
        self.lock = threading.Lock()

    def state(self, model_id, live=False):
        with self.lock:
            return dict(self.states.get(model_id, {
                'status': 'connected' if live else 'checking',
                'detail': 'Desktop 已连接' if live else '正在检测并恢复 Desktop 连接',
                'checked_at': None, 'retry_in': 0,
            }))

    def update(self, model_id, status, detail, *, retry_in=0):
        with self.lock:
            previous=self.states.get(model_id,{}).get('status')
            self.states[model_id] = {'status': status, 'detail': detail,
                                    'checked_at': now(), 'retry_in': retry_in}
            self.due[model_id] = time.monotonic() + retry_in
        if previous!=status:logger.info('Desktop %s connection %s -> %s',model_id,previous,status)

    def healthy(self, model_id):
        self.failures.pop(model_id, None)
        self.update(model_id, 'connected', 'Desktop 已连接，已核对报表与模型结构',
                    retry_in=self.healthy_interval)

    def lost(self, model_id):
        self.service.live_ids.discard(model_id)
        self.update(model_id,'reconnecting','Desktop 查询连接中断，正在自动重连')

    def wait(self, delay):
        check_cancelled()
        token = current_cancellation()
        if token: token.event.wait(delay)
        else: time.sleep(delay)
        check_cancelled()

    @staticmethod
    def target(model, instances, reports):
        expected = model.get('report', {})
        original = next((i for i in instances if i.port == model['port']), None)
        # A persisted engine PID on the same port identifies the original process. A new
        # engine (even on a reused port) must go through report identity matching.
        if original and expected.get('engine_pid') == getattr(original, 'pid', None) and expected.get('engine_pid'):
            return original, expected
        if expected.get('name'):
            matches = [(i, reports.get(getattr(i, 'pid', None), {})) for i in instances
                       if same_report(expected, reports.get(getattr(i, 'pid', None), {}))]
            if len(matches) > 1:
                raise ConnectionIssue('ambiguous', '发现多个同名或同路径报表，请手动选择要连接的 Desktop 实例')
            if matches:
                instance, report=matches[0]
                if expected.get('path') and not report.get('path'):
                    same_names=[i for i in instances if normalize_name(reports.get(getattr(i,'pid',None),{}).get('name'))==normalize_name(expected['name'])]
                    if len(same_names)>1:
                        raise ConnectionIssue('ambiguous','报表路径暂不可用且存在多个同名报表，请手动选择实例')
                return instance, report
            # Window enumeration can fail transiently. Never infer a new report by port.
            raise ConnectionIssue('disconnected', '原报表暂不可识别，正在等待 Desktop 恢复；也可手动重新扫描连接')
        # Legacy snapshots have no report identity. Retain the old same-port + schema
        # verification, but never jump to another port solely because its schema matches.
        if original and not expected.get('engine_pid'):
            return original, reports.get(getattr(original, 'pid', None), {})
        raise ConnectionIssue('disconnected', 'Desktop 连接已断开，正在等待原报表；未保存报表身份时请手动重新扫描连接')

    def require_live(self, model_id, discover, identify, load):
        service = self.service
        with service.lock:
            model = service.get(model_id)
            if model['source'] != 'desktop':
                raise HTTPException(409, '请重新连接本机 Desktop 后实跑，模型快照不能执行代码')
            issue = None
            for attempt, delay in enumerate(self.delays):
                self.wait(delay)
                try:
                    instances = discover()
                    original = next((i for i in instances if i.port == model['port']), None)
                    known_engine = (original and model.get('report', {}).get('engine_pid')
                                    and model['report']['engine_pid'] == getattr(original, 'pid', None))
                    reports = {} if known_engine else identify(instances)
                    instance, report = self.target(model, instances, reports)
                    current = load(instance.port)
                    check_cancelled()
                    if connection_schema(asdict(current)) != connection_schema(model['context']):
                        raise ConnectionIssue('changed', '模型结构已变化，请点击右侧“同步模型”，同步后可在当前对话继续')
                    self.bind(model, instance, report)
                    self.healthy(model_id)
                    return service.get(model_id), current
                except GenerationCancelled: raise
                except ConnectionIssue as exc:
                    issue = exc
                    if exc.state == 'ambiguous': break
                except Exception as exc:
                    logger.warning('Desktop %s reconnect attempt=%s error=%s', model_id, attempt+1, type(exc).__name__)
                    issue = ConnectionIssue('reconnecting', '暂时无法读取 Desktop 模型，正在自动重连')
                service.live_ids.discard(model_id)
                self.update(model_id, 'reconnecting', '连接检查未通过，正在重新扫描并核对原报表')
            service.live_ids.discard(model_id)
            failures = self.failures.get(model_id, 0) + 1
            self.failures[model_id] = failures
            retry = self.retry_intervals[min(failures-1, len(self.retry_intervals)-1)]
            self.update(model_id, issue.state, issue.detail, retry_in=retry)
            raise issue

    def bind(self, model, instance, report):
        service = self.service
        previous = model.get('report', {})
        report = {**previous, **report, 'engine_pid': getattr(instance, 'pid', None), 'engine_port': instance.port}
        if not report.get('path') and previous.get('path'):report['path']=previous['path']
        changed_engine = (instance.port != model['port'] or previous.get('engine_pid') != report['engine_pid'])
        with service.db.connection() as db:
            if instance.port != model['port']:
                db.execute('UPDATE models SET port=? WHERE id=?', (instance.port, model['id']))
            if report != previous:
                db.execute('INSERT OR REPLACE INTO model_reports VALUES (?,?,?)', (model['id'], encode(report), now()))
            if changed_engine:
                db.execute('DELETE FROM field_value_profiles WHERE model_id=?', (model['id'],))
                db.execute('DELETE FROM model_value_lookups WHERE model_id=?', (model['id'],))
        service.live_ids.add(model['id'])
        if changed_engine:
            logger.info('Desktop %s rebound port=%s; semantic definitions preserved, value caches cleared', model['id'], instance.port)

    def poll(self, model_id):
        model = self.service.get(model_id)
        if model['source'] != 'desktop':
            return {'model_id': model_id, 'live': False, 'port': model['port'],
                    'connection': {'status': 'demo', 'detail': '示例模型', 'checked_at': None, 'retry_in': 0}}
        with self.lock:
            due = time.monotonic() >= self.due.get(model_id, 0)
            start = due and model_id not in self.jobs
            if start: self.jobs.add(model_id)
        if start:
            def work():
                try:
                    # A health check must not queue behind active Desktop reads or writes.
                    if not self.service.lock.acquire(blocking=False): return
                    try: self.service.require_live(model_id, unchanged=True)
                    except HTTPException: pass
                    finally: self.service.lock.release()
                finally:
                    with self.lock: self.jobs.discard(model_id)
            threading.Thread(target=work, daemon=True, name='desktop-reconnect').start()
        model = self.service.get(model_id)
        return {'model_id': model_id, 'live': model['live'], 'port': model['port'],
                'connection': self.state(model_id, model['live'])}
