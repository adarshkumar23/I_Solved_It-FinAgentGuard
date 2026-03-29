# FinAgentGuard

> **Guardrail layer for financial AI agents — stops hallucinated tool calls before they cost real money.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-green)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-agentic-orange)](https://github.com/langchain-ai/langgraph)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Dashboard Preview

![FinAgentGuard Dashboard Overview](docs/dashboard_overview.png)
![Top Rule Hits Chart](docs/dashboard_top_rule_hits_chart.png)
![Hallucination Alerts](docs/dashboard_hallucination_alerts.png)
![Top Rule Hits Table](docs/dashboard_top_rule_hits_table.png)
![Scenario Replay Results](docs/dashboard_scenario_replay_results.png)
![Decision Details](docs/dashboard_decision_details.png)

---

## The Problem This Solves

Razorpay's Agent Studio (launched March 2026) enables AI agents to execute high-stakes payment operations — refunds, dispute resolution, routing decisions, reconciliation. A single hallucinated tool call in this environment doesn't just fail silently: **it moves real money.**

Existing LLM guardrail libraries (NeMo Guardrails, Guardrails AI) are designed for text output safety. They don't understand payment semantics — ledger state, policy thresholds, approval hierarchies, or financial risk scoring.

**FinAgentGuard fills that gap.**

---

## Dashboard

![FinAgentGuard Dashboard](docs/dashboard_overview.png)

The Streamlit decision inspector shows live guardrail metrics, top rule hits, hallucination alerts, and full per-decision traces with risk scores.

**Decision trace — hallucinated refund caught at the LLM layer:**

```
Stage 0  policy         ALLOW   refund policy checks passed              risk: 24
Stage 1  deterministic  ALLOW   deterministic checks passed              risk: 22
Stage 2  llm            DENY    references unrelated txn: txn_fake_123   risk: 99
```

Final: **DENY** · Rule: `llm.reasoning_hallucinated_txn` · Risk Score: **99**

---

## Live Results (743 total calls)

| Metric | Value |
|---|---|
| Total Calls | **743** |
| Allowed | 224 |
| Blocked | 351 |
| Approval Required | 168 |
| Unsafe Block Rate | **100.0%** |
| False Positive Rate | **0.0%** |
| Hallucination Detection Rate | **100.0%** |
| Hallucination Alerts (Live) | 54 |
| Simulation Pass Rate | **330 / 330** |

### Top Rule Hits

| Rule | Hits |
|---|---|
| `llm.reasoning_hallucinated_txn` | 50 |
| `deterministic.txn_exists` | 30 |
| `deterministic.amount_match` | 30 |
| `defaults.allowed_currencies` | 20 |
| `deterministic.no_duplicate_refund` | 20 |
| `refund.max_auto_refund_amount` | 20 |
| `dispute.require_approval_actions` | 20 |
| `refund.require_transaction_timestamp` | 20 |
| `refund.max_transaction_age_days` | 20 |

---

## How It Works

Every agent action passes through a three-layer decision pipeline before execution:

```
Agent Request
    │
    ▼
┌──────────────────────────────────────────┐
│  Layer 1: Policy Check                   │
│  rules.yaml — amount limits, action      │
│  types, merchant rules, currencies       │
└──────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────┐
│  Layer 2: Deterministic Ledger Check     │
│  balance state, duplicate detection,     │
│  txn existence, timestamp validation     │
└──────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────┐
│  Layer 3: LLM Reasoning Check            │
│  hallucination detection, intent         │
│  coherence, semantic anomaly scoring     │
└──────────────────────────────────────────┘
    │
    ▼
  ALLOW / BLOCK / REQUIRE_APPROVAL
  + full trace + risk score + audit log
```

---

## Scenario Coverage

| Tool | Adversarial Scenarios |
|---|---|
| `refund` | Over-limit, duplicate, hallucinated order IDs, fake txn references |
| `dispute` | High-value auto-accept, manipulation, approval bypass |
| `route_payment` | Routing override attacks, currency mismatch |
| `reconciliation` | Injection attacks, amount tampering |

---

## Tech Stack

| Layer | Technology |
|---|---|
| API | FastAPI + Pydantic |
| Agent runtime | LangGraph |
| LLM backends | OpenAI / Anthropic (configurable) |
| Dashboard | Streamlit |
| Tests | Pytest + pytest-asyncio |
| Config | YAML rules engine |

---

## Project Structure

```
app/
  main.py              # FastAPI app + routes
  middleware.py        # Request interception
  guarded_runner.py    # Core guardrail orchestration
  policy_engine.py     # YAML rule evaluation
  validators.py        # Deterministic + LLM validators
  ledger.py            # Ledger state management
  schemas.py           # Pydantic models
  rules.yaml           # Financial policy rules
dashboard/
  app.py               # Streamlit decision inspector
simulations/
  scenarios.json       # 330 adversarial test scenarios
  replay.py            # Batch simulation runner
tests/
  test_policy.py
  test_validators.py
  test_guarded_runner.py
```

---

## Quickstart

**1. Clone and set up environment**

```bash
git clone https://github.com/adarshkumar23/I_-Solved_It-FinAgentGuard.git
cd I_-Solved_It-FinAgentGuard
python -m venv .venv
```

Windows:
```bash
.\.venv\Scripts\Activate.ps1
```

Mac/Linux:
```bash
source .venv/bin/activate
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Configure environment**

```bash
cp .env.example .env
```

```env
OPENAI_API_KEY=sk-...
# or
ANTHROPIC_API_KEY=sk-ant-...
```

**4. Run the API** (Terminal 1)

```bash
uvicorn app.main:app --reload
```

**5. Run the dashboard** (Terminal 2)

```bash
streamlit run dashboard/app.py
```

Dashboard at `http://localhost:8501`

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/agent/tool-call` | Guardrailed execution endpoint |
| POST | `/agent/run` | LangGraph path with full guardrail pipeline |

---

## Run Simulations

```bash
python simulations/replay.py
```

Outputs: `simulations/last_report.json` · `simulations/last_outcomes.json` · `logs/guardrail_decisions.jsonl`

---

## Run Tests

```bash
pytest -q
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — contributions welcome, especially new adversarial scenario types and additional payment action validators.

---

## Author

**Adarsh Kumar** — AI/ML Engineer, IIT Patna
[GitHub](https://github.com/adarshkumar23) · [LinkedIn](https://www.linkedin.com/in/adarsh-kumar-50b13a3b3)

---

*Built as part of the [#ISolvedIt](https://www.linkedin.com/in/adarsh-kumar-50b13a3b3) series — identifying and solving real engineering problems at companies building with AI.*
