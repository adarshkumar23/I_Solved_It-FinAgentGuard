from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Literal, Protocol

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from app.ledger import InMemoryLedger


Decision = Literal["allow", "deny", "require_approval"]


@dataclass(slots=True)
class ValidationResult:
    decision: Decision
    reason: str
    rule: str

    def to_dict(self) -> dict[str, str]:
        return {"decision": self.decision, "reason": self.reason, "rule": self.rule}


class DeterministicValidator:
    def __init__(self, ledger: InMemoryLedger) -> None:
        self.ledger = ledger

    def evaluate(self, tool_name: str, payload: Any) -> ValidationResult:
        transaction = self.ledger.get_transaction(payload.txn_id)
        if transaction is None:
            return ValidationResult(
                decision="deny",
                reason=f"transaction {payload.txn_id} was not found in ledger",
                rule="deterministic.txn_exists",
            )

        if payload.merchant_id != transaction.merchant_id:
            return ValidationResult(
                decision="deny",
                reason=(
                    f"merchant mismatch: payload merchant {payload.merchant_id} "
                    f"does not own transaction {payload.txn_id}"
                ),
                rule="deterministic.merchant_ownership",
            )

        if _as_decimal(payload.amount) != transaction.amount:
            return ValidationResult(
                decision="deny",
                reason=(
                    f"amount mismatch for transaction {payload.txn_id}: "
                    f"payload={payload.amount}, ledger={transaction.amount}"
                ),
                rule="deterministic.amount_match",
            )

        if str(payload.currency).upper() != transaction.currency.upper():
            return ValidationResult(
                decision="deny",
                reason=(
                    f"currency mismatch for transaction {payload.txn_id}: "
                    f"payload={payload.currency}, ledger={transaction.currency}"
                ),
                rule="deterministic.currency_match",
            )

        if tool_name == "refund" and self.ledger.has_refund_for_transaction(payload.txn_id):
            return ValidationResult(
                decision="deny",
                reason=f"refund already exists for transaction {payload.txn_id}",
                rule="deterministic.no_duplicate_refund",
            )

        return ValidationResult(
            decision="allow",
            reason="deterministic checks passed",
            rule="deterministic.allow",
        )


