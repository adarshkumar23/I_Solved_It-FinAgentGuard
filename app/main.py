from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.guarded_runner import GuardedExecutionRunner
from app.ledger import InMemoryLedger
from app.middleware import install_tool_guardrail_middleware
from app.policy_engine import PolicyEngine, PolicyGuardrailPipeline
from app.tools import build_guarded_agent_graph, run_agent_tool_request
from app.validators import (
    DeterministicValidationPipeline,
    DeterministicValidator,
    LLMReasoningPipeline,
    LLMReasoningValidator,
)


app = FastAPI(title="FinAgentGuard")

def build_guardrail_pipeline() -> GuardedExecutionRunner:
    settings = get_settings()
    rules_path = Path(__file__).with_name("rules.yaml")

    policy_engine = PolicyEngine.from_yaml(rules_path)
    policy_pipeline = PolicyGuardrailPipeline(policy_engine)

    ledger = InMemoryLedger.seed_default()
    deterministic_pipeline = DeterministicValidationPipeline(DeterministicValidator(ledger))

    llm_validator = LLMReasoningValidator(
        ledger=ledger,
        enabled=settings.llm_validator_enabled,
        provider=settings.llm_provider,
        openai_api_key=settings.openai_api_key,
        openai_model=settings.openai_model,
        anthropic_api_key=settings.anthropic_api_key,
        anthropic_model=settings.anthropic_model,
        timeout_seconds=settings.llm_timeout_seconds,
        fail_open=settings.llm_fail_open,
    )
    llm_pipeline = LLMReasoningPipeline(llm_validator)
    return GuardedExecutionRunner(
        policy_stage=policy_pipeline,
        deterministic_stage=deterministic_pipeline,
        llm_stage=llm_pipeline,
    )


guardrail_pipeline = build_guardrail_pipeline()
install_tool_guardrail_middleware(app, pipeline=guardrail_pipeline)
guarded_agent_graph = build_guarded_agent_graph(app)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent/tool-call")
async def tool_call(request: Request) -> dict:
    payload = await request.json()
    return {
        "status": "executed",
        "request_id": getattr(request.state, "guardrail_request_id", None),
        "received_at": str(getattr(request.state, "guardrail_received_at", "")),
        "tool": getattr(request.state, "guardrail_tool_name", None),
        "input": getattr(request.state, "guardrail_validated_input", None),
        "risk_score": getattr(request.state, "guardrail_risk_score", None),
        "trace": getattr(request.state, "guardrail_trace", []),
        "payload": payload,
    }


@app.post("/agent/run")
async def run_agent(payload: dict):
    tool = payload.get("tool")
    tool_input = payload.get("input")
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        return JSONResponse(
            status_code=400,
            content={
                "status_code": 400,
                "result": {"status": "invalid_request", "reason": "tool and input are required"},
                "tool": tool,
            },
        )

    result = await run_agent_tool_request(guarded_agent_graph, tool=tool, tool_input=tool_input)
    inner_status = result.get("status_code")
    if isinstance(inner_status, int):
        return JSONResponse(status_code=inner_status, content=result)
    return JSONResponse(status_code=500, content={"status_code": 500, "result": {"status": "error"}})
