from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Amount = Annotated[Decimal, Field(gt=Decimal("0"), max_digits=14, decimal_places=2)]


class BaseToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    txn_id: Annotated[str, Field(min_length=6, max_length=64)]
    merchant_id: Annotated[str, Field(min_length=4, max_length=64)]
    amount: Amount
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    reason: Annotated[str, Field(min_length=5, max_length=300)]
    actor_id: Annotated[str, Field(min_length=3, max_length=64)]
    txn_created_at: datetime | None = None

    @field_validator("txn_id", "merchant_id", "actor_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        if " " in value:
            raise ValueError("must not contain spaces")
        return value

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        normalized = value.upper()
        if not normalized.isalpha() or len(normalized) != 3:
            raise ValueError("must be a valid 3-letter ISO code")
        return normalized


class RefundRequest(BaseToolRequest):
    refund_type: Literal["full", "partial"]
    idempotency_key: Annotated[str, Field(min_length=8, max_length=128)]


class RoutePaymentRequest(BaseToolRequest):
    source_gateway: Annotated[str, Field(min_length=2, max_length=32)]
    target_gateway: Annotated[str, Field(min_length=2, max_length=32)]
    route_rule: Annotated[str | None, Field(max_length=120)] = None

    @model_validator(mode="after")
    def validate_gateway_change(self) -> "RoutePaymentRequest":
        if self.source_gateway == self.target_gateway:
            raise ValueError("source_gateway and target_gateway must be different")
        return self


class DisputeRequest(BaseToolRequest):
    dispute_id: Annotated[str, Field(min_length=6, max_length=64)]
    action: Literal["respond", "accept", "escalate"]
    payment_status: Literal["created", "authorized", "captured", "failed", "refunded", "disputed"]
    evidence_note: Annotated[str | None, Field(max_length=500)] = None


class ReconciliationRequest(BaseToolRequest):
    recon_batch_id: Annotated[str, Field(min_length=6, max_length=64)]
    ledger_entry_id: Annotated[str | None, Field(max_length=64)] = None
    settlement_ref: Annotated[str | None, Field(max_length=64)] = None
    expected_settlement_date: date | None = None