class LLMReasoningValidator:
    _TXN_PATTERN = re.compile(r"\btxn_[A-Za-z0-9_]+\b")

    def __init__(
        self,
        ledger: InMemoryLedger,
        *,
        enabled: bool,
        provider: Literal["openai", "anthropic"],
        openai_api_key: str | None,
        openai_model: str,
        anthropic_api_key: str | None,
        anthropic_model: str,
        timeout_seconds: float = 8.0,
        fail_open: bool = True,
    ) -> None:
        self.ledger = ledger
        self.enabled = enabled
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.fail_open = fail_open
        self.openai_model = openai_model
        self.anthropic_model = anthropic_model

        self._openai = AsyncOpenAI(api_key=openai_api_key) if openai_api_key else None
        self._anthropic = AsyncAnthropic(api_key=anthropic_api_key) if anthropic_api_key else None

    async def evaluate(self, tool_name: str, payload: Any) -> ValidationResult:
        rationale = str(getattr(payload, "reason", "") or "").strip()
        if not rationale:
            return ValidationResult(
                decision="require_approval",
                reason="missing reasoning text for action",
                rule="llm.reasoning_missing",
            )

        claimed_txn_ids = set(self._TXN_PATTERN.findall(rationale))
        hallucinated_ids = {txn_id for txn_id in claimed_txn_ids if txn_id != payload.txn_id}
        if hallucinated_ids:
            listed = ", ".join(sorted(hallucinated_ids))
            return ValidationResult(
                decision="deny",
                reason=f"reasoning references unrelated transaction IDs: {listed}",
                rule="llm.reasoning_hallucinated_txn",
            )

        if not self.enabled:
            return ValidationResult(
                decision="allow",
                reason="llm validator disabled",
                rule="llm.disabled",
            )

        if self.provider == "openai" and not self._openai:
            return self._fallback_result("missing OpenAI API key")
        if self.provider == "anthropic" and not self._anthropic:
            return self._fallback_result("missing Anthropic API key")

        evidence = self._build_evidence(payload)
        prompt = self._build_prompt(tool_name=tool_name, payload=payload, evidence=evidence)

        try:
            response_json = await asyncio.wait_for(self._call_model(prompt), timeout=self.timeout_seconds)
        except Exception as exc:
            return self._fallback_result(f"llm validator failed: {exc}")

        decision, explanation, confidence = self._parse_model_output(response_json)
        if decision == "FAIL":
            return ValidationResult(
                decision="deny",
                reason=f"llm reasoning check failed ({confidence:.2f}): {explanation}",
                rule="llm.reasoning_fail",
            )
        if decision == "WARN":
            return ValidationResult(
                decision="require_approval",
                reason=f"llm reasoning check flagged risk ({confidence:.2f}): {explanation}",
                rule="llm.reasoning_warn",
            )
        return ValidationResult(
            decision="allow",
            reason=f"llm reasoning check passed ({confidence:.2f})",
            rule="llm.reasoning_pass",
        )

    def _build_evidence(self, payload: Any) -> dict[str, Any]:
        transaction = self.ledger.get_transaction(payload.txn_id)
        return {
            "txn_id": payload.txn_id,
            "ledger_transaction": (
                None
                if not transaction
                else {
                    "txn_id": transaction.txn_id,
                    "merchant_id": transaction.merchant_id,
                    "amount": str(transaction.amount),
                    "currency": transaction.currency,
                    "status": transaction.status,
                    "created_at": transaction.created_at.isoformat(),
                }
            ),
            "refund_exists": self.ledger.has_refund_for_transaction(payload.txn_id),
        }

    def _build_prompt(self, *, tool_name: str, payload: Any, evidence: dict[str, Any]) -> str:
        request_payload = payload.model_dump(mode="json")
        return (
            "You are validating an AI agent tool call in a financial system.\n"
            "Check whether the reasoning is grounded ONLY in the evidence provided.\n"
            "Flag hallucinated transaction references or unsupported claims.\n\n"
            f"Tool: {tool_name}\n"
            f"Request: {json.dumps(request_payload, ensure_ascii=True)}\n"
            f"Evidence: {json.dumps(evidence, ensure_ascii=True)}\n\n"
            "Return JSON only with keys:\n"
            '{"decision":"PASS|WARN|FAIL","confidence":0.0,"explanation":"short reason"}'
        )

    async def _call_model(self, prompt: str) -> dict[str, Any]:
        if self.provider == "anthropic":
            return await self._call_anthropic(prompt)
        return await self._call_openai(prompt)

    async def _call_openai(self, prompt: str) -> dict[str, Any]:
        assert self._openai is not None
        response = await self._openai.chat.completions.create(
            model=self.openai_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Return strict JSON. No markdown."},
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)

    async def _call_anthropic(self, prompt: str) -> dict[str, Any]:
        assert self._anthropic is not None
        response = await self._anthropic.messages.create(
            model=self.anthropic_model,
            max_tokens=300,
            temperature=0,
            system="Return strict JSON only. No markdown.",
            messages=[{"role": "user", "content": prompt}],
        )
        text_chunks = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        return json.loads("".join(text_chunks) or "{}")

    def _parse_model_output(self, data: dict[str, Any]) -> tuple[str, str, float]:
        decision = str(data.get("decision", "WARN")).upper()
        if decision not in {"PASS", "WARN", "FAIL"}:
            decision = "WARN"
        explanation = str(data.get("explanation", "model returned no explanation")).strip()
        confidence_raw = data.get("confidence", 0.0)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(confidence, 1.0))
        return decision, explanation, confidence

    def _fallback_result(self, reason: str) -> ValidationResult:
        if self.fail_open:
            return ValidationResult(decision="allow", reason=reason, rule="llm.fail_open")
        return ValidationResult(decision="require_approval", reason=reason, rule="llm.fail_closed")


class GuardrailPipeline(Protocol):
    async def evaluate(self, context: Any) -> dict[str, Any]:
        ...


class DeterministicValidationPipeline:
    def __init__(self, validator: DeterministicValidator) -> None:
        self.validator = validator

    async def evaluate(self, context: Any) -> dict[str, str]:
        return self.validator.evaluate(context.tool_name, context.tool_args).to_dict()


class LLMReasoningPipeline:
    def __init__(self, validator: LLMReasoningValidator) -> None:
        self.validator = validator

    async def evaluate(self, context: Any) -> dict[str, str]:
        return (await self.validator.evaluate(context.tool_name, context.tool_args)).to_dict()


class ChainedGuardrailPipeline:
    def __init__(self, pipelines: Iterable[GuardrailPipeline]) -> None:
        self.pipelines = list(pipelines)

    async def evaluate(self, context: Any) -> dict[str, Any]:
        last_allow: dict[str, Any] = {"decision": "allow", "reason": "no_checks"}
        for pipeline in self.pipelines:
            result = await pipeline.evaluate(context)
            decision = str(result.get("decision", "deny")).lower()
            if decision != "allow":
                return result
            last_allow = result
        return last_allow


def _as_decimal(value: Any) -> Decimal:
    return Decimal(str(value))
