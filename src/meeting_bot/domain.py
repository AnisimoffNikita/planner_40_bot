from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Role(StrEnum):
    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN = "admin"


class UserStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class ChatStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    BLOCKED = "blocked"


class PatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["set_field", "add_entry", "delete_entry", "clear_field", "clear_block"]
    block_id: str
    entry_id: str | None = None
    field_id: str | None = None
    value: str | None = None
    human_label: str = ""

    @model_validator(mode="after")
    def validate_shape(self) -> PatchOperation:
        if self.op == "set_field" and self.field_id is None:
            raise ValueError("set_field requires field_id")
        if self.op == "clear_field" and self.field_id is None:
            raise ValueError("clear_field requires field_id")
        if self.op == "delete_entry" and self.entry_id is None:
            raise ValueError("delete_entry requires entry_id")
        return self


class IntentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["question", "show_status", "show_history", "propose_update", "unknown"]
    confidence: float = Field(ge=0, le=1)
    answer: str | None
    patches: list[PatchOperation]
    needs_clarification: bool
    clarification_question: str | None
