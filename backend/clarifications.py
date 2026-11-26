"""One business definition at a time, with explicit choices and a free-text alternative."""
import json
import re

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .database import encode, now
from .language import wording


class ClarificationOption(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1000)
    detail: str = Field(default='', max_length=240)


class ClarificationQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    topic: str = Field(default_factory=lambda: wording('分析口径', 'Analysis scope'), min_length=1, max_length=40)
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
                                       value=wording(f'使用模型已有度量值 [{m.name}]', f'Use the existing measure [{m.name}]')[:1000],
                                       detail=wording(f'来自 {m.table}', f'From {m.table}')[:240]) for m in ctx.measures[:4]]
        if len(options) < 2:
            options.append(ClarificationOption(label=wording('由我定义指标', 'Define a metric'),
                                               value=wording('使用用户补充的自定义指标口径', 'Use the custom metric I describe')))
        if len(options) < 2:
            options.insert(0, ClarificationOption(label=wording('先查看数据概览', 'Start with an overview'),
                value=wording('先按模型中可查询的指标做数据概览', 'Start with an overview of available measures')))
        question = ClarificationQuestion(topic=wording('分析指标', 'Metric'),
            question=wording('这次分析希望采用哪个指标口径？', 'Which metric should this analysis use?'), options=options)
    card = question.model_dump()
    for index, option in enumerate(card['options']):
        option['id'] = chr(65 + index) if index < 26 else str(index + 1)
    card['answered'] = False
    return card


def card_markdown(card):
    lines = [wording('### 确认', '### Confirm ') + card['topic'], '', card['question'], '']
    if card['context']:
        lines += [card['context'], '']
    for option in card['options']:
        line = f"- **{option['id']}. {option['label']}**" + wording('：', ': ') + option['value']
        if option['detail']:
            line += wording('（', ' (') + option['detail'] + wording('）', ')')
        lines.append(line)
    return '\n'.join(lines) + wording('\n\n可选择一项并补充说明，也可以直接填写自定义口径。',
                                     '\n\nChoose an option, add details or enter your own definition.')


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
    text = wording('口径确认 · ', 'Clarification · ') + f"{card['topic']}\n{card['question']}"
    if option:
        text += wording('\n选择：', '\nSelection: ') + option['label'] + wording('\n采用口径：', '\nDefinition: ') + option['value']
        detail = option['detail']
        choice = card.get('scope_binding', {}).get('choices', {}).get(option['id'])
        if choice:
            # The observation label belongs to the card's turn. Keep the field reference.
            detail = detail.removeprefix(choice['field'] + ' · ')
            members = card['scope_binding'].get('members')
            if members:
                text += wording('\n该字段对应的完整原值清单：', '\nAll original values for this field: ') + encode(members)
        if detail:
            text += wording('\n选项说明：', '\nOption details: ') + detail
    if reply.supplement:
        text += wording('\n用户补充：', '\nAdditional details: ') + reply.supplement
    return text
