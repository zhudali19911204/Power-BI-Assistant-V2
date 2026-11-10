"""Keep the HTTP stream responsive while a synchronous model client waits for output."""
import queue
import threading
import time
import logging
from contextvars import copy_context

from fastapi import HTTPException
from .engine.llm.diagnostics import DEFAULT_MAX_TOKENS, GenerationReport
from .cancellation import current_cancellation, check_cancelled, close_on_cancel

logger = logging.getLogger(__name__)

GENERATION_TIMEOUT = 900
GENERATION_IDLE_TIMEOUT = 90
HEARTBEAT_INTERVAL = 2


def generation_timeout(provider):
    return getattr(provider, 'generation_timeout', GENERATION_TIMEOUT)


class BackgroundStream:
    """Consume a generation independently of its HTTP reader; snapshots stay in SQLite."""

    def __init__(self, source, *, buffer_size=256):
        self.source = source
        self.output = queue.Queue(maxsize=buffer_size)
        self.detached = threading.Event()
        self.finished = threading.Event()
        self.overflow = threading.Event()
        context = copy_context()
        threading.Thread(target=lambda: context.run(self._run), daemon=True, name='background-generation').start()

    def _run(self):
        try:
            for line in self.source:
                if self.detached.is_set():
                    continue
                try:
                    self.output.put_nowait(line)
                except queue.Full:
                    # A slow/closed viewer must never hold up the actual generation.
                    self.overflow.set()
                    self.detached.set()
        finally:
            self.finished.set()

    def events(self):
        from .database import encode

        try:
            while True:
                if self.overflow.is_set():
                    yield encode({'type': 'background', 'text': '生成仍在后台继续，正在恢复会话进度'}) + '\n'
                    return
                try:
                    yield self.output.get(timeout=0.2)
                except queue.Empty:
                    if self.finished.is_set():
                        return
        finally:
            # This only stops publishing to this reader, not consuming the source.
            self.detached.set()


def task_events(work,*,timeout=100,heartbeat=HEARTBEAT_INTERVAL):
    """Send keep-alives while bounded, read-only engine work runs in a worker thread."""
    output=queue.Queue(maxsize=1)
    context=copy_context()
    token=current_cancellation()
    def run():
        try:
            check_cancelled()
            output.put(('result',work()))
        except Exception as exc:output.put(('error',exc))
    check_cancelled()
    threading.Thread(target=lambda:context.run(run),daemon=True,name='analysis-query').start()
    started=last_heartbeat=time.monotonic()
    while True:
        check_cancelled()
        remaining=timeout-(time.monotonic()-started)
        if remaining<=0:raise HTTPException(504,'等待本机模型查询超时，请缩小数据范围')
        try:kind,value=output.get(timeout=min(0.1 if token else heartbeat,remaining))
        except queue.Empty:
            if time.monotonic()-last_heartbeat>=heartbeat:
                last_heartbeat=time.monotonic()
                yield {'type':'heartbeat','elapsed':int(time.monotonic()-started)}
            continue
        check_cancelled()
        if kind=='error':raise value
        yield {'type':'result','result':value}
        break


def completion_events(provider, system, messages, *, timeout=None, idle_timeout=None, heartbeat=None,
                      report=None, on_report=None, **opts):
    timeout = generation_timeout(provider) if timeout is None else timeout
    idle_timeout = getattr(provider, 'idle_timeout', GENERATION_IDLE_TIMEOUT) if idle_timeout is None else idle_timeout
    heartbeat = HEARTBEAT_INTERVAL if heartbeat is None else heartbeat
    output = queue.Queue(maxsize=64)
    stopped = threading.Event()
    started = time.monotonic()
    last_heartbeat = started
    last_activity = started
    warned = False
    stall_after = min(30, idle_timeout / 2)
    opts.setdefault('max_tokens', getattr(provider, 'max_tokens', DEFAULT_MAX_TOKENS))
    report = report if report is not None else GenerationReport()
    report.max_tokens = opts['max_tokens']
    token = current_cancellation()
    context = copy_context()
    unregister = lambda: None

    def publish(kind, value=None):
        received_at = time.monotonic()
        while not stopped.is_set():
            try:
                output.put((kind, value, received_at), timeout=0.1)
                return
            except queue.Full:
                pass

    def generate():
        stream = None
        try:
            check_cancelled()
            event_stream = getattr(provider, 'stream_events', None)
            stream = event_stream(system, messages, **opts) if callable(event_stream) else provider.stream(system, messages, **opts)
            for item in stream:
                if stopped.is_set():
                    break
                if isinstance(item, dict):
                    publish(item['type'], item)
                elif item:
                    publish('delta', {'type': 'delta', 'text': item})
            publish('finished')
        except Exception as exc:
            publish('error', exc)
        finally:
            if hasattr(stream, 'close'):
                stream.close()

    try:
        check_cancelled()
        client = getattr(provider, '_client', None)
        if client and callable(getattr(client, 'close', None)):
            unregister = close_on_cancel(client.close)
        threading.Thread(target=lambda:context.run(generate), daemon=True, name='model-stream').start()
        while True:
            check_cancelled()
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                raise HTTPException(504, f'已达到本次生成总时限（{int(timeout)} 秒），已保留收到的内容；可在模型服务设置中延长总时限后重试')
            if time.monotonic() - last_heartbeat >= heartbeat:
                last_heartbeat = time.monotonic()
                yield {'type': 'heartbeat', 'elapsed': int(last_heartbeat-started)}
                continue
            idle = time.monotonic() - last_activity
            if idle >= idle_timeout:
                raise HTTPException(504, f'模型连续 {int(idle_timeout)} 秒未返回新数据，已保留收到的内容；请检查模型服务或在模型服务设置中调整等待时间后重试')
            if not warned and idle >= stall_after:
                warned = True
                yield {'type': 'status', 'text': f'模型暂未返回新数据，继续等待（连续 {int(idle)} 秒）'}
            try:
                kind, value, received_at = output.get(timeout=min(0.1 if token else heartbeat, timeout-elapsed, idle_timeout-idle))
            except queue.Empty:
                continue
            check_cancelled()
            if kind == 'finished':
                report.stream_ended = True
                break
            if kind == 'error':
                raise value
            if kind == 'metadata':
                report.update(value)
                continue
            # Only upstream activity renews this deadline. Our own keep-alives cannot.
            if kind in ('delta', 'activity', 'status'):
                last_activity = received_at
                if warned:
                    warned = False
                    yield {'type': 'status', 'text': '模型已恢复响应，正在继续生成'}
            if kind == 'activity':
                continue
            if kind == 'delta':
                report.output_chars += len(value.get('text', ''))
            yield value
    except BaseException as exc:
        report.error_type = type(exc).__name__
        raise
    finally:
        stopped.set()
        unregister()
        report.seconds = round(time.monotonic() - started, 3)
        if on_report:
            try:
                on_report(report)
            except Exception:
                # A diagnostics failure must not replace an answer or its original error.
                logger.warning('Unable to persist LLM diagnostics', exc_info=False)
