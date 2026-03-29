from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from simulations.replay import OUTCOMES_PATH, REPORT_PATH, run_replay


LOG_PATH = PROJECT_ROOT / "logs" / "guardrail_decisions.jsonl"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_jsonl(path: Path, limit: int = 1000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]


def _decision_counts(rows: list[dict[str, Any]]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for row in rows:
        decision = str(row.get("decision", "unknown")).lower()
        counter[decision] += 1
    return counter


def _render_header() -> None:
    st.set_page_config(page_title="FinAgentGuard Dashboard", layout="wide")
    st.title("FinAgentGuard Dashboard")
    st.caption("Quick view of guardrail decisions and replay results")


def _render_replay_controls() -> None:
    st.subheader("Replay")
    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("Run Replay Now", use_container_width=True):
            with st.spinner("Running adversarial replay scenarios..."):
                result = run_replay()
            st.success(f"Replay completed: {result['scenario_count']} scenarios")
    with col2:
        st.write("Runs scenarios through `/agent/run` and refreshes report files.")


def _render_metrics(report: dict[str, Any], log_rows: list[dict[str, Any]]) -> None:
    metrics = report.get("metrics", {})
    decisions = _decision_counts(log_rows)
    hallucination_alerts = sum(1 for row in log_rows if "hallucinated" in str(row.get("rule", "")))

    st.subheader("Key Metrics")
    cols = st.columns(6)
    cols[0].metric("Total Calls", str(sum(decisions.values())))
    cols[1].metric("Allowed", str(decisions.get("allow", 0)))
    cols[2].metric("Blocked", str(decisions.get("deny", 0)))
    cols[3].metric("Approval Required", str(decisions.get("require_approval", 0)))
    cols[4].metric("Unsafe Block Rate", f"{metrics.get('unsafe_block_rate', 0) * 100:.1f}%")
    cols[5].metric("False Positive Rate", f"{metrics.get('false_positive_rate', 0) * 100:.1f}%")

    cols2 = st.columns(3)
    cols2[0].metric("Hallucination Detection", f"{metrics.get('hallucination_detection_rate', 0) * 100:.1f}%")
    cols2[1].metric("Hallucination Alerts (Live)", str(hallucination_alerts))
    cols2[2].metric("Expected Pass", f"{metrics.get('passed_expected', 0)}/{metrics.get('total_scenarios', 0)}")


def _render_top_rules(report: dict[str, Any]) -> None:
    st.subheader("Top Rule Hits")
    top_rules = report.get("top_rule_hits", {})
    if not top_rules:
        st.info("No rule-hit data available yet. Run replay first.")
        return

    rows = [{"rule": rule, "hits": hits} for rule, hits in top_rules.items()]
    df = pd.DataFrame(rows).sort_values("hits", ascending=False)
    st.bar_chart(df.set_index("rule"))
    st.dataframe(df, use_container_width=True, hide_index=True)


def _render_hallucination_alerts(log_rows: list[dict[str, Any]]) -> None:
    st.subheader("Hallucination Alerts")
    alerts = [row for row in log_rows if "hallucinated" in str(row.get("rule", ""))]
    if not alerts:
        st.success("No live hallucination alerts recorded yet.")
        return
    alerts_df = pd.DataFrame(
        [
            {
                "at": row.get("at"),
                "request_id": row.get("request_id"),
                "tool": row.get("tool_name"),
                "risk_score": row.get("risk_score"),
                "reason": row.get("reason"),
            }
            for row in alerts
        ]
    ).sort_values("at", ascending=False)
    st.dataframe(alerts_df, use_container_width=True, hide_index=True)


def _render_scenario_table(outcomes_payload: dict[str, Any]) -> None:
    st.subheader("Scenario Replay Results")
    outcomes = outcomes_payload.get("outcomes", [])
    if not outcomes:
        st.info("No replay outcomes found. Run replay to generate scenario results.")
        return

    df = pd.DataFrame(outcomes)
    tool_options = sorted(df["tool"].dropna().unique().tolist())
    status_options = sorted(df["actual_status"].dropna().unique().tolist())

    col1, col2 = st.columns(2)
    selected_tools = col1.multiselect("Filter by Tool", options=tool_options, default=tool_options)
    selected_status = col2.multiselect("Filter by Actual Status", options=status_options, default=status_options)

    filtered = df[df["tool"].isin(selected_tools) & df["actual_status"].isin(selected_status)]
    st.dataframe(
        filtered[
            [
                "id",
                "tool",
                "expected_status",
                "actual_status",
                "risk_score",
                "rule",
                "reason",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )


def _render_decision_inspector(log_rows: list[dict[str, Any]]) -> None:
    st.subheader("Decision Details")
    if not log_rows:
        st.info("No decision log entries yet. Trigger requests or run replay.")
        return

    recent = list(reversed(log_rows[-200:]))
    labels = [
        f"{row.get('at', 'unknown')} | {row.get('decision', 'unknown')} | {row.get('tool_name', '-')}"
        for row in recent
    ]
    selected_index = st.selectbox("Select Decision", options=list(range(len(recent))), format_func=lambda i: labels[i])
    selected = recent[selected_index]

    c1, c2 = st.columns(2)
    c1.write(f"**Request ID:** {selected.get('request_id')}")
    c1.write(f"**Actor ID:** {selected.get('actor_id')}")
    c1.write(f"**Tool:** {selected.get('tool_name')}")
    c2.write(f"**Decision:** {selected.get('decision')}")
    c2.write(f"**Rule:** {selected.get('rule')}")
    c2.write(f"**Risk Score:** {selected.get('risk_score')}")

    st.write("**Reason**")
    st.code(str(selected.get("reason", "")))
    st.write("**Trace**")
    st.json(selected.get("trace", []))


def main() -> None:
    _render_header()
    _render_replay_controls()

    report = _read_json(REPORT_PATH)
    outcomes_payload = _read_json(OUTCOMES_PATH)
    log_rows = _read_jsonl(LOG_PATH, limit=5000)

    _render_metrics(report, log_rows)

    left, right = st.columns(2)
    with left:
        _render_top_rules(report)
    with right:
        _render_hallucination_alerts(log_rows)

    _render_scenario_table(outcomes_payload)
    _render_decision_inspector(log_rows)


if __name__ == "__main__":
    main()
