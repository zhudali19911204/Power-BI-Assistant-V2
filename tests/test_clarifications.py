import json

import pytest

from backend.analysis import parse_plan
from backend.app import create_app
from backend.clarifications import ClarificationQuestion
from tests.test_analysis import Provider, plan, result, send, setup


def question(topic='费用分类', text='personnel 费用采用哪个分类字段？'):
    return {'topic': topic, 'question': text, 'context': '已知成本中心编号为 0257。', 'options': [
        {'label': '按费用类别', 'value': "按 'Cost Element'[CE Category] 筛选 personnel 费用", 'detail': '具体分类值可在补充说明填写'},
        {'label': '按一级分类', 'value': "按 'Cost Element'[Class 1] 筛选 personnel 费用", 'detail': ''},
    ]}


def clarify(card=None, **extra):
    return json.dumps({'clarification': card or question(), 'queries': [], **extra}, ensure_ascii=False)


def reply(client, conv, message, *, option='A', supplement=''):
    return client.post(f"/api/conversations/{conv['id']}/messages", json={
        'content': '客户端展示文字不作为采用口径的依据',
        'clarification_reply': {'message_id': message['id'], 'option_id': option, 'supplement': supplement},
    })


def latest(client, conv):
    return client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]


def test_structured_question_is_persisted_with_choices_and_never_executes_a_query(setup, monkeypatch):
    app, client, conv = setup
    provider = Provider([clarify(queries=[{'title': '不能在等待确认时执行', 'dax': 'EVALUATE ROW("x",1)'}])])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    def unexpected(*a):
        raise AssertionError('Must wait for the user')
    monkeypatch.setattr(app.state.chat.analysis, 'execute', unexpected)
    events = send(client, conv, '分析 0257 的 personnel 费用')
    card = next(e['clarification'] for e in events if e['type'] == 'clarification')
    assert card['question'] == question()['question']
    assert [option['id'] for option in card['options']] == ['A', 'B']
    assert not card['answered']
    saved = latest(client, conv)
    assert saved['clarification'] == card and not saved['analysis']
    assert '按费用类别' in saved['content'] and '自定义口径' in saved['content']
    restarted = create_app(app.state.db.path.parent)
    assert restarted.state.chat.messages(conv['id'])[-1]['clarification'] == card
    assert '按费用类别' in client.get(f"/api/conversations/{conv['id']}/export").text
    client.delete(f"/api/conversations/{conv['id']}")
    assert not app.state.db.all('SELECT * FROM message_clarifications')


def test_confirmation_is_one_question_per_turn_and_keeps_prior_confirmed_scope(setup, monkeypatch):
    app, client, conv = setup
    second = question('时间范围', '这次分析采用哪个时间范围？')
    second['options'] = [{'label': '2026 年', 'value': '采用 2026 全年实际发生额'},
                         {'label': '2025 年', 'value': '采用 2025 全年实际发生额'}]
    provider = Provider([clarify(), clarify(second), plan(), '结果125。[Q1]'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    executed = []
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a: (executed.append(a), result())[1])
    send(client, conv, '分析 0257 的 personnel 费用')
    first = latest(client, conv)
    assert not executed
    response = reply(client, conv, first, option='A', supplement='CE Category 的值为 personnel；保留前导零 0257')
    assert response.status_code == 200
    second_message = latest(client, conv)
    assert second_message['clarification']['topic'] == '时间范围'
    assert not executed
    history = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert history[1]['clarification']['answered']
    assert 'CE Category' in history[2]['content'] and '保留前导零 0257' in history[2]['content']
    assert '客户端展示文字' not in history[2]['content']
    assert reply(client, conv, second_message, option='B').status_code == 200
    assert len(executed) == 1
    planner_history = '\n'.join(m.content for m in provider.calls[2][1])
    assert 'personnel' in planner_history and '0257' in planner_history and '2025 全年实际发生额' in planner_history
    assert latest(client, conv)['clarification'] is None


def test_custom_supplement_can_replace_options_and_is_sent_to_the_model(setup, monkeypatch):
    app, client, conv = setup
    provider = Provider([clarify(), plan(), '结果见[Q1]。'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a: result())
    send(client, conv)
    message = latest(client, conv)
    response = reply(client, conv, message, option=None, supplement='自定义：按 Class 2 的人员费用汇总')
    assert response.status_code == 200
    assert '用户补充：自定义：按 Class 2 的人员费用汇总' in provider.calls[1][1][-1].content


@pytest.mark.parametrize('option,supplement', [('Z', '任意说明'), (None, '   ')])
def test_invalid_answer_does_not_start_generation_or_consume_question(setup, monkeypatch, option, supplement):
    app, client, conv = setup
    provider = Provider([clarify()])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    send(client, conv)
    message = latest(client, conv)
    response = reply(client, conv, message, option=option, supplement=supplement)
    assert response.status_code == 422
    assert len(provider.calls) == 1 and not app.state.chat.generation_status(conv['id'])['running']
    assert not latest(client, conv)['clarification']['answered']


def test_old_or_foreign_question_is_rejected_before_calling_model(setup, monkeypatch):
    app, client, conv = setup
    provider = Provider([clarify(), clarify(question('时间范围', '采用哪个时间范围？'))])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    send(client, conv)
    old = latest(client, conv)
    assert reply(client, conv, old).status_code == 200
    assert reply(client, conv, old).status_code == 409
    other = client.post('/api/conversations', json={'model_id': conv['model_id'], 'mode': 'analysis'}).json()
    assert reply(client, other, latest(client, conv)).status_code == 409
    assert len(provider.calls) == 2


def test_manual_chat_reply_also_moves_to_the_next_confirmation(setup, monkeypatch):
    app, client, conv = setup
    provider = Provider([clarify(), clarify(question('时间范围', '采用哪个时间范围？'))])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    send(client, conv)
    send(client, conv, '按费用类别，取值 personnel')
    messages = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert messages[1]['clarification']['answered'] and not messages[-1]['clarification']['answered']
    assert '按费用类别，取值 personnel' in provider.calls[1][1][-1].content


@pytest.mark.parametrize('payload', [
    {'question': '费用类别是哪项？成本中心是哪项？'},
    {'options': [{'label': '只有一项', 'value': '不够明确'}]},
    {'question': 'a' * 241},
])
def test_question_schema_rejects_multiple_questions_or_unbounded_options(payload):
    with pytest.raises(ValueError):
        parse_plan(clarify({**question(), **payload}))
