"""Language of application-authored text for one request or background generation."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

Language = Literal['zh-CN', 'en']
# These section labels are also recognized when compacting saved analysis replies.
OPEN_QUESTION_LABELS = ('尚未完成：', 'Still open: ')
_language: ContextVar[Language] = ContextVar('ui_language', default='zh-CN')


@contextmanager
def language_scope(language: Language):
    token = _language.set(language)
    try:
        yield
    finally:
        _language.reset(token)


def wording(chinese: str, english: str) -> str:
    return english if _language.get() == 'en' else chinese


def reply_instruction() -> str:
    name = wording('Simplified Chinese', 'English')
    return (f'Write user-facing explanations, headings, questions, query titles and new code comments in {name}. '
            'Follow an explicit language request from the user. Preserve actual model identifiers, field values, '
            'JSON keys, evidence labels and code fence languages exactly.')
