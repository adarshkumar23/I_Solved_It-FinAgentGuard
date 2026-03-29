from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml

from app.schemas import DisputeRequest, ReconciliationRequest, RefundRequest, RoutePaymentRequest


Decision = Literal["allow", "deny", "require_approval"]


@dataclass(slots=True)
class PolicyResult:
    decision: Decision
    reason: str
    rule: str

    def to_dict(self) -> dict[str, str]:
        return {"decision": self.decision, "reason": self.reason, "rule": self.rule}


class PolicyEngine:
    def __init__(self, rules: dict[str, Any]) -> None:
        self.rules = rules

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PolicyEngine":
        rules_path = Path(path)
        if not rules_path.exists():
            raise FileNotFoundError(f"Rules file not found: {rules_path}")
        with rules_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise ValueError("Rules file must contain a top-level object")
        return cls(loaded)

    def evaluate(self, tool_name: str, payload: Any) -> PolicyResult:
        common_result = self._evaluate_common(payload)
        if common_result:
            return common_result

        if tool_name == "refund" and isinstance(payload, RefundRequest):
            return self._evaluate_refund(payload)
        if tool_name == "route_payment" and isinstance(payload, RoutePaymentRequest):
            return self._evaluate_route_payment(payload)
        if tool_name == "dispute" and isinstance(payload, DisputeRequest):
            return self._evaluate_dispute(payload)
        if tool_name == "reconciliation" and isinstance(payload, ReconciliationRequest):
            return self._evaluate_reconciliation(payload)

        return PolicyResult(
            decision="deny",
            reason=f"unknown tool or payload mismatch: {tool_name}",
            rule="tool.schema_mismatch",
        )

    def _evaluate_common(self, payload: Any) -> PolicyResult | None:
        defaults = self.rules.get("defaults", {})
        allowed_currencies = {str(c).upper() for c in defaults.get("allowed_currencies", [])}
        blocked_actors = set(defaults.get("blocked_actor_ids", []))

        if allowed_currencies and str(payload.currency).upper() not in allowed_currencies:
            return PolicyResult(
                decision="deny",
                reason=f"currency {payload.currency} is not allowed",
                rule="defaults.allowed_currencies",
            )

        if payload.actor_id in blocked_actors:
            return PolicyResult(
                decision="deny",
                reason=f"actor {payload.actor_id} is blocked",
                rule="defaults.blocked_actor_ids",
            )

        return None

    def _evaluate_refund(self, payload: RefundRequest) -> PolicyResult:
        cfg = self.rules.get("refund", {})

        blocked_merchants = set(cfg.get("blocked_merchants", []))
        if payload.merchant_id in blocked_merchants:
            return PolicyResult(
                decision="deny",
                reason=f"merchant {payload.merchant_id} is blocked for refunds",
                rule="refund.blocked_merchants",
            )

        max_transaction_age_days = cfg.get("max_transaction_age_days")
        require_transaction_timestamp = bool(cfg.get("require_transaction_timestamp", False))
        txn_timestamp = payload.txn_created_at

        if require_transaction_timestamp and txn_timestamp is None:
            return PolicyResult(
                decision="require_approval",
                reason="transaction timestamp missing for refund age validation",
                rule="refund.require_transaction_timestamp",
            )

        if max_transaction_age_days is not None and txn_timestamp is not None:
            now_utc = datetime.now(timezone.utc)
            txn_time_utc = txn_timestamp if txn_timestamp.tzinfo else txn_timestamp.replace(tzinfo=timezone.utc)
            txn_time_utc = txn_time_utc.astimezone(timezone.utc)
            age_days = (now_utc - txn_time_utc).days
            if age_days > int(max_transaction_age_days):
                return PolicyResult(
                    decision="deny",
                    reason=f"refund blocked because transaction is {age_days} days old",
                    rule="refund.max_transaction_age_days",
                )

        max_refund_amount = _to_decimal(cfg.get("max_refund_amount"))
        if max_refund_amount is not None and payload.amount > max_refund_amount:
            return PolicyResult(
                decision="deny",
                reason=f"refund amount {payload.amount} exceeds hard limit {max_refund_amount}",
                rule="refund.max_refund_amount",
            )

        max_auto_refund_amount = _to_decimal(cfg.get("max_auto_refund_amount"))
        if max_auto_refund_amount is not None and payload.amount > max_auto_refund_amount:
            return PolicyResult(
                decision="require_approval",
                reason=f"refund amount {payload.amount} exceeds auto-approval limit {max_auto_refund_amount}",
                rule="refund.max_auto_refund_amount",
            )

        return PolicyResult(decision="allow", reason="refund policy checks passed", rule="refund.allow")

    def _evaluate_route_payment(self, payload: RoutePaymentRequest) -> PolicyResult:
        cfg = self.rules.get("route_payment", {})

        allowed_merchants = set(cfg.get("allowed_merchants", []))
        if allowed_merchants and payload.merchant_id not in allowed_merchants:
            return PolicyResult(
                decision="deny",
                reason=f"merchant {payload.merchant_id} is not allowlisted for routing changes",
                rule="route_payment.allowed_merchants",
            )

        allowed_targets = set(cfg.get("allowed_target_gateways", []))
        if allowed_targets and payload.target_gateway not in allowed_targets:
            return PolicyResult(
                decision="deny",
                reason=f"target gateway {payload.target_gateway} is not allowlisted",
                rule="route_payment.allowed_target_gateways",
            )

        require_approval_above_amount = _to_decimal(cfg.get("require_approval_above_amount"))
        if require_approval_above_amount is not None and payload.amount > require_approval_above_amount:
            return PolicyResult(
                decision="require_approval",
                reason=f"routing amount {payload.amount} requires approval",
                rule="route_payment.require_approval_above_amount",
            )

        return PolicyResult(decision="allow", reason="routing policy checks passed", rule="route_payment.allow")

    def _evaluate_dispute(self, payload: DisputeRequest) -> PolicyResult:
        cfg = self.rules.get("dispute", {})

        allowed_statuses = set(cfg.get("allowed_payment_statuses", []))
        if allowed_statuses and payload.payment_status not in allowed_statuses:
            return PolicyResult(
                decision="deny",
                reason=f"dispute action blocked for payment status {payload.payment_status}",
                rule="dispute.allowed_payment_statuses",
            )

        require_approval_actions = set(cfg.get("require_approval_actions", []))
        if payload.action in require_approval_actions:
            return PolicyResult(
                decision="require_approval",
                reason=f"dispute action {payload.action} requires manual approval",
                rule="dispute.require_approval_actions",
            )

        return PolicyResult(decision="allow", reason="dispute policy checks passed", rule="dispute.allow")

    def _evaluate_reconciliation(self, payload: ReconciliationRequest) -> PolicyResult:
        cfg = self.rules.get("reconciliation", {})

        require_approval_above_amount = _to_decimal(cfg.get("require_approval_above_amount"))
        if require_approval_above_amount is not None and payload.amount > require_approval_above_amount:
            return PolicyResult(
                decision="require_approval",
                reason=f"reconciliation amount {payload.amount} requires manual approval",
                rule="reconciliation.require_approval_above_amount",
            )

        return PolicyResult(
            decision="allow",
            reason="reconciliation policy checks passed",
            rule="reconciliation.allow",
        )


class PolicyGuardrailPipeline:
    def __init__(self, engine: PolicyEngine) -> None:
        self.engine = engine

    async def evaluate(self, context: Any) -> dict[str, str]:
        result = self.engine.evaluate(context.tool_name, context.tool_args)
        return result.to_dict()


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
