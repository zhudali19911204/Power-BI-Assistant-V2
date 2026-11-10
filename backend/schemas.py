from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .streaming import GENERATION_IDLE_TIMEOUT, GENERATION_TIMEOUT
from .engine.llm.diagnostics import DEFAULT_MAX_TOKENS
from .engine.llm.reasoning import ReasoningLevel


class SettingsInput(BaseModel):
    provider: Literal["openai_compat", "claude"] = "openai_compat"
    model: str = Field(default="", max_length=160)
    base_url: str = Field(default="", max_length=500)
    api_key: str | None = Field(default=None, max_length=2000)
    clear_key: bool = False
    enable_thinking: bool | None = None
    reasoning_level: ReasoningLevel = 'low'
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=256, le=131072)
    idle_timeout: int = Field(default=GENERATION_IDLE_TIMEOUT, ge=30, le=600)
    generation_timeout: int = Field(default=GENERATION_TIMEOUT, ge=180, le=7200)

    @field_validator("model", "base_url", "api_key")
    @classmethod
    def trimmed(cls, value):
        return value.strip() if isinstance(value, str) else value


class LLMProfileInput(SettingsInput):
    name: str = Field(default="", max_length=160)

    @field_validator("name")
    @classmethod
    def trimmed_name(cls, value):
        return value.strip()


class ReasoningInput(BaseModel):
    reasoning_level: ReasoningLevel


class ConnectInput(BaseModel):
    port: int = Field(ge=1, le=65535)
    report_name: str = Field(default='', max_length=260)


class ConversationInput(BaseModel):
    model_id: str
    mode: Literal["dax", "mquery", "analysis"] = "dax"
    query_name: str = Field(default="", max_length=300)
    title: str = Field(default="新对话", min_length=1, max_length=120)


class FilterInput(BaseModel):
    table: str = Field(min_length=1, max_length=300)
    column: str = Field(min_length=1, max_length=300)
    value: str | int | float | bool | None


class CalibrationPoint(BaseModel):
    filters: list[FilterInput] = Field(default_factory=list, max_length=10)
    expected: float = Field(allow_inf_nan=False)
    tolerance: float = Field(default=0.01, ge=0, allow_inf_nan=False)


class ClarificationReply(BaseModel):
    message_id: str = Field(min_length=1, max_length=64)
    option_id: str | None = Field(default=None, max_length=8)
    supplement: str = Field(default='', max_length=4000)

    @field_validator('supplement')
    @classmethod
    def trimmed_supplement(cls, value):
        return value.strip()


class MessageInput(BaseModel):
    content: str = Field(default='', max_length=24000)
    generation_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    image_ids: list[str] = Field(default_factory=list, max_length=4)
    llm_profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    home_table: str = Field(default="", max_length=300)
    clarification_reply: ClarificationReply | None = None
    # One expected result under the intersection of multiple filters.
    # Keep the existing wire field so stored clients with a single point remain compatible.
    points: list[CalibrationPoint] = Field(default_factory=list, max_length=1)

    @field_validator("content")
    @classmethod
    def not_blank(cls, value):
        return value.strip()

    @field_validator('image_ids')
    @classmethod
    def valid_images(cls, value):
        import re
        if len(set(value))!=len(value) or any(not re.fullmatch(r'[a-f0-9]{32}',v) for v in value):
            raise ValueError('图片标识无效或重复，请重新添加图片')
        return value

    @model_validator(mode='after')
    def require_input(self):
        if not self.content and not self.image_ids:raise ValueError('请输入需求或添加图片')
        if not self.content:self.content='请结合图片和当前上下文，帮我分析并处理。'
        return self


class ApplyInput(BaseModel):
    overwrite: bool = False
    name: str | None = Field(default=None, min_length=1, max_length=300)
    home_table: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator('name','home_table')
    @classmethod
    def write_target(cls,value):
        if value is None:return value
        value=value.strip()
        if not value or any(ord(c)<32 for c in value):
            raise ValueError('名称和存放表不能为空，也不能包含控制字符')
        return value


class CancelGenerationInput(BaseModel):
    generation_id: str = Field(pattern=r'^[a-f0-9]{32}$')
