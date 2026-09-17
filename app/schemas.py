"""JSON response schemas. They carry no pre-baked payloads: every field is
populated from the values the parser computed for the uploaded batch."""

from __future__ import annotations

from pydantic import BaseModel


class DetailOut(BaseModel):
    line: int
    sequence: int
    invoice: str
    amount_cents: int


class DeclaredOut(BaseModel):
    batch_number: str
    detail_count: int
    total_amount_cents: int


class ErrorOut(BaseModel):
    line: int
    column: int
    code: str
    message: str


class VerificationResponse(BaseModel):
    status: str
    declared: DeclaredOut | None = None
    computed_detail_count: int | None = None
    computed_total_amount_cents: int | None = None
    details: list[DetailOut] = []
    differences: dict[str, int] | None = None
    error: ErrorOut | None = None
