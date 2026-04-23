# SignalForge — Project Memory

> Read this file at the start of every session. It is the single source of truth for
> what has been built, what the current state is, and what comes next.
> Last updated: 2026-04-23

---

## What This Project Is

**SignalForge** is an automated B2B lead generation and conversion system built for
**Tenacious Consulting and Outsourcing** as part of a 1-week challenge (The Conversion Engine).
The system finds prospective clients from public data, qualifies them against four ICP segments,
runs a 3-email nurture sequence, and books discovery calls with a Tenacious delivery lead.

**Repo:** `d:\Projects\SignalForge`
**Challenge deadline:** Wednesday 2026-04-22 (interim, Acts I–II) and Saturday 2026-04-25 (final, Acts III–V)
**Status as of 2026-04-23:** Acts I and II complete. Acts III–V pending.

---

## Architecture: Five Agents

```
Public Data → ResearchAgent → InsightAgent → MessageAgent → GuardrailAgent → Send
                                                                ↓ (on reply)
                                                        ConversationAgent → Cal.com / SMS
```

| Agent | File | Role |
|---|---|---|
| 1. ResearchAgent | `agent/agents/research_agent.py` | Pulls Crunchbase + layoffs.fyi + Wellfound; runs signal_computer; no LLM |
| 2. InsightAgent | `agent/agents/insight_agent.py` | 1 LLM call → narrative + schema-compliant competitor_gap_brief |
| 3. MessageAgent | `agent/agents/message_agent.py` | 3-email cold sequence; grounded in brief; 120/100/70 word limits |
| 4. GuardrailAgent | `agent/agents/guardrail_agent.py` | PASS/WARN/BLOCK; auto-corrects WARN; drops BLOCK |
| 5. ConversationAgent | `agent/agents/conversation_agent.py` | Webhook reply handler; state machine; Cal.com + SMS |

**Pipeline entry point:** `agent/pipeline.py` — `run_outbound()` and `handle_webhook_reply()`
**Kill-switch:** `OUTBOUND_ENABLED=false` (default) routes all email to `SINK_EMAIL`

---

## Production Stack

| Layer | Tool | Status |
|---|---|---|
| Email (primary) | Resend | Integrated — `agent/email_handler.py` |
| SMS (secondary) | Africa's Talking | Integrated — `agent/sms_handler.py` |
| CRM | HubSpot Developer Sandbox | Integrated — `agent/crm_handler.py` |
| Calendar | Cal.com | Integrated — `agent/calendar_handler.py` |
| Observability | Langfuse 4.x | Integrated — `agent/observability.py` (`@traced`) |
| LLM dev tier | OpenRouter → DeepSeek V3 | `agent/llm_client.py` |
| LLM eval tier | OpenRouter → claude-sonnet-4-6 | Same client, `tier="eval"` |

---

## Data Sources

| Source | File | Records | Notes |
|---|---|---|---|
| Crunchbase ODM | `data/crunchbase_data.csv` | 1,514 companies | Apache 2.0; primary firmographic source |
| layoffs.fyi | `data/layoffs.csv` | 4,360 events | CC-BY; scraper at `scripts/fetch_layoffs_v5.py` |
| Tenacious seed | `data/Tenacious Data/tenacious_sales_data/tenacious_sales_data/` | 25 files | ICP, style guide, bench, pricing, case studies, email templates, schemas |
| Job posts | Wellfound scraper | Live | `agent/enrichment/job_scraper.py`; frozen snapshot via `--snapshot` arg |

**Key seed file path (always use this exact path):**
`data/Tenacious Data/tenacious_sales_data/tenacious_sales_data/seed/`

---

## ICP Segments (fixed names — do not rename)

| # | Enum value | Segment | Key qualifier |
|---|---|---|---|
| 1 | `recently_funded` | Series A/B startup | $5–30M round, last 180d, headcount 15–80, ≥5 open roles |
| 2 | `cost_restructuring` | Mid-market restructure | Layoff last 120d, headcount 200–2000, ≥3 open roles |
| 3 | `leadership_change` | New CTO/VP Eng | Appointment last 90d, headcount 50–500 |
| 4 | `capability_gap` | Specialized AI build | AI maturity ≥ 2, bench-feasible, **NOT pitched at score 0–1** |
| — | `abstain` | No clear segment | confidence_score < 0.6 → generic exploratory email only |

