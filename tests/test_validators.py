from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ledger import InMemoryLedger
from app.schemas import RefundRequest
from app.validators import DeterministicValidator, LLMReasoningValidator


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_deterministic_missing_transaction_denies() -> None:
    validator = DeterministicValidator(InMemoryLedger.seed_default())
    payload = RefundRequest(
        txn_id="txn_missing_001",
        merchant_id="m_001",
        amount="499.99",
        currency="INR",
        reason="Refund request",
        actor_id="ops_1",
        txn_created_at=_now(),
        refund_type="full",
        idempotency_key="idem_val_001",
    )
    result = validator.evaluate("refund", payload)
    assert result.decision == "deny"
    assert result.rule == "deterministic.txn_exists"


def test_deterministic_duplicate_refund_denies() -> None:
    validator = DeterministicValidator(InMemoryLedger.seed_default())
    payload = RefundRequest(
        txn_id="txn_refunded_01",
        merchant_id="merchant_01",
        amount="1200.00",
        currency="INR",
        reason="Duplicate refund attempt",
        actor_id="ops_1",
        txn_created_at=_now(),
        refund_type="full",
        idempotency_key="idem_val_002",
    )
    result = validator.evaluate("refund", payload)
    assert result.decision == "deny"
    assert result.rule == "deterministic.no_duplicate_refund"


@pytest.mark.asyncio
async def test_llm_reasoning_flags_hallucinated_transaction_reference() -> None:
    validator = LLMReasoningValidator(
        ledger=InMemoryLedger.seed_default(),
        enabled=True,
        provider="openai",
        openai_api_key=None,
        openai_model="gpt-4.1-mini",
        anthropic_api_key=None,
        anthropic_model="claude-3-5-sonnet-latest",
        timeout_seconds=3.0,
        fail_open=True,
    )
    payload = RefundRequest(
        txn_id="txn_123456",
        merchant_id="m_001",
        amount="499.99",
        currency="INR",
        reason="Refund for txn_123456 validated against txn_fake_991",
        actor_id="ops_1",
        txn_created_at=_now(),
        refund_type="full",
        idempotency_key="idem_val_003",
    )
    result = await validator.evaluate("refund", payload)
    assert result.decision == "deny"
    assert result.rule == "llm.reasoning_hallucinated_txn"


@pytest.mark.asyncio
async def test_llm_fail_open_allows_when_key_is_missing() -> None:
    validator = LLMReasoningValidator(
        ledger=InMemoryLedger.seed_default(),
        enabled=True,
        provider="openai",
        openai_api_key=None,
        openai_model="gpt-4.1-mini",
        anthropic_api_key=None,
        anthropic_model="claude-3-5-sonnet-latest",
        timeout_seconds=3.0,
        fail_open=True,
    )
    payload = RefundRequest(
        txn_id="txn_123456",
        merchant_id="m_001",
        amount="499.99",
        currency="INR",
        reason="Refund for txn_123456 only",
        actor_id="ops_1",
        txn_created_at=_now(),
        refund_type="full",
        idempotency_key="idem_val_004",
    )
    result = await validator.evaluate("refund", payload)
    assert result.decision == "allow"
    assert result.rule == "llm.fail_open"
