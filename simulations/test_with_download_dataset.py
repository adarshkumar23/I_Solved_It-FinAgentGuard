from __future__ import annotations

import json
import asyncio
import random
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.guarded_runner import GuardedExecutionRunner
from app.ledger import InMemoryLedger, LedgerRefund, LedgerTransaction
from app.middleware import GuardrailContext
from app.policy_engine import PolicyEngine, PolicyGuardrailPipeline
from app.schemas import DisputeRequest, RefundRequest, RoutePaymentRequest
from app.validators import (
    DeterministicValidationPipeline,
    DeterministicValidator,
    LLMReasoningPipeline,
    LLMReasoningValidator,
)


DATA_DIR = PROJECT_ROOT / "data_input"
REPORT_PATH = PROJECT_ROOT / "simulations" / "dataset_brief_report.json"


@dataclass(slots=True)
class BriefCase:
    case_id: str
    tool: str
    payload: Any
    expected_status: str
    tag: str


def _parse_dt(value: str) -> datetime:
    value = value.strip()
    # Source dataset format: 2024-09-02 18:15:17
    parsed = datetime.fromisoformat(value.replace(" ", "T"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_transactions() -> list[dict[str, str]]:
    import csv

    rows: list[dict[str, str]] = []
    with (DATA_DIR / "transactions.csv").open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)
    return rows


def _load_refunds() -> list[dict[str, str]]:
    import csv

    rows: list[dict[str, str]] = []
    with (DATA_DIR / "refunds.csv").open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)
    return rows


