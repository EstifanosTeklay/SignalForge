# SignalForge

Automated B2B outbound system for **Tenacious Consulting & Outsourcing**. Detects hiring-intent signals, enriches prospects, generates personalised cold emails, handles replies, and books discovery calls — all without human intervention.

---

## Architecture

```
  Public Data Sources
  ┌──────────────────────────────────────────────────────────────────┐
  │  Crunchbase ODM CSV  ·  layoffs.fyi (4,360 records)  ·  Wellfound│
  │  BuiltIn  ·  LinkedIn public pages                               │
  └───────────────────────────┬──────────────────────────────────────┘
                              │ enrichment
  ┌───────────────────────────▼──────────────────────────────────────┐
  │  1. ResearchAgent  (agent/agents/research_agent.py)              │
  │  Outputs: HiringSignalBrief — funding / layoff / leadership /    │
  │           AI-maturity / ICP segment / confidence score           │
  │  ──────────────────────── writes ──────────────────────────────► │ HubSpot CRM
  │  (upsert_contact, enrichment_timestamp, icp_segment)            │ (contact created,
  └───────────────────────────┬──────────────────────────────────────┘  enrichment logged)
                              │ brief
  ┌───────────────────────────▼──────────────────────────────────────┐
  │  2. InsightAgent  (agent/agents/insight_agent.py)                │
  │  LLM call → narrative + competitor_gap_brief + pitch angle       │
  │  Backbone LLM: OpenRouter → deepseek-v3 (dev)                   │
  │                           → claude-sonnet-4-6 (eval)            │
  │  All LLM calls traced ─────────────────────────────────────────► │ Langfuse 4.x
  └───────────────────────────┬──────────────────────────────────────┘  (observability)
                              │ insight + draft
  ┌───────────────────────────▼──────────────────────────────────────┐
  │  3. MessageAgent  (agent/agents/message_agent.py)                │
  │  Produces 3-email sequence (120 / 100 / 70 word limits)         │
  │  Kill-switch: OUTBOUND_ENABLED=false → routes to SINK_EMAIL      │
  └───────────────────────────┬──────────────────────────────────────┘
                              │ draft email
  ┌───────────────────────────▼──────────────────────────────────────┐
  │  4. GuardrailAgent  (agent/agents/guardrail_agent.py)            │
  │  Verdict: PASS / WARN (auto-correct) / BLOCK (drop + regen)     │
  │  Checks: tone · claim honesty · bench availability              │
  │  All verdicts traced ──────────────────────────────────────────► │ Langfuse 4.x
  └───────────────────────────┬──────────────────────────────────────┘
                              │ approved email
  ┌───────────────────────────▼──────────────────────────────────────┐
  │  email_handler.py  (Resend)                                      │
  │  send_email() + handle_reply_webhook()                          │
  │  ──────────────────────── writes ──────────────────────────────► │ HubSpot CRM
  └───────────────────────────┬──────────────────────────────────────┘  (email_sent event)
                              │ on_reply callback
  ┌───────────────────────────▼──────────────────────────────────────┐
  │  5. ConversationAgent  (agent/agents/conversation_agent.py)      │
  │  State machine: COLD→REPLIED→QUALIFIED→BOOKED→STALLED→CLOSED    │
  │  Handles reply webhooks + SMS inbound (Africa's Talking)        │
  │  ──────────────── book_discovery_call() ───────────────────────► │ Cal.com
  │  (get_available_slots, generate booking link, confirm booking)  │ (booking link
  │  ──────────────── send_sms() [warm leads only] ────────────────► │  in email + SMS)
  │  ──────────────── writes ──────────────────────────────────────► │ HubSpot CRM
  │  (qualification answers, call_booked, thread_state updates)     │
  │  All state transitions traced ─────────────────────────────────► │ Langfuse 4.x
  └──────────────────────────────────────────────────────────────────┘

  Channel Handlers (supporting services)
  ┌──────────────────────────────────────────────────────────────────┐
  │  email_handler.py     Resend API — send + reply webhook          │
  │  sms_handler.py       Africa's Talking — send + inbound webhook  │
  │                       warm-lead gate: only after email reply     │
  │  crm_handler.py       HubSpot — upsert_contact, log_email_event  │
  │  calendar_handler.py  Cal.com — get_slots, book_discovery_call   │
  │  observability.py     Langfuse 4.x — @observe decorator,        │
  │                       thread-safe across parallel agent runs     │
  └──────────────────────────────────────────────────────────────────┘
```

---

## Repository Layout

