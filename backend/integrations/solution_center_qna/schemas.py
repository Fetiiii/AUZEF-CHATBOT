"""Stable public contracts for Solution Center QnA synchronization."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class Pagination(BaseModel):
    next_cursor: str | None
    has_more: bool


class QnAItem(BaseModel):
    id: int
    question: str
    answer: str
    categories: list[str]
    updated_at: datetime


class QnAResponse(BaseModel):
    until: datetime
    items: list[QnAItem]
    pagination: Pagination


class UpsertChange(QnAItem):
    operation: Literal["upsert"] = "upsert"


class DeleteChange(BaseModel):
    id: int
    operation: Literal["delete"] = "delete"
    updated_at: datetime


ChangeItem = Annotated[UpsertChange | DeleteChange, Field(discriminator="operation")]


class ChangesResponse(BaseModel):
    since: datetime
    until: datetime
    items: list[ChangeItem]
    pagination: Pagination


class MetaResponse(BaseModel):
    dataset_version: str | None
    total_active_records: int
    last_updated_at: datetime | None
