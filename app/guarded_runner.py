from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol


class GuardrailStage(Protocol):
    async def evaluate(self, context: Any) -> dict[str, Any]:
        ...


@dataclass(slots=True)
class GuardedStepTrace:
    stage: str
    decision: str
    rule: str
    reason: str
    risk: int
    at: str


@dataclass(slots=True)
class GuardedDecision:
    decision: str
    reason: str
    rule: str
    risk_score: int
    trace: list[GuardedStepTrace]

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "rule": self.rule,
            "risk_score": self.risk_score,
            "trace": [asdict(item) for item in self.trace],
        }


@dataclass(slots=True)
class GuardedRunResult:
    executed: bool
    decision: GuardedDecision
    response: Any | None = None


class GuardedExecutionRunner:
    def __init__(
        self,
        policy_stage: GuardrailStage,
        deterministic_stage: GuardrailStage,
        llm_stage: GuardrailStage,
    ) -> None:
        self._stages: list[tuple[str, GuardrailStage]] = [
            ("policy", policy_stage),
            ("deterministic", deterministic_stage),
            ("llm", llm_stage),
        ]
        self.logger = logging.getLogger("finagentguard.guardrails")
        self.log_path = Path(__file__).resolve().parents[1] / "logs" / "guardrail_decisions.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    async def evaluate(self, context: Any) -> dict[str, Any]:
        return (await self.decide(context)).to_dict()

    async def run(
        self,
        context: Any,
        execute_fn: Callable[[GuardedDecision], Awaitable[Any]],
    ) -> GuardedRunResult:
        decision = await self.decide(context)
        if decision.decision != "allow":
            return GuardedRunResult(executed=False, decision=decision, response=None)
        response = await execute_fn(decision)
        return GuardedRunResult(executed=True, decision=decision, response=response)

    async def decide(self, context: Any) -> GuardedDecision:
        trace: list[GuardedStepTrace] = []
        final_decision = "deny"
        final_reason = "runner stopped before reaching a final decision"
        final_rule = "guardrail.unknown"

        for stage_name, stage in self._stages:
            raw = await stage.evaluate(context)
            decision = str(raw.get("decision", "deny")).lower()
            reason = str(raw.get("reason", "no reason provided"))
            rule = str(raw.get("rule", f"{stage_name}.unknown_rule"))
            risk = self._score(stage_name, decision, rule)

            item = GuardedStepTrace(
                stage=stage_name,
                decision=decision,
                rule=rule,
                reason=reason,
                risk=risk,
                at=datetime.now(timezone.utc).isoformat(),
            )
            trace.append(item)

            final_decision = decision
            final_reason = reason
            final_rule = rule

            if decision in {"deny", "require_approval"}:
                break

        if final_decision == "allow":
            risk_score = max(item.risk for item in trace) if trace else 20
            risk_score = max(risk_score, 20)
        else:
            risk_score = max(item.risk for item in trace) if trace else 95

        decision_obj = GuardedDecision(
            decision=final_decision,
            reason=final_reason,
            rule=final_rule,
            risk_score=risk_score,
            trace=trace,
        )
        self._log_decision(context, decision_obj)
        return decision_obj

    def _score(self, stage_name: str, decision: str, rule: str) -> int:
        # Fast heuristic score for dashboarding and triage.
        if decision == "deny":
            base = 92
        elif decision == "require_approval":
            base = 70
        else:
            base = {"policy": 22, "deterministic": 18, "llm": 28}.get(stage_name, 20)

        if "hallucinated_txn" in rule:
            base = max(base, 99)
        elif rule.startswith("deterministic."):
            base = min(100, base + 4)
        elif rule.startswith("policy.") or rule.startswith("refund.") or rule.startswith("route_payment."):
            base = min(100, base + 2)

        return min(base, 100)

    def _log_decision(self, context: Any, decision: GuardedDecision) -> None:
        payload = {
            "at": datetime.now(timezone.utc).isoformat(),
            "request_id": getattr(context, "request_id", None),
            "tool_name": getattr(context, "tool_name", None),
            "actor_id": getattr(context, "actor_id", None),
            "decision": decision.decision,
            "reason": decision.reason,
            "rule": decision.rule,
            "risk_score": decision.risk_score,
            "trace": [asdict(item) for item in decision.trace],
        }
        self.logger.info(json.dumps(payload, ensure_ascii=True))
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=True) + "\n")
        except OSError:
            # If log write fails, don't break the request flow.
            pass