```
SignalForge/
├── agent/
│   ├── agents/
│   │   ├── research_agent.py       # Agent 1 — signal extraction
│   │   ├── insight_agent.py        # Agent 2 — narrative + competitor gap
│   │   ├── message_agent.py        # Agent 3 — email generation
│   │   ├── guardrail_agent.py      # Agent 5 — PASS/WARN/BLOCK
│   │   └── conversation_agent.py   # Agent 4 — reply handling + booking
│   ├── enrichment/
│   │   ├── crunchbase_loader.py    # Crunchbase ODM CSV parser
│   │   ├── layoffs_parser.py       # layoffs.fyi CSV lookup (4,360 records)
│   │   └── signal_computer.py      # ICP segment + AI-maturity scoring
│   ├── models/
│   │   ├── company.py              # Company, FundingInfo, LayoffInfo
│   │   └── signals.py              # HiringSignalBrief, ICPClassification
│   ├── pipeline.py                 # Orchestrator — run_outbound(), handle_webhook_reply()
│   ├── email_handler.py            # Resend integration
│   ├── sms_handler.py              # Africa's Talking integration
│   ├── crm_handler.py              # HubSpot upsert_contact / log_email_event
│   ├── calendar_handler.py         # Cal.com get_slots / book_discovery_call
│   ├── llm_client.py               # OpenRouter wrapper (dev + eval tiers)
│   └── observability.py            # Langfuse 4.x @observe decorator
├── data/
│   ├── layoffs.csv                 # 4,360 real records scraped from layoffs.fyi
│   ├── crunchbase_data.csv         # Crunchbase ODM export
│   └── bench_summary.json          # Available bench capacity by stack
├── eval/
│   ├── tau2-bench/                 # tau^2-Bench harness (cloned)
│   ├── score_log.json              # pass@1, 95% CI, cost, latency
│   ├── trace_log.jsonl             # Full trajectories across all dev trials
│   └── baseline.md                 # Reproduction report (<=400 words)
├── probes/
│   ├── probe_library.md            # 35 adversarial probes across 10 failure categories
│   ├── failure_taxonomy.md         # Probes grouped by category with aggregate trigger rates
│   └── target_failure_mode.md      # Highest-ROI failure selected with business-cost arithmetic
├── scripts/
│   ├── fetch_layoffs_v5.py         # Playwright scraper — intercepts Airtable API
│   └── fetch_layoffs.py            # Fallback scraper
├── method.md                       # Mechanism design: bench validation gate + ablations + test plan
├── requirements.txt
└── README.md
```

---

## Quick Start

### 1. Prerequisites

- Python 3.11+
- [Playwright browsers](https://playwright.dev/python/docs/intro): `playwright install chromium`

### 2. Clone and install

```bash
git clone https://github.com/<your-handle>/SignalForge.git
cd SignalForge
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure environment

Copy `.env.example` and fill in your keys:

```bash
cp .env.example .env
```

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | LLM calls (dev: deepseek, eval: claude-sonnet-4-6) |
| `RESEND_API_KEY` | Outbound email |
| `RESEND_FROM_EMAIL` | Verified sender domain |
| `AFRICAS_TALKING_API_KEY` + `_USERNAME` | SMS (warm leads) |
| `HUBSPOT_API_KEY` | CRM upsert + activity logging |
| `CALCOM_API_KEY` + `CALCOM_EVENT_TYPE_ID` | Discovery call booking |
| `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` | Observability tracing |
| `OUTBOUND_ENABLED` | `false` = route all email to `SINK_EMAIL` |
| `SINK_EMAIL` | Safety sink for test runs |
| `CRUNCHBASE_CSV` | Path to Crunchbase ODM CSV |
| `LAYOFFS_CSV` | Path to layoffs CSV (default: `data/layoffs.csv`) |

### 4. Refresh layoffs data

```bash
python scripts/fetch_layoffs_v5.py
```

Intercepts Airtable's `readSharedViewData` API (view `viwN3RMGptp84mfag`) and writes `data/layoffs.csv` (4,360 records).

### 5. Run the pipeline

```python
from agent.pipeline import run_outbound

result = run_outbound(
    company_name="Acme Corp",
    prospect_email="decision.maker@acme.com",
    prospect_name="Alex Johnson",
    prospect_role="VP Engineering",
    wellfound_slug="acme-corp",   # optional
)
print(result)
```

### 6. Handle reply webhooks

Point your Resend webhook to a FastAPI endpoint calling:

```python
from agent.pipeline import handle_webhook_reply
result = handle_webhook_reply(webhook_payload, contact_id, thread_state, insight)
```

### 7. Run tau^2-Bench evaluation

```bash
cd eval/tau2-bench
python simulate_5trials.py     # regenerate score_log + trace_log
```

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| OpenRouter with two tiers | Dev tier (deepseek) for iteration speed and cost; eval tier (claude-sonnet-4-6) for grading |
| Kill-switch `OUTBOUND_ENABLED=false` | All email routes to `SINK_EMAIL` — no accidental production sends during development |
| Deterministic fallback on LLM failure | Pipeline never breaks; guardrail still runs; HubSpot still logs |
| Langfuse 4.x `@observe` decorator | Thread-safe across parallel agent runs; no shared trace state |
| layoffs.fyi scraper via route rewriting | Airtable returns gzip-msgpack by default; stripping `x-airtable-accept-msgpack` forces JSON |

---

## Requirements

See [requirements.txt](requirements.txt).
