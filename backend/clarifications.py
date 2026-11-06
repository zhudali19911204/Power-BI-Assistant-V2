"""One business definition at a time, with explicit choices and a free-text alternative."""
import json
import re

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .database import encode, now


class ClarificationOption(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1000)
    detail: str = Field(default='', max_length=240)


class ClarificationQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    topic: str = Field(default='分析口径', min_length=1, max_length=40)
    question: str = Field(min_length=1, max_length=240)
    context: str = Field(default='', max_length=400)
    options: list[ClarificationOption] = Field(min_length=2, max_length=4)

    @field_validator('question')
    @classmethod
    def one_question(cls, value):
        if len(re.findall(r'[?？]', value)) > 1:
            raise ValueError('本轮只能确认一个问题')
        return value


QUESTION_RULES = '''口径讨论规则：每轮只确认一个最影响分析的问题，等待用户回答后再确认下一项。
不要在同一问题里同时追问费用分类、成本中心、年份和指标；不要把多个问题塞进 context 或选项。
提供 2～4 个围绕同一口径的可选项，用用户能读懂的标签说明差异。value 写完整含义，detail 可注明真实字段引用。
选项只是候选口径，不得将猜测的分类值说成模型实际存在的值；无法确认的值留给用户补充。
用户可选择一项并补充，也可完全自定义。不得默认替用户选择，不得在等待确认时执行分析查询。
从历史中继承用户已明确和已确认的内容，不重复追问，不改动前面确认的条件；如果信息已足够，直接查询，不强行提问。
context 只简短说明用户已提供的信息；question 只问本轮这一项，不预告一连串后续问题。'''


def make_card(question, ctx):
    if not isinstance(question, ClarificationQuestion):
        # Older servers may still return prose containing several questions. Do not display it
        # as a multi-question form or manufacture business values from that prose.
        options = [ClarificationOption(label=f'[{m.name[:96]}'+('…]' if len(m.name)>96 else ']'),
                                       value=f'使用模型已有度量值 [{m.name}]'[:1000],
                                       detail=f'来自 {m.table}'[:240]) for m in ctx.measures[:4]]
        if len(options) < 2:
            options.append(ClarificationOption(label='由我定义指标', value='使用用户补充的自定义指标口径'))
        if len(options) < 2:
            options.insert(0, ClarificationOption(label='先查看数据概览', value='先按模型中可查询的指标做数据概览'))
        question = ClarificationQuestion(topic='分析指标', question='这次分析希望采用哪个指标口径？', options=options)
    card = question.model_dump()
    for index, option in enumerate(card['options']):
        option['id'] = chr(65 + index)
    card['answered'] = False
    return card


def card_markdown(card):
    lines = ['### 确认' + card['topic'], '', card['question'], '']
    if card['context']:
        lines += [card['context'], '']
    for option in card['options']:
        line = f"- **{option['id']}. {option['label']}**：{option['value']}"
        if option['detail']:
            line += '（' + option['detail'] + '）'
        lines.append(line)
    return '\n'.join(lines) + '\n\n可选择一项并补充说明，也可以直接填写自定义口径。'


def save_card(db, message_id, card):
    db.execute('INSERT INTO message_clarifications VALUES (?,?,?)', (message_id, encode(card), now()))


def resolve_answer(db, conversation_id, reply):
    latest = db.one('SELECT id,role,status FROM messages WHERE conversation_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
                    (conversation_id,))
    if not latest or latest['id'] != reply.message_id or latest['role'] != 'assistant' or latest['status'] != 'complete':
        raise HTTPException(409, '这条口径问题已不是当前待确认项，请查看会话中的最新问题')
    row = db.one('SELECT payload FROM message_clarifications WHERE message_id=?', (reply.message_id,))
    if not row:
        raise HTTPException(422, '此回复没有待确认的口径问题')
    card = json.loads(row['payload'])
    option = next((o for o in card['options'] if o['id'] == reply.option_id), None)
    if reply.option_id and not option:
        raise HTTPException(422, '请选择当前问题提供的选项，或填写自定义口径')
    if not option and not reply.supplement:
        raise HTTPException(422, '请选择一个选项或补充你的口径')
    text = f"口径确认 · {card['topic']}\n{card['question']}"
    if option:
        text += f"\n选择：{option['label']}\n采用口径：{option['value']}"
        if option['detail']:
            text += '\n选项说明：' + option['detail']
    if reply.supplement:
        text += '\n用户补充：' + reply.supplement
    return text
