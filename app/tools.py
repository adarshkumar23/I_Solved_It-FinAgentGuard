from __future__ import annotations

from typing import Any, TypedDict

import httpx
from fastapi import FastAPI
from langgraph.graph import END, StateGraph


class AgentRunState(TypedDict, total=False):
    tool: str
    input: dict[str, Any]
    proposed_call: dict[str, Any]
    guardrail_response: dict[str, Any]
    status_code: int


def _propose_tool_call(state: AgentRunState) -> AgentRunState:
    tool_name = state.get("tool")
    tool_input = state.get("input")
    return {"proposed_call": {"tool": tool_name, "input": tool_input}}


def build_guarded_agent_graph(app: FastAPI):
    graph = StateGraph(AgentRunState)

    async def execute_guarded_tool_call(state: AgentRunState) -> AgentRunState:
        payload = state.get("proposed_call", {})
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://finagentguard.local") as client:
            response = await client.post("/agent/tool-call", json=payload)
        return {"guardrail_response": response.json(), "status_code": response.status_code}

    graph.add_node("propose_tool_call", _propose_tool_call)
    graph.add_node("execute_guarded_tool_call", execute_guarded_tool_call)
    graph.set_entry_point("propose_tool_call")
    graph.add_edge("propose_tool_call", "execute_guarded_tool_call")
    graph.add_edge("execute_guarded_tool_call", END)
    return graph.compile()


async def run_agent_tool_request(
    graph,
    *,
    tool: str,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    result = await graph.ainvoke({"tool": tool, "input": tool_input})
    return {
        "status_code": result.get("status_code"),
        "result": result.get("guardrail_response"),
        "tool": tool,
    }
