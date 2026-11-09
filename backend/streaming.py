"""Keep the HTTP stream responsive while a synchronous model client waits for output."""
import queue
import threading
import time

from fastapi import HTTPException

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
        threading.Thread(target=self._run, daemon=True, name='background-generation').start()

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
    def run():
        try:output.put(('result',work()))
        except Exception as exc:output.put(('error',exc))
    threading.Thread(target=run,daemon=True,name='analysis-query').start()
    started=time.monotonic()
    while True:
        remaining=timeout-(time.monotonic()-started)
        if remaining<=0:raise HTTPException(504,'等待本机模型查询超时，请缩小数据范围')
        try:kind,value=output.get(timeout=min(heartbeat,remaining))
        except queue.Empty:
            yield {'type':'heartbeat','elapsed':int(time.monotonic()-started)}
            continue
        if kind=='error':raise value
        yield {'type':'result','result':value}
        break


def completion_events(provider, system, messages, *, timeout=None, idle_timeout=None, heartbeat=None, **opts):
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
            event_stream = getattr(provider, 'stream_events', None)
            stream = event_stream(system, messages, **opts) if callable(event_stream) else provider.stream(system, messages, **opts)
            for item in stream:
                if stopped.is_set():
                    break
                if isinstance(item, dict):
                    publish(item['type'], item.get('text'))
                elif item:
                    publish('delta', item)
            publish('finished')
        except Exception as exc:
            publish('error', exc)
        finally:
            if hasattr(stream, 'close'):
                stream.close()

    threading.Thread(target=generate, daemon=True, name='model-stream').start()
    try:
        while True:
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
                kind, value, received_at = output.get(timeout=min(heartbeat, timeout-elapsed, idle_timeout-idle))
            except queue.Empty:
                continue
            if kind == 'finished':
                break
            if kind == 'error':
                raise value
            # Only upstream activity renews this deadline. Our own keep-alives cannot.
            if kind in ('delta', 'activity', 'status'):
                last_activity = received_at
                if warned:
                    warned = False
                    yield {'type': 'status', 'text': '模型已恢复响应，正在继续生成'}
            if kind == 'activity':
                continue
            yield {'type': kind, 'text': value}
    finally:
        stopped.set()
