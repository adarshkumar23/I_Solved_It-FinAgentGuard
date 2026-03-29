from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.policy_engine import PolicyEngine
from app.schemas import DisputeRequest, RefundRequest


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_refund_over_auto_limit_requires_approval() -> None:
    engine = PolicyEngine.from_yaml(PROJECT_ROOT / "app" / "rules.yaml")
    payload = RefundRequest(
        txn_id="txn_123456",
        merchant_id="m_001",
        amount="6000.00",
        currency="INR",
        reason="High-value refund request",
        actor_id="ops_1",
        txn_created_at=_now(),
        refund_type="partial",
        idempotency_key="idem_policy_001",
    )
    result = engine.evaluate("refund", payload)
    assert result.decision == "require_approval"
    assert result.rule == "refund.max_auto_refund_amount"


def test_refund_with_stale_transaction_is_denied() -> None:
    engine = PolicyEngine.from_yaml(PROJECT_ROOT / "app" / "rules.yaml")
    payload = RefundRequest(
        txn_id="txn_123456",
        merchant_id="m_001",
        amount="499.99",
        currency="INR",
        reason="Refund for old transaction",
        actor_id="ops_1",
        txn_created_at=_now() - timedelta(days=45),
        refund_type="partial",
        idempotency_key="idem_policy_002",
    )
    result = engine.evaluate("refund", payload)
    assert result.decision == "deny"
    assert result.rule == "refund.max_transaction_age_days"


def test_dispute_accept_requires_approval() -> None:
    engine = PolicyEngine.from_yaml(PROJECT_ROOT / "app" / "rules.yaml")
    payload = DisputeRequest(
        txn_id="txn_981234",
        merchant_id="merchant_01",
        amount="4500.00",
        currency="INR",
        reason="Accept dispute after review",
        actor_id="ops_1",
        txn_created_at=_now(),
        dispute_id="disp_1001",
        action="accept",
        payment_status="captured",
    )
    result = engine.evaluate("dispute", payload)
    assert result.decision == "require_approval"
    assert result.rule == "dispute.require_approval_actions"