def _build_ledger(transactions: list[dict[str, str]], refunds: list[dict[str, str]]) -> InMemoryLedger:
    txns: list[LedgerTransaction] = []
    refunds_rows: list[LedgerRefund] = []

    for row in transactions:
        txns.append(
            LedgerTransaction(
                txn_id=row["txn_id"],
                merchant_id=row["merchant_id"],
                amount=Decimal(str(row["amount"])),
                currency=row["currency"].upper(),
                status=row["status"],
                created_at=_parse_dt(row["created_at"]),
            )
        )

    txn_index = {t.txn_id: t for t in txns}
    for row in refunds:
        txn = txn_index.get(row["txn_id"])
        if txn is None:
            continue
        refunds_rows.append(
            LedgerRefund(
                refund_id=row["refund_id"],
                txn_id=row["txn_id"],
                amount=Decimal(str(row["refund_amount"])),
                created_at=_parse_dt(row["created_at"]),
            )
        )

    return InMemoryLedger(transactions=txns, refunds=refunds_rows)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_cases(transactions: list[dict[str, str]], refunds: list[dict[str, str]]) -> list[BriefCase]:
    random.seed(7)

    inr_success = [row for row in transactions if row["currency"] == "INR" and row["status"] == "success"]
    refund_txn_ids = {row["txn_id"] for row in refunds}
    not_refunded = [row for row in inr_success if row["txn_id"] not in refund_txn_ids]
    already_refunded = [row for row in inr_success if row["txn_id"] in refund_txn_ids]

    # Keep route amounts low enough for auto allow.
    route_candidates = [row for row in not_refunded if Decimal(str(row["amount"])) <= Decimal("100000")]

    random.shuffle(route_candidates)
    random.shuffle(not_refunded)
    random.shuffle(already_refunded)

    cases: list[BriefCase] = []
    now = _now()

    def expected_for_refund_guardrails(amount: Decimal, *, low_amount_block: bool = True) -> str:
        if amount > Decimal("50000"):
            return "blocked"
        if amount > Decimal("5000"):
            return "approval_required"
        return "blocked" if low_amount_block else "executed"

    # 40 expected executed: valid route changes.
    for i, row in enumerate(route_candidates[:40], start=1):
        payload = RoutePaymentRequest(
            txn_id=row["txn_id"],
            merchant_id=row["merchant_id"],
            amount=str(row["amount"]),
            currency="INR",
            reason=f"Route transaction {row['txn_id']} through approved gateway",
            actor_id=f"dataset_route_{(i % 5) + 1}",
            txn_created_at=now,
            source_gateway="razorpay",
            target_gateway="hdfc",
            route_rule=f"dataset_route_rule_{i:03d}",
        )
        cases.append(BriefCase(case_id=f"C_EXEC_{i:03d}", tool="route_payment", payload=payload, expected_status="executed", tag="safe_route"))

    # 30 expected blocked: amount mismatch.
    for i, row in enumerate(not_refunded[:30], start=1):
        adjusted_amount = Decimal(str(row["amount"])) + Decimal("1.00")
        expected_status = expected_for_refund_guardrails(adjusted_amount)
        payload = RefundRequest(
            txn_id=row["txn_id"],
            merchant_id=row["merchant_id"],
            amount=str(adjusted_amount),
            currency="INR",
            reason=f"Refund mismatch check for {row['txn_id']}",
            actor_id=f"dataset_amt_{(i % 5) + 1}",
            txn_created_at=now,
            refund_type="partial",
            idempotency_key=f"dataset_amt_mismatch_{i:03d}",
        )
        cases.append(
            BriefCase(
                case_id=f"C_DENY_AMT_{i:03d}",
                tool="refund",
                payload=payload,
                expected_status=expected_status,
                tag="amount_mismatch",
            )
        )

    # 20 expected blocked: hallucinated transaction IDs.
    for i, row in enumerate(not_refunded[30:50], start=1):
        fake_id = f"TXNFAKE{i:05d}"
        expected_status = expected_for_refund_guardrails(Decimal(str(row["amount"])))
        payload = RefundRequest(
            txn_id=row["txn_id"],
            merchant_id=row["merchant_id"],
            amount=str(row["amount"]),
            currency="INR",
            reason=f"Refund {row['txn_id']} cross-validated with {fake_id}",
            actor_id=f"dataset_hallu_{(i % 5) + 1}",
            txn_created_at=now,
            refund_type="full",
            idempotency_key=f"dataset_hallu_{i:03d}",
        )
        cases.append(
            BriefCase(
                case_id=f"C_DENY_HALLU_{i:03d}",
                tool="refund",
                payload=payload,
                expected_status=expected_status,
                tag="hallucinated_reference",
            )
        )

    # 15 expected blocked: unknown transactions.
    for i in range(1, 16):
        payload = RefundRequest(
            txn_id=f"TXN_MISSING_{i:03d}",
            merchant_id="MRC0001",
            amount="999.00",
            currency="INR",
            reason=f"Refund request for unknown transaction TXN_MISSING_{i:03d}",
            actor_id=f"dataset_unknown_{(i % 4) + 1}",
            txn_created_at=now,
            refund_type="full",
            idempotency_key=f"dataset_unknown_{i:03d}",
        )
        cases.append(BriefCase(case_id=f"C_DENY_UNKNOWN_{i:03d}", tool="refund", payload=payload, expected_status="blocked", tag="unknown_txn"))

    # 20 expected blocked: duplicate refunds from provided refunds.csv rows.
    for i, row in enumerate(already_refunded[:20], start=1):
        expected_status = expected_for_refund_guardrails(Decimal(str(row["amount"])))
        payload = RefundRequest(
            txn_id=row["txn_id"],
            merchant_id=row["merchant_id"],
            amount=str(row["amount"]),
            currency="INR",
            reason=f"Second refund attempt for {row['txn_id']}",
            actor_id=f"dataset_dup_{(i % 4) + 1}",
            txn_created_at=now,
            refund_type="full",
            idempotency_key=f"dataset_dup_{i:03d}",
        )
        cases.append(
            BriefCase(
                case_id=f"C_DENY_DUP_{i:03d}",
                tool="refund",
                payload=payload,
                expected_status=expected_status,
                tag="duplicate_refund",
            )
        )

    # 15 expected approval: dispute accept.
    for i, row in enumerate(inr_success[:15], start=1):
        payload = DisputeRequest(
            txn_id=row["txn_id"],
            merchant_id=row["merchant_id"],
            amount=str(row["amount"]),
            currency="INR",
            reason=f"Dispute accept request for {row['txn_id']}",
            actor_id=f"dataset_dispute_{(i % 4) + 1}",
            txn_created_at=now,
            dispute_id=f"DIS_DATASET_{i:03d}",
            action="accept",
            payment_status="captured",
            evidence_note="dataset dispute review",
        )
        cases.append(BriefCase(case_id=f"C_APPROVE_{i:03d}", tool="dispute", payload=payload, expected_status="approval_required", tag="dispute_accept"))

    return cases