**Classification priority (in order):** Layoff+Funding → Seg2 · Leadership → Seg3 · AI≥2 → Seg4 · Funding → Seg1 · Abstain

**Disqualifiers:** Layoff >15% in last 90d shifts Seg1→Seg2. Layoff >40% disqualifies Seg2 entirely.

---

## Bench Capacity (as of 2026-04-21)

Loaded from seed at runtime by `agent/enrichment/bench_loader.py`.
**Never hardcode or hallucinate these — always call `bench_loader.available_count(stack)`.**

| Stack | Available | Deploy time |
|---|---|---|
| python | 7 | 7 days |
| data | 9 | 7 days |
| frontend | 6 | 7 days |
| ml | 5 | 10 days |
| infra | 4 | 14 days |
| go | 3 | 14 days |
| fullstack_nestjs | 2 | 14 days (Modo Compass through Q3 2026) |

Total on bench: 36. On paid engagements: 26.

---

## tau²-Bench Baseline (Act I — Complete)

| Metric | Value |
|---|---|
| Model | `anthropic/claude-haiku-4-5-20251001` |
| pass@1 | **0.5133** (51.3%) |
| 95% CI | [0.4333, 0.5933] |
| Cost per run | $0.0182 |
| Total cost (150 runs) | $2.73 |
| Latency p50 / p95 | 21.7s / 30.2s |

Files: `eval/score_log.json`, `eval/trace_log.jsonl`, `eval/baseline.md`
Dev slice: 30 tasks × 5 trials. Sealed held-out partition (20 tasks) not yet run.

---

## Session Changes — 2026-04-23

Tenacious seed data integrated. Six files changed:

| File | What changed |
|---|---|
| `agent/models/signals.py` | `UNKNOWN→ABSTAIN`, `confidence_score: float`, `honesty_flags`, `prospect_domain` added |
| `agent/enrichment/bench_loader.py` | **NEW** — loads real bench_summary.json; `check_bench_match()` gates Seg4 |
| `agent/enrichment/signal_computer.py` | ICP priority fixed; qualification filters; disqualifiers; 0.6 abstention gate |
| `agent/agents/insight_agent.py` | Real style markers; schema-compliant `competitor_gap_brief` with `gap_quality_self_check` |
| `agent/agents/message_agent.py` | 3-email sequence (120/100/70w); correct subject patterns; bench from real file |
| `agent/pipeline.py` | `signal_confidence` → float; added `signal_confidence_tier`, `honesty_flags` to result |

All 5 ICP classification rules confirmed passing via unit tests.

---

## What's Next (Acts III–V, Due 2026-04-25)

| Act | Deliverable | Status |
|---|---|---|
| III | `probes/probe_library.md` — 30+ adversarial probes | Not started |
| III | `probes/failure_taxonomy.md` — grouped by category | Not started |
| III | `probes/target_failure_mode.md` — highest-ROI failure with business cost | Not started |
| IV | `method.md` — mechanism design; beat Day-1 baseline with 95% CI separation | Not started |
| IV | `ablation_results.json` + `held_out_traces.jsonl` | Not started |
| V | `memo.pdf` — 2-page decision memo to Tenacious CEO/CFO | Not started |
| V | `evidence_graph.json` — every claim traces to a source | Not started |

**Probe categories required (Act III):** ICP misclassification, signal over-claiming, bench
over-commitment, tone drift, multi-thread leakage, cost pathology, dual-control coordination,
scheduling edge cases (EU/US/East Africa), signal reliability with false-positive rates, gap
over-claiming.

---

## Key Constraints — Never Violate

1. `OUTBOUND_ENABLED=false` by default. All outbound routes to `SINK_EMAIL` unless explicitly enabled.
2. Every prospect in the challenge week is **synthetic**. No real Tenacious customer data.
3. Agent must never quote ACV totals not in `seed/baseline_numbers.md` (fabrication = disqualifying violation).
4. Agent must never commit to bench capacity not shown in `seed/bench_summary.json`.
5. Segment 4 pitch is only valid at AI maturity ≥ 2. Pitching Seg4 to a score-0 prospect damages brand.
6. Email sequence max: 3 emails per prospect within 30 days. A 4th touch is a policy violation.
7. All email/SMS output must carry `"draft": True` metadata until reviewed.
8. Case studies: only AdTech, Loyalty Platform, Fitness Franchise. Do not fabricate additional cases.
