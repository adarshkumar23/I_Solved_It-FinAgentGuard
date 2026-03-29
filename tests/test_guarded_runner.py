from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.guarded_runner import GuardedExecutionRunner
from app.ledger import InMemoryLedger
from app.main import app
from app.middleware import GuardrailContext
from app.policy_engine import PolicyEngine, PolicyGuardrailPipeline
from app.schemas import RefundRequest
from app.validators import (
    DeterministicValidationPipeline,
    DeterministicValidator,
    LLMReasoningPipeline,
    LLMReasoningValidator,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_runner() -> GuardedExecutionRunner:
    ledger = InMemoryLedger.seed_default()
    engine = PolicyEngine.from_yaml(PROJECT_ROOT / "app" / "rules.yaml")
    policy = PolicyGuardrailPipeline(engine)
    deterministic = DeterministicValidationPipeline(DeterministicValidator(ledger))
    llm = LLMReasoningPipeline(
        LLMReasoningValidator(
            ledger=ledger,
            enabled=True,
            provider="openai",
            openai_api_key=None,
            openai_model="gpt-4.1-mini",
            anthropic_api_key=None,
            anthropic_model="claude-3-5-sonnet-latest",
            timeout_seconds=3.0,
            fail_open=True,
        )
    )
    return GuardedExecutionRunner(policy_stage=policy, deterministic_stage=deterministic, llm_stage=llm)


@pytest.mark.asyncio
async def test_runner_trace_order_is_policy_then_deterministic_then_llm() -> None:
    runner = _build_runner()
    payload = RefundRequest(
        txn_id="txn_123456",
        merchant_id="m_001",
        amount="499.99",
        currency="INR",
        reason="Refund for txn_123456 only",
        actor_id="ops_1",
        txn_created_at=_now(),
        refund_type="full",
        idempotency_key="idem_runner_001",
    )
    ctx = GuardrailContext(
        request_id="req_test_001",
        received_at=_now(),
        actor_id="ops_1",
        tool_name="refund",
        tool_args=payload,
        client_ip="127.0.0.1",
        user_agent="pytest",
    )
    decision = await runner.decide(ctx)
    assert decision.decision == "allow"
    assert [item.stage for item in decision.trace] == ["policy", "deterministic", "llm"]
    assert decision.risk_score >= 20


@pytest.mark.asyncio
async def test_runner_denies_hallucinated_reasoning() -> None:
    runner = _build_runner()
    payload = RefundRequest(
        txn_id="txn_123456",
        merchant_id="m_001",
        amount="499.99",
        currency="INR",
        reason="Refund for txn_123456 validated by txn_fake_555",
        actor_id="ops_1",
        txn_created_at=_now(),
        refund_type="full",
        idempotency_key="idem_runner_002",
    )
    ctx = GuardrailContext(
        request_id="req_test_002",
        received_at=_now(),
        actor_id="ops_1",
        tool_name="refund",
        tool_args=payload,
        client_ip="127.0.0.1",
        user_agent="pytest",
    )
    decision = await runner.decide(ctx)
    assert decision.decision == "deny"
    assert decision.rule == "llm.reasoning_hallucinated_txn"
    assert decision.risk_score >= 90


def test_end_to_end_agent_run_respects_guardrails() -> None:
    client = TestClient(app)
    now = _now().isoformat()

    safe = {
        "tool": "refund",
        "input": {
            "txn_id": "txn_123456",
            "merchant_id": "m_001",
            "amount": "499.99",
            "currency": "INR",
            "reason": "Refund for txn_123456 only",
            "actor_id": "ops_1",
            "txn_created_at": now,
            "refund_type": "full",
            "idempotency_key": "idem_e2e_001",
        },
    }
    blocked = {
        "tool": "refund",
        "input": {
            "txn_id": "txn_123456",
            "merchant_id": "m_001",
            "amount": "499.99",
            "currency": "INR",
            "reason": "Refund for txn_123456 validated by txn_fake_123",
            "actor_id": "ops_1",
            "txn_created_at": now,
            "refund_type": "full",
            "idempotency_key": "idem_e2e_002",
        },
    }

    ok_response = client.post("/agent/run", json=safe)
    blocked_response = client.post("/agent/run", json=blocked)

    assert ok_response.status_code == 200
    assert ok_response.json()["result"]["status"] == "executed"

    assert blocked_response.status_code == 403
    assert blocked_response.json()["result"]["status"] == "blocked"