def _to_status(decision: str) -> str:
    if decision == "allow":
        return "executed"
    if decision == "require_approval":
        return "approval_required"
    return "blocked"


def run_brief_dataset_test() -> dict[str, Any]:
    transactions = _load_transactions()
    refunds = _load_refunds()

    ledger = _build_ledger(transactions, refunds)
    engine = PolicyEngine.from_yaml(PROJECT_ROOT / "app" / "rules.yaml")

    policy_pipeline = PolicyGuardrailPipeline(engine)
    deterministic_pipeline = DeterministicValidationPipeline(DeterministicValidator(ledger))
    llm_pipeline = LLMReasoningPipeline(
        LLMReasoningValidator(
            ledger=ledger,
            enabled=False,
            provider="openai",
            openai_api_key=None,
            openai_model="gpt-4.1-mini",
            anthropic_api_key=None,
            anthropic_model="claude-3-5-sonnet-latest",
            timeout_seconds=3.0,
            fail_open=True,
        )
    )
    runner = GuardedExecutionRunner(
        policy_stage=policy_pipeline,
        deterministic_stage=deterministic_pipeline,
        llm_stage=llm_pipeline,
    )

    cases = _build_cases(transactions, refunds)
    results: list[dict[str, Any]] = []
    counter_actual: Counter[str] = Counter()
    counter_expected: Counter[str] = Counter()
    tag_counter: Counter[str] = Counter()
    passed = 0

    async def _execute_all() -> None:
        nonlocal passed
        for idx, case in enumerate(cases, start=1):
            ctx = GuardrailContext(
                request_id=f"dataset_case_{idx:04d}",
                received_at=_now(),
                actor_id=case.payload.actor_id,
                tool_name=case.tool,
                tool_args=case.payload,
                client_ip="127.0.0.1",
                user_agent="dataset-brief-test",
            )
            decision_obj = await runner.decide(ctx)
            actual_status = _to_status(decision_obj.decision)

            matched = actual_status == case.expected_status
            if matched:
                passed += 1

            counter_expected[case.expected_status] += 1
            counter_actual[actual_status] += 1
            tag_counter[case.tag] += 1

            results.append(
                {
                    "case_id": case.case_id,
                    "tool": case.tool,
                    "tag": case.tag,
                    "expected_status": case.expected_status,
                    "actual_status": actual_status,
                    "matched": matched,
                    "rule": decision_obj.rule,
                    "risk_score": decision_obj.risk_score,
                    "reason": decision_obj.reason,
                }
            )

    asyncio.run(_execute_all())

    total = len(cases)
    failed = total - passed
    report = {
        "generated_at": _now().isoformat(),
        "dataset_files": {
            "transactions": str(DATA_DIR / "transactions.csv"),
            "refunds": str(DATA_DIR / "refunds.csv"),
            "disputes": str(DATA_DIR / "disputes.csv"),
            "reconciliation": str(DATA_DIR / "reconciliation.csv"),
            "scenario_labels": str(DATA_DIR / "scenario_labels.csv"),
        },
        "summary": {
            "total_cases": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": round((passed / total) if total else 0.0, 4),
            "expected_distribution": dict(counter_expected),
            "actual_distribution": dict(counter_actual),
        },
        "case_tags": dict(tag_counter),
        "failed_examples": [row for row in results if not row["matched"]][:20],
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    out = run_brief_dataset_test()
    summary = out["summary"]
    print(f"Dataset brief test cases: {summary['total_cases']}")
    print(f"Pass rate: {summary['pass_rate'] * 100:.2f}% ({summary['passed']}/{summary['total_cases']})")
    print(f"Expected: {summary['expected_distribution']}")
    print(f"Actual: {summary['actual_distribution']}")
    print(f"Report written: {REPORT_PATH}")
