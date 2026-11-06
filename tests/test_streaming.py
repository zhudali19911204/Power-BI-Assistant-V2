import threading

import pytest
from fastapi import HTTPException

from backend.streaming import completion_events


def test_heartbeat_precedes_slow_first_token():
    ready = threading.Event()
    class Provider:
        def stream(self,*a,**k):
            ready.wait(1)
            yield '正文'
    events=completion_events(Provider(),'test',[],heartbeat=0.01,timeout=2)
    try:
        assert next(events)['type']=='heartbeat'
        ready.set()
        assert list(events)==[{'type':'delta','text':'正文'}]
    finally:
        ready.set()
        events.close()


def test_generation_has_total_deadline_even_if_server_sends_no_answer():
    ready=threading.Event()
    class Provider:
        def stream(self,*a,**k):
            ready.wait(1)
            yield 'too late'
    try:
        with pytest.raises(HTTPException) as failure:
            list(completion_events(Provider(),'test',[],heartbeat=0.01,timeout=0.03))
        assert failure.value.status_code==504
    finally:
        ready.set()


def test_provider_activity_can_update_ui_without_revealing_reasoning():
    class Provider:
        def stream_events(self,*a,**k):
            yield {'type':'status','text':'模型正在分析，等待正文输出'}
            yield {'type':'delta','text':'结果'}
    assert list(completion_events(Provider(),'test',[]))==[
        {'type':'status','text':'模型正在分析，等待正文输出'}, {'type':'delta','text':'结果'}]
