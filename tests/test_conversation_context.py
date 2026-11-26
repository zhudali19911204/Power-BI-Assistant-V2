import json
import threading

import pytest

from backend.app import create_app
from backend.conversation_context import SUMMARY_CHARS, SUMMARY_HEADER
from backend.database import encode, now, uid
from tests.test_analysis import plan, result, setup
from tests.test_api import FakeProvider, app, client, conversation, send
from tests.test_images import upload


def add(app, cid, role, content, status='complete'):
    mid = uid()
    app.state.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)', (mid, cid, role, content, status, now()))
    return mid


def turns(app, cid, count):
    rows = []
    for i in range(count):
        rows += [add(app, cid, 'user', f'第 {i + 1} 轮需求'), add(app, cid, 'assistant', f'第 {i + 1} 轮回答')]
    return rows


def query(app, conv, mid, label, title, *, exception='', ok=True):
    qid = uid()
    dax = 'EVALUATE ROW("value", 987654321)'
    record = dict(result([{'value': 987654321}], ok=ok), id=qid, label=label, title=title, dax=dax, attempt=1)
    if exception:
        record['convention_exceptions'] = {'C1': {'title': '总部范围', 'reason': exception}}
    app.state.db.execute('INSERT INTO analysis_queries VALUES (?,?,?,?,?,?,?)',
                         (qid, mid, conv['model_id'], title, dax, encode(record), now()))
    return qid


def test_preferences_persist_per_conversation_and_default_for_legacy_rows(app, client, tmp_path):
    conv, model = conversation(client)
    cid = conv['id']
    other = client.post('/api/conversations', json={'mode': 'dax', 'model_id': model['id'],
                        'auto_compress': False}).json()['id']
    path = f'/api/conversations/{cid}/context'
    usage = client.get(path).json()
    assert (usage['percent'], usage['messages'], usage['automatic']) == (0, 0, True)
    assert not usage['can_compress'] and not client.post(path + '/compress', json={}).json()['compressed_now']
    assert client.get(f'/api/conversations/{other}/context').json()['automatic'] is False
    assert client.put(path, json={'automatic': 'false'}).status_code == 422
    assert client.put(path, json={'automatic': False}).json()['automatic'] is False
    assert not create_app(tmp_path).state.chat.history.window(cid).usage['automatic']
    assert client.put(path, json={'automatic': True}).json()['automatic'] is True
    assert client.delete(f'/api/conversations/{cid}').is_success
    assert client.get(path).status_code == client.post(path + '/compress', json={}).status_code == 404
    assert not app.state.db.one('SELECT 1 FROM conversation_contexts WHERE conversation_id=?', (cid,))
    assert not app.state.chat.history.window(other).usage['automatic']


@pytest.mark.parametrize('follow_up_label', ['尚未完成：', 'Still open: '])
def test_manual_compaction_preserves_originals_and_scopes_each_exception_to_its_query(setup, tmp_path, follow_up_label):
    app, client, conv = setup
    cid = conv['id']
    path = f'/api/conversations/{cid}'
    goal = "比较 '成本中心'[编号] 的 0257 和 0810，排除冲销。"
    old_image = upload(client)
    app.state.images.save_user(cid, goal, [old_image['id']])
    first = add(app, cid, 'assistant', '旧回复中的数字 987654321。\n' + follow_up_label + '按月复查。')
    query(app, conv, first, 'Q1', '第一次尝试', exception='修复前的无效例外', ok=False)
    q1 = query(app, conv, first, 'Q1', '本次比较', exception='仅本查询的分母取全部成本中心')
    confirmation = '口径确认 · 归属字段\n选择：成本中心\n采用口径：以实际编号为准\n用户补充：保留前导零'
    add(app, cid, 'user', confirmation)
    second = add(app, cid, 'assistant', '已记录。')
    q2 = query(app, conv, second, 'Q1', '另一个问题', exception='仅此问题排除退货')
    add(app, cid, 'assistant', '失败回复不可采用', 'error')
    raw = turns(app, cid, 2)
    before = client.get(path + '/messages').json()
    exported = client.get(path + '/export').text
    assert '987654321' in exported and 'data:image/png;base64,' in exported
    usage = client.post(path + '/context/compress', json={}).json()
    assert usage['compressed_now'] and usage['folded_messages'] == 4
    assert usage['messages'] == 5 and usage['percent'] == 31
    notes = usage['summary']
    text = encode(notes)
    assert goal in text and any(n['内容'] == confirmation for n in notes)
    assert any(n['类别'] == '历史图片引用' and n['内容'] == old_image['name'] for n in notes)
    assert '按月复查' in text and '987654321' not in text and '失败回复不可采用' not in text
    refs = [n for n in notes if n['类别'] == '历史查询引用']
    assert len(refs) == 2 and '修复前的无效例外' not in text
    for msg_id, qid, reason in [(first, q1, '分母取全部'), (second, q2, '排除退货')]:
        assert any(n['来源'] == msg_id and qid in n['内容'] and reason in n['内容'] for n in refs)
    restarted = create_app(tmp_path).state.chat.history.window(cid, send=True)
    assert restarted.notes == notes and [m['id'] for m in restarted.messages] == raw
    assert [m.content for m in restarted.quoted_users()] == [goal, confirmation]
    assert '分母取全部' not in encode(restarted.retrieval_history())
    assert not client.post(path + '/context/compress', json={}).json()['compressed_now']
    assert client.get(path + '/messages').json() == before and client.get(path + '/export').text == exported
    assert client.get(f'/api/analysis/{q1}/csv').status_code == 200
    for _ in range(9):
        add(app, cid, 'user', '继续')
        mid = add(app, cid, 'assistant', '另一轮')
        query(app, conv, mid, 'Q1', '普通查询')
        query(app, conv, mid, 'Q2', '另一普通查询')
    later = client.post(path + '/context/compress', json={}).json()
    assert later['folded_messages'] == 22
    assert all(item in encode(later['summary']) for item in [q1, q2, goal])
    assert sum(n['内容'] == goal for n in later['summary']) == 1


