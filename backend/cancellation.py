"""A cancellation signal belongs to one generation, independently of HTTP viewers."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading

from fastapi import HTTPException

CANCELLED_TEXT = '已中止本次生成，已保留收到的文字和查询结果'
_current = ContextVar('generation_cancellation', default=None)


class GenerationCancelled(HTTPException):
    def __init__(self):
        super().__init__(499, CANCELLED_TEXT)


def _close_async(callback):
    def run():
        try: callback()
        except Exception: pass  # Closing an already closed transport is harmless.
    threading.Thread(target=run, daemon=True, name='cancel-generation-transport').start()


class CancellationToken:
    def __init__(self, generation_id):
        self.id = generation_id
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.callbacks = {}

    def check(self):
        if self.event.is_set(): raise GenerationCancelled()

    def cancel(self):
        with self.lock:
            if self.event.is_set(): return
            self.event.set()
            callbacks = list(self.callbacks.values())
            self.callbacks.clear()
        for callback in callbacks: _close_async(callback)

    def register(self, callback):
        key = object()
        with self.lock:
            cancelled = self.event.is_set()
            if not cancelled: self.callbacks[key] = callback
        if cancelled:
            _close_async(callback)
            self.check()
        def unregister():
            with self.lock: self.callbacks.pop(key, None)
        return unregister


def current_cancellation():
    return _current.get()


def check_cancelled():
    token = current_cancellation()
    if token: token.check()


def close_on_cancel(callback):
    token = current_cancellation()
    return token.register(callback) if token else lambda: None


def close_cancelled_provider(provider):
    client = getattr(provider, '_client', None)
    close = getattr(client, 'close', None)
    if callable(close): _close_async(close)


@contextmanager
def cancellation_scope(token):
    marker = _current.set(token)
    try: yield
    finally: _current.reset(marker)
