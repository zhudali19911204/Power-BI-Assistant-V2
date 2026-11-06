from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SettingsInput(BaseModel):
    provider: Literal["openai_compat", "claude"] = "openai_compat"
    model: str = Field(default="", max_length=160)
    base_url: str = Field(default="", max_length=500)
    api_key: str | None = Field(default=None, max_length=2000)
    clear_key: bool = False
    enable_thinking: bool | None = None

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


class ConnectInput(BaseModel):
    port: int = Field(ge=1, le=65535)


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
    content: str = Field(min_length=1, max_length=24000)
    llm_profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    home_table: str = Field(default="", max_length=300)
    clarification_reply: ClarificationReply | None = None
    # One expected result under the intersection of multiple filters.
    # Keep the existing wire field so stored clients with a single point remain compatible.
    points: list[CalibrationPoint] = Field(default_factory=list, max_length=1)

    @field_validator("content")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("请输入需求")
        return value.strip()


class ApplyInput(BaseModel):
    overwrite: bool = False