@pytest.mark.parametrize('mode', ['dax', 'mquery'])
def test_automatic_compaction_runs_on_send_and_recent_images_still_reach_provider(mode, app, client, monkeypatch):
    conv, _ = conversation(client, mode)
    cid = conv['id']
    rows = turns(app, cid, 7)
    img = upload(client)
    app.state.images.save_user(cid, '最近的图片', [img['id']])
    add(app, cid, 'assistant', '看过图片。')
    usage = client.get(f'/api/conversations/{cid}/context').json()
    assert usage['percent'] == 80 and usage['folded_messages'] == 0
    provider = FakeProvider(['可以继续。'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    events = send(client, cid, '请继续说明')
    start = next(e for e in events if e['type'] == 'start')
    assert events[-1]['type'] == 'done' and start['context_usage']['compressed_now']
    assert len(provider.calls) == 1
    messages = provider.calls[0][1]
    assert len(messages) == 6 and messages[0].content.startswith(SUMMARY_HEADER)
    assert '第 1 轮需求' in messages[0].content and '第 1 轮回答' not in messages[0].content
    assert [m.content for m in messages[1:]] == ['第 7 轮需求', '第 7 轮回答', '最近的图片', '看过图片。', '请继续说明']
    assert len(messages[-3].images) == 1 and not messages[0].images
    assert len(app.state.chat.messages(cid)) == len(rows) + 4


def test_analysis_compaction_passes_history_but_only_current_evidence_to_conclusion(setup, monkeypatch):
    app, client, conv = setup
    cid = conv['id']
    entries = turns(app, cid, 7)
    old = query(app, conv, entries[1], 'Q1', '旧数据', exception='仅该查询扩大分母')
    provider = FakeProvider([plan(), '本轮实查为 125。[Q1]'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a: result())
    events = send(client, cid, '请更新这个指标')
    start = next(e for e in events if e['type'] == 'start')
    assert events[-1]['type'] == 'done' and start['context_usage']['folded_messages'] == 10
    for _, messages in provider.calls:
        assert sum(m.content.startswith(SUMMARY_HEADER) for m in messages) == 1
        assert old in messages[0].content and '987654321' not in ''.join(m.content for m in messages)
        assert sum(m.content == '请更新这个指标' for m in messages) == 1
    evidence = json.loads(provider.calls[-1][1][-1].content.split('\n', 1)[1])['evidence']
    assert len(evidence) == 1 and '125' in encode(evidence[0]) and old not in encode(evidence)


def test_manual_mode_keeps_existing_limits_and_can_compact_after_overflow(app, client):
    conv, _ = conversation(client)
    cid = conv['id']
    path = f'/api/conversations/{cid}/context'
    client.put(path, json={'automatic': False})
    ids = turns(app, cid, 15)
    window = app.state.chat.history.window(cid, send=True)
    assert not window.notes and [m['id'] for m in window.messages] == ids[-20:]
    assert window.usage['omitted_messages'] == 10 and window.usage['percent'] == 100
    usage = client.post(path + '/compress', json={}).json()
    assert usage['automatic'] is False and usage['percent'] == 25 and usage['folded_messages'] == 26
    turns(app, cid, 10)
    next_window = app.state.chat.history.window(cid, send=True)
    assert len(next_window.with_summary(next_window.messages)) == 20
    assert next_window.usage['omitted_messages'] == 5


def test_folded_code_uses_the_saved_expression_and_does_not_cut_long_formulas(app, client):
    conv, model = conversation(client)
    cid = conv['id']
    rows = turns(app, cid, 4)
    expression = "// 按实际列汇总\nSUM('销售明细'[销售额])"
    for mid, name, formula in [(rows[1], '销售合计', expression), (rows[3], '长公式', '1 + ' * 160 + '[销售合计]')]:
        app.state.db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)',
            (uid(), mid, model['id'], 'dax_measure', name, formula, '销售明细', '{}', None, now()))
    notes = client.post(f'/api/conversations/{cid}/context/compress', json={}).json()['summary']
    refs = [n for n in notes if n['类别'] == '历史代码引用']
    assert len(refs) == 2
    assert expression in refs[0]['内容'] and '长公式' in refs[1]['内容'] and '1 +' not in refs[1]['内容']
    assert app.state.db.one('SELECT expression FROM artifacts WHERE name=?', ('长公式',))['expression'].endswith('[销售合计]')


@pytest.mark.parametrize('size,triggered', [(47999, False), (48000, True), (62000, True)])
def test_character_trigger_handles_single_oversized_turn_without_splitting_a_code(size, triggered, app, client):
    conv, _ = conversation(client)
    cid = conv['id']
    literal = "仅筛选 '门店'[编号] 为 0000810。"
    content = 'x' * (size - len(literal) - 1) + '\n' + literal
    mid = add(app, cid, 'user', content)
    window = app.state.chat.history.window(cid, send=True)
    assert window.usage['compressed_now'] == triggered
    if triggered:
        assert window.usage['messages'] == 1 and not window.messages
        assert window.usage['chars'] <= SUMMARY_CHARS and window.notes[0]['来源'] == mid
        assert literal in window.notes[0]['内容'] and 'x' not in window.notes[0]['内容']
        assert not window.quoted_users()
    else:
        assert window.usage['percent'] == 79 and window.messages[0]['content'] == content


def test_busy_chat_blocks_manual_operations_but_allows_reads_and_other_chats(app, client):
    first, _ = conversation(client)
    second, _ = conversation(client)
    path = f"/api/conversations/{first['id']}/context"
    lock = app.state.chat.locks[first['id']] = threading.Lock()
    with lock:
        assert client.post(path + '/compress', json={}).status_code == 409
        assert client.put(path, json={'automatic': False}).status_code == 409
        assert client.get(path).status_code == 200
        assert client.put(f"/api/conversations/{second['id']}/context", json={'automatic': False}).is_success
    assert client.get(path).json()['automatic'] is True


def test_local_compression_does_not_mark_the_shared_model_or_dictionary_as_busy(app, client, monkeypatch):
    conv, model = conversation(client)
    chat = app.state.chat
    original = chat.history.window

    def while_compressing(cid, **opts):
        assert chat.locks[cid].locked()
        assert not chat.generation_status(cid)['running']
        assert not app.state.semantics.in_use(model['id'])
        assert not app.state.powerbi.snapshot_in_use(model['id'])
        return original(cid, **opts)

    monkeypatch.setattr(chat.history, 'window', while_compressing)
    assert client.post(f"/api/conversations/{conv['id']}/context/compress", json={}).is_success
    assert not chat.updating_context and not chat.locks[conv['id']].locked()


def test_folded_pending_card_can_still_receive_a_user_choice(setup, monkeypatch):
    app, client, conv = setup
    cid = conv['id']
    add(app, cid, 'user', '按月对比。')
    mid = add(app, cid, 'assistant', '长的历史输出。' * 6000)
    card = {'topic': '时间', 'question': '按哪个日期？', 'context': '', 'options': [
        {'id': 'A', 'label': '订单日期', 'value': '采用订单日期', 'detail': ''},
        {'id': 'B', 'label': '发货日期', 'value': '采用发货日期', 'detail': ''}]}
    app.state.db.execute('INSERT INTO message_clarifications VALUES (?,?,?)', (mid, encode(card), now()))
    usage = client.post(f'/api/conversations/{cid}/context/compress', json={}).json()
    assert usage['folded_messages'] == 2 and '按哪个日期' in encode(usage['summary'])
    messages = client.get(f'/api/conversations/{cid}/messages').json()
    assert messages[-1]['clarification']['answered'] is False
    provider = FakeProvider([plan(), '实查为 125。[Q1]'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *args: result())
    events = send(client, cid, '我的选择', clarification_reply={'message_id': mid, 'option_id': 'A'})
    assert events[-1]['type'] == 'done' and '采用口径：采用订单日期' in provider.calls[0][1][-1].content
    assert client.get(f'/api/conversations/{cid}/messages').json()[1]['clarification']['answered']
