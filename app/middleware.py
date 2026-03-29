from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp, Message

from app.schemas import (
    DisputeRequest,
    ReconciliationRequest,
    RefundRequest,
    RoutePaymentRequest,
)


SUPPORTED_TOOLS = {
    "refund": RefundRequest,
    "route_payment": RoutePaymentRequest,
    "dispute": DisputeRequest,
    "reconciliation": ReconciliationRequest,
}


@dataclass(slots=True)
class GuardrailContext:
    request_id: str
    received_at: datetime
    actor_id: str
    tool_name: str
    tool_args: Any
    client_ip: str | None
    user_agent: str | None


class GuardrailPipeline(Protocol):
    async def evaluate(self, context: GuardrailContext) -> dict[str, Any]:
        ...


class GuardrailRunner(Protocol):
    async def run(self, context: GuardrailContext, execute_fn) -> Any:
        ...


class NoopGuardrailPipeline:
    async def evaluate(self, context: GuardrailContext) -> dict[str, Any]:
        return {"decision": "allow", "reason": "no_policy_configured"}


def _request_with_cached_body(request: Request, body: bytes) -> Request:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(request.scope, receive)


class ToolCallGuardrailMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        pipeline: GuardrailPipeline,
        tool_call_path: str = "/agent/tool-call",
    ) -> None:
        super().__init__(app)
        self.pipeline = pipeline
        self.tool_call_path = tool_call_path

    async def dispatch(self, request: Request, call_next):
        if request.method != "POST" or request.url.path != self.tool_call_path:
            return await call_next(request)

        raw_body = await request.body()
        if not raw_body:
            return JSONResponse(status_code=400, content={"detail": "request body is required"})

        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"detail": "request body must be valid JSON"})

        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"detail": "request body must be a JSON object"})

        tool_name = body.get("tool") or body.get("tool_name")
        tool_input = body.get("input") or body.get("arguments")
        if not isinstance(tool_name, str):
            return JSONResponse(status_code=400, content={"detail": "tool (or tool_name) is required"})
        if not isinstance(tool_input, dict):
            return JSONResponse(status_code=400, content={"detail": "input (or arguments) must be an object"})

        schema = SUPPORTED_TOOLS.get(tool_name)
        if schema is None:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"unsupported tool '{tool_name}'",
                    "supported_tools": sorted(SUPPORTED_TOOLS.keys()),
                },
            )

        try:
            validated = schema.model_validate(tool_input)
        except ValidationError as exc:
            return JSONResponse(
                status_code=422,
                content={"detail": exc.errors(include_url=False, include_context=False, include_input=False)},
            )

        request_id = str(uuid4())
        received_at = datetime.now(timezone.utc)

        context = GuardrailContext(
            request_id=request_id,
            received_at=received_at,
            actor_id=validated.actor_id,
            tool_name=tool_name,
            tool_args=validated,
            client_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        decision: dict[str, Any]
        forwarded_request = _request_with_cached_body(request, raw_body)

        def apply_state(target_request: Request, decision_payload: dict[str, Any]) -> None:
            target_request.state.guardrail_request_id = request_id
            target_request.state.guardrail_received_at = received_at
            target_request.state.guardrail_actor_id = validated.actor_id
            target_request.state.guardrail_tool_name = tool_name
            target_request.state.guardrail_validated_input = validated.model_dump(mode="json")
            target_request.state.guardrail_decision = decision_payload
            target_request.state.guardrail_risk_score = decision_payload.get("risk_score")
            target_request.state.guardrail_trace = decision_payload.get("trace", [])

        if hasattr(self.pipeline, "run"):
            async def execute_next(decision_obj) -> Any:
                decision_payload = decision_obj.to_dict()
                apply_state(request, decision_payload)
                apply_state(forwarded_request, decision_payload)
                return await call_next(forwarded_request)

            run_result = await self.pipeline.run(context, execute_next)
            decision = run_result.decision.to_dict()
            if run_result.executed:
                response = run_result.response
                response.headers["x-guardrail-request-id"] = request_id
                response.headers["x-guardrail-risk-score"] = str(decision.get("risk_score", ""))
                return response
        else:
            decision = await self.pipeline.evaluate(context)

        outcome = str(decision.get("decision", "deny")).lower()
        if outcome == "deny":
            return JSONResponse(
                status_code=403,
                content={
                    "request_id": request_id,
                    "status": "blocked",
                    "reason": decision.get("reason", "blocked_by_guardrail"),
                    "rule": decision.get("rule"),
                    "risk_score": decision.get("risk_score"),
                    "trace": decision.get("trace", []),
                    "at": received_at.isoformat(),
                },
                headers={"x-guardrail-risk-score": str(decision.get("risk_score", ""))},
            )
        if outcome == "require_approval":
            return JSONResponse(
                status_code=202,
                content={
                    "request_id": request_id,
                    "status": "approval_required",
                    "reason": decision.get("reason", "approval_required"),
                    "rule": decision.get("rule"),
                    "risk_score": decision.get("risk_score"),
                    "trace": decision.get("trace", []),
                    "at": received_at.isoformat(),
                },
                headers={"x-guardrail-risk-score": str(decision.get("risk_score", ""))},
            )

        apply_state(request, decision)
        apply_state(forwarded_request, decision)

        response = await call_next(forwarded_request)
        response.headers["x-guardrail-request-id"] = request_id
        response.headers["x-guardrail-risk-score"] = str(decision.get("risk_score", ""))
        return response


def install_tool_guardrail_middleware(
    app: FastAPI,
    pipeline: GuardrailPipeline | None = None,
    tool_call_path: str = "/agent/tool-call",
) -> None:
    app.add_middleware(
        ToolCallGuardrailMiddleware,
        pipeline=pipeline or NoopGuardrailPipeline(),
        tool_call_path=tool_call_path,
    )
