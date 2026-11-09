import threading
import time

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


def test_continuous_output_renews_idle_deadline():
    class Provider:
        generation_timeout = 2
        idle_timeout = 0.15

        def stream(self, *args, **kwargs):
            for _ in range(5):
                yield '正文'
                time.sleep(0.05)

    events = list(completion_events(Provider(), '', [], heartbeat=0.01))
    assert ''.join(e['text'] for e in events if e['type'] == 'delta') == '正文' * 5


def test_stalled_stream_times_out_even_with_local_heartbeats_and_closes_late_stream():
    resume, closed = threading.Event(), threading.Event()

    class Provider:
        def stream(self, *args, **kwargs):
            try:
                yield '已收到的文字'
                resume.wait(2)
                yield '不应显示的迟到文字'
            finally:
                closed.set()

    events = []
    try:
        with pytest.raises(HTTPException) as failure:
            for event in completion_events(Provider(), '', [], timeout=1, idle_timeout=0.08, heartbeat=0.01):
                events.append(event)
        assert '未返回新数据' in failure.value.detail
        assert '已保留' in failure.value.detail
        assert sum(e['type'] == 'heartbeat' for e in events) >= 2
        assert [e['text'] for e in events if e['type'] == 'delta'] == ['已收到的文字']
        assert any('继续等待' in e.get('text', '') for e in events)
    finally:
        resume.set()
    assert closed.wait(2)


def test_reasoning_activity_renews_deadline_without_becoming_body_text():
    class Provider:
        def stream_events(self, *args, **kwargs):
            for _ in range(6):
                yield {'type': 'activity'}
                time.sleep(0.03)
            yield {'type': 'delta', 'text': '最终正文'}

    events = list(completion_events(Provider(), '', [], timeout=2, idle_timeout=0.15, heartbeat=0.01))
    assert [e['text'] for e in events if e['type'] == 'delta'] == ['最终正文']
    assert not any(e['type'] == 'activity' for e in events)
    assert sum(e['type'] == 'heartbeat' for e in events) >= 3


def test_stall_warning_recovers_without_replacing_previous_output():
    resume = threading.Event()

    class Provider:
        def stream(self, *args, **kwargs):
            yield '前一段'
            resume.wait(2)
            yield '后一段'

    events = []
    try:
        for event in completion_events(Provider(), '', [], timeout=2, idle_timeout=0.3, heartbeat=0.01):
            events.append(event)
            if '继续等待' in event.get('text', ''):
                resume.set()
    finally:
        resume.set()
    assert [e['text'] for e in events if e['type'] == 'delta'] == ['前一段', '后一段']
    assert any('已恢复响应' in e.get('text', '') for e in events)


def test_total_deadline_still_limits_a_continuously_active_stream():
    closed = threading.Event()

    class Provider:
        def stream(self, *args, **kwargs):
            try:
                while True:
                    yield '正文'
                    time.sleep(0.01)
            finally:
                closed.set()

    with pytest.raises(HTTPException) as failure:
        list(completion_events(Provider(), '', [], timeout=0.1, idle_timeout=1, heartbeat=0.01))
    assert '总时限' in failure.value.detail
    assert closed.wait(2)


def test_first_response_is_bounded_by_idle_deadline_too():
    resume = threading.Event()

    class Provider:
        def stream(self, *args, **kwargs):
            resume.wait(2)
            yield '迟到的首段'

    try:
        with pytest.raises(HTTPException) as failure:
            list(completion_events(Provider(), '', [], timeout=1, idle_timeout=0.05, heartbeat=0.01))
        assert '未返回新数据' in failure.value.detail
    finally:
        resume.set()
