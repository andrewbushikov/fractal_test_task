"""
Data models for AI Inbox Classifier.
Strict Pydantic schemas for LLM output validation.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class Category(str, Enum):
    AUTOMATION = "автоматизація"
    INTEGRATION = "інтеграція"
    REPORT_ANALYTICS = "звіт/аналітика"
    BUG_SUPPORT = "баг/підтримка"
    QUESTION_CONSULTING = "питання/консультація"
    OUT_OF_SCOPE = "поза скоупом"


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ClassifiedRequest(BaseModel):
    """Structured classification result for a single inbox request."""

    request_id: str = Field(description="Original request ID from CSV")
    channel: str = Field(description="Source channel (slack/telegram/email)")
    timestamp: str = Field(description="Original timestamp")
    raw_text: str = Field(description="Original raw text")

    # LLM-extracted fields
    category: Category = Field(description="Request category")
    target_department: Optional[str] = Field(
        default=None,
        description="Requesting department or null if unclear",
    )
    priority: Priority = Field(description="Inferred priority: low/medium/high")
    short_summary: str = Field(description="One-sentence summary of the request")
    requested_actions: list[str] = Field(
        default_factory=list,
        description="List of concrete actions requested (0, 1, or more)",
    )
    needs_clarification: bool = Field(
        description="True if request is too vague to act on as-is"
    )

    # Extended fields (added for richer triage)
    sentiment: Optional[str] = Field(
        default=None,
        description="Tone: neutral / frustrated / urgent / polite",
    )
    estimated_complexity: Optional[str] = Field(
        default=None,
        description="Rough complexity: simple / medium / complex",
    )
    clarification_question: Optional[str] = Field(
        default=None,
        description="If needs_clarification=true, what to ask the requester",
    )
    llm_confidence: Optional[str] = Field(
        default=None,
        description="Model self-assessed confidence: low / medium / high",
    )

    @field_validator("short_summary")
    @classmethod
    def summary_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("short_summary cannot be empty")
        return v

    @field_validator("requested_actions", mode="before")
    @classmethod
    def coerce_actions(cls, v) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            # Sometimes LLM returns a single string instead of list
            return [v] if v.strip() else []
        return [str(a) for a in v if str(a).strip()]


class LLMRawOutput(BaseModel):
    """Schema for parsing raw JSON returned by LLM — before merging with CSV fields."""

    category: str
    target_department: Optional[str] = None
    priority: str
    short_summary: str
    requested_actions: list[str] = Field(default_factory=list)
    needs_clarification: bool
    sentiment: Optional[str] = None
    estimated_complexity: Optional[str] = None
    clarification_question: Optional[str] = None
    llm_confidence: Optional[str] = None

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        allowed = {c.value for c in Category}
        if v not in allowed:
            raise ValueError(f"Unknown category '{v}'. Allowed: {allowed}")
        return v

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str) -> str:
        allowed = {p.value for p in Priority}
        v_lower = v.lower()
        if v_lower not in allowed:
            raise ValueError(f"Unknown priority '{v}'. Allowed: {allowed}")
        return v_lower
