# Failure Taxonomy — SignalForge / Tenacious Outbound Agent

> Every probe from `probe_library.md` is grouped below. No orphan probes; no double-counting.
> Aggregate trigger rate = mean across all probes in the category (50 adversarial runs per probe).
> Pattern description captures the shared root cause within each category.

---

## Taxonomy Table

| # | Category | Probes | Aggregate Trigger Rate | Pattern Description |
|---|----------|--------|----------------------|---------------------|
| 1 | ICP Misclassification | PL-001–PL-005 | **21.6%** | Threshold boundary errors and classification priority violations push prospects into the wrong ICP segment, resulting in a pitch that mismatches the buyer's actual moment. |
| 2 | Hiring-Signal Over-Claiming | PL-006–PL-009 | **19.0%** | Stale or duplicated job-post data inflates hiring-velocity signals, causing the agent to state headcount or role-count claims that the prospect can immediately contradict. |
| 3 | Bench Over-Commitment | PL-010–PL-013 | **11.3%** | Capacity data not refreshed between pipeline runs or at reply-handling time allows the agent to promise bench availability that does not exist at the moment of delivery. |
| 4 | Tone Drift | PL-014–PL-016 | **13.3%** | GuardrailAgent passes emails that contain style-guide violations (filler affirmations, wrong-segment pitch language, word-count overruns) because the checks fire on keywords rather than semantic alignment. |
| 5 | Multi-Thread Leakage | PL-017–PL-019 | **6.3%** | Concurrent pipeline runs for contacts at the same company, or alias-email mismatches in HubSpot, cause two independent sequences to reach the same account with no awareness of each other. |
| 6 | Cost Pathology | PL-020–PL-022 | **11.7%** | Unbounded enrichment loops, over-retry correction cycles, and concurrent Playwright instances consume disproportionate compute and can exhaust weekly LLM or memory budgets. |
| 7 | Dual-Control Coordination | PL-023–PL-025 | **7.3%** | Asynchronous webhook handlers and cron nudges fire without locking on prospect state, producing contradictory simultaneous outbound messages to the same contact. |
| 8 | Scheduling Edge Cases | PL-026–PL-029 | **15.0%** | Timezone conversion errors (EAT ↔ CET, EAT ↔ EST), misapplied overlap-policy language to same-timezone prospects, and unblocked Ethiopian public holidays produce booking slots that cause friction or no-shows. |
| 9 | Signal Reliability | PL-030–PL-032 | **17.7%** | Crunchbase name collisions, stale layoffs.fyi date formats, and unimplemented signal inputs (GitHub activity) produce false-positive and false-negative enrichment records that are silently propagated into email content. |
| 10 | Gap Over-Claiming | PL-033–PL-035 | **21.7%** | The LLM in InsightAgent generalises from one or two weak evidence points to broad competitive-trend claims; the `gap_quality_self_check` field is present but not enforced as a blocking condition. |

---

## Per-Category Detail

### Category 1 — ICP Misclassification
**Probes:** PL-001, PL-002, PL-003, PL-004, PL-005  
**Aggregate Trigger Rate:** 21.6% (individual rates: 18%, 24%, 31%, 14%, 22%)  
**Shared Failure Pattern:** The classification pipeline applies thresholds as point comparisons (e.g., `funding >= 5M`) without range or uncertainty handling. Values at the boundary (4.8M, 81 headcount) pass or fail deterministically based on noisy input data rather than a confidence band. Priority-ordering logic (layoff+funding → Seg2) is implemented but bypassed when the layoff check uses a cached result from an earlier pipeline stage.  
**Distinguishing Feature from Other Categories:** These failures are in the *classification decision*, before any message is generated. A correct enrichment with a bad classification gate produces this pattern; incorrect enrichment data is Category 9 (Signal Reliability).

---

### Category 2 — Hiring-Signal Over-Claiming
**Probes:** PL-006, PL-007, PL-008, PL-009  
**Aggregate Trigger Rate:** 19.0% (individual rates: 29%, 17%, 21%, 9%)  
**Shared Failure Pattern:** Job-post signals are assembled from multiple sources (Wellfound, BuiltIn, company careers page) and optionally served from a file-system snapshot. Neither the deduplication layer nor the freshness check is enforced as a hard gate; stale or duplicated data passes silently into the `HiringSignal` struct. The AI-role-fraction computation amplifies a single stale posting when total open roles are low.  
**Distinguishing Feature from Other Categories:** Failures here are in *hiring-specific* signal assembly, specifically the job-post and AI-role inputs. Funding and layoff signal errors are Category 9 (Signal Reliability) when caused by data-source quality, and Category 1 (ICP Misclassification) when caused by classification logic.

---

### Category 3 — Bench Over-Commitment
**Probes:** PL-010, PL-011, PL-012, PL-013  
**Aggregate Trigger Rate:** 11.3% (individual rates: 12%, 7%, 15%, 11%)  
**Shared Failure Pattern:** `bench_loader.available_count()` is called once at pipeline-run time and its result is cached in the pipeline result dict. The cache is not invalidated by other concurrent pipeline runs or by the passage of time. Reply-handling (ConversationAgent) uses the cached value from the initial run, not a live check. The Modo Compass constraint (fullstack_nestjs committed through Q3 2026) is stored as a comment in the JSON, not as a structured field; the loader does not parse it.  
**Distinguishing Feature from Other Categories:** Bench failures occur specifically when *capacity claims* are made in message generation or reply handling. Over-commitment errors that occur before any message is generated (e.g., Seg4 qualification gating) would be Category 1.

---

### Category 4 — Tone Drift
**Probes:** PL-014, PL-015, PL-016  
**Aggregate Trigger Rate:** 13.3% (individual rates: 8%, 13%, 19%)  
**Shared Failure Pattern:** GuardrailAgent runs keyword-level checks (presence of banned phrases, word-count threshold) but does not apply semantic or segment-coherence checks. Filler affirmations that are not in the banned-phrase list pass. Pitch-language segment mismatch (Seg1 language in a Seg2 email) passes because no check compares the email body's semantic register against the assigned ICP segment.  
**Distinguishing Feature from Other Categories:** These failures are in *output quality* — a correctly classified, correctly enriched prospect still receives a poorly framed email. They would not be caught by improving classification (Cat 1) or enrichment (Cat 9).

---

### Category 5 — Multi-Thread Leakage
**Probes:** PL-017, PL-018, PL-019  
**Aggregate Trigger Rate:** 6.3% (individual rates: 6%, 4%, 9%)  
**Shared Failure Pattern:** The pipeline has no account-level deduplication layer; it operates on contact-level identifiers only. HubSpot upsert uses email as the primary key, so alias emails and group inboxes create duplicate contact records. No lock or mutex is applied at the company-domain level before initiating a new outbound sequence.  
**Distinguishing Feature from Other Categories:** Failures here arise from *account-level coordination* failures between separate pipeline invocations. Dual-control failures (Category 7) occur within a single prospect's thread from concurrent webhooks.

---

### Category 6 — Cost Pathology
**Probes:** PL-020, PL-021, PL-022  
**Aggregate Trigger Rate:** 11.7% (individual rates: 16%, 11%, 8%)  
**Shared Failure Pattern:** Unbounded iteration is a common factor: `load_companies_by_industry(limit=40)` with broad industry names, GuardrailAgent correction loop without hard exit on "close enough," and Playwright parallelism without a concurrency cap. Each failure multiplies cost or resource consumption by the degree of unboundedness.  
**Distinguishing Feature from Other Categories:** Cost failures do not necessarily produce wrong outputs — the email content may be correct. The failure is in *resource consumption* that prevents the system from running at production volume.

---

### Category 7 — Dual-Control Coordination
**Probes:** PL-023, PL-024, PL-025  
**Aggregate Trigger Rate:** 7.3% (individual rates: 7%, 10%, 5%)  
**Shared Failure Pattern:** Multiple async handlers (reply webhook, cron nudge, booking confirmation callback, SMS reply callback) can fire against the same prospect simultaneously. The ConversationAgent does not acquire a per-contact lock before generating a response; state writes back to HubSpot may not be visible to a concurrently-executing handler.  
**Distinguishing Feature from Other Categories:** Dual-control failures are *intra-thread* concurrency issues for a single prospect. Multi-thread leakage (Category 5) is *inter-thread* (different prospects or different contacts at the same account).

---

### Category 8 — Scheduling Edge Cases
**Probes:** PL-026, PL-027, PL-028, PL-029  
**Aggregate Trigger Rate:** 15.0% (individual rates: 22%, 19%, 13%, 6%)  
**Shared Failure Pattern:** Cal.com integration exposes slots in EAT (Tenacious's timezone) without converting to the prospect's local time in the email confirmation. The overlap-hours talking point (3–5 hours/day) is applied as a templated sentence regardless of whether the prospect is in a far timezone (US) or the same timezone (East Africa). Ethiopian public holidays are not blocked in the Cal.com event type.  
**Distinguishing Feature from Other Categories:** Scheduling failures result in *booking-stage friction* that occurs after the reply has been received and the prospect is warm. This is distinct from ICP classification (pre-send) and signal enrichment (also pre-send).

---

### Category 9 — Signal Reliability
**Probes:** PL-030, PL-031, PL-032  
**Aggregate Trigger Rate:** 17.7% (individual rates: 16%, 23%, 14%)  
**Shared Failure Pattern:** All three failures share an unverified assumption about the data source: that Crunchbase names are unique identifiers, that layoffs.fyi dates are parseable in a standard format, and that all 6 AI-maturity signal inputs are available. None of these assumptions holds universally. The enrichment pipeline does not surface data-quality warnings; silent fallback means the pipeline "succeeds" with low-quality data.  
**Distinguishing Feature from Other Categories:** Signal reliability failures are at the *data layer*, before any classification or message generation. A signal-reliability failure can cause classification failures (Cat 1), hiring-signal over-claiming (Cat 2), or gap over-claiming (Cat 10) as downstream effects. The root is always in the data, not the classification logic.

---

### Category 10 — Gap Over-Claiming
**Probes:** PL-033, PL-034, PL-035  
**Aggregate Trigger Rate:** 21.7% (individual rates: 28%, 20%, 17%)  
**Shared Failure Pattern:** InsightAgent's LLM prompt instructs the model to generate gap findings from structured peer data, but does not enforce a minimum evidence count per finding at the prompt level. `gap_quality_self_check` is present in the output schema but its value is not checked by the calling code; a self-check of "low" does not trigger a retry or a downgrade. The sparse-sector fallback path (`_empty_gap_brief()`) correctly returns a no-finding brief, but the LLM generation path is still called with partial peer data in certain edge cases (PL-035).  
**Distinguishing Feature from Other Categories:** Gap over-claiming is specifically about *LLM-generated competitive intelligence* exceeding what the structured evidence supports. Signal reliability failures (Cat 9) corrupt the inputs; gap over-claiming failures are in the LLM's inference over those inputs.

---

## Coverage Verification

| Probe | Category Assigned | Verified No Double-Count |
|-------|------------------|--------------------------|
| PL-001 | ICP Misclassification | ✓ |
| PL-002 | ICP Misclassification | ✓ |
| PL-003 | ICP Misclassification | ✓ |
| PL-004 | ICP Misclassification | ✓ |
| PL-005 | ICP Misclassification | ✓ |
| PL-006 | Hiring-Signal Over-Claiming | ✓ |
| PL-007 | Hiring-Signal Over-Claiming | ✓ |
| PL-008 | Hiring-Signal Over-Claiming | ✓ |
| PL-009 | Hiring-Signal Over-Claiming | ✓ |
| PL-010 | Bench Over-Commitment | ✓ |
| PL-011 | Bench Over-Commitment | ✓ |
| PL-012 | Bench Over-Commitment | ✓ |
| PL-013 | Bench Over-Commitment | ✓ |
| PL-014 | Tone Drift | ✓ |
| PL-015 | Tone Drift | ✓ |
| PL-016 | Tone Drift | ✓ |
| PL-017 | Multi-Thread Leakage | ✓ |
| PL-018 | Multi-Thread Leakage | ✓ |
| PL-019 | Multi-Thread Leakage | ✓ |
| PL-020 | Cost Pathology | ✓ |
| PL-021 | Cost Pathology | ✓ |
| PL-022 | Cost Pathology | ✓ |
| PL-023 | Dual-Control Coordination | ✓ |
| PL-024 | Dual-Control Coordination | ✓ |
| PL-025 | Dual-Control Coordination | ✓ |
| PL-026 | Scheduling Edge Cases | ✓ |
| PL-027 | Scheduling Edge Cases | ✓ |
| PL-028 | Scheduling Edge Cases | ✓ |
| PL-029 | Scheduling Edge Cases | ✓ |
| PL-030 | Signal Reliability | ✓ |
| PL-031 | Signal Reliability | ✓ |
| PL-032 | Signal Reliability | ✓ |
| PL-033 | Gap Over-Claiming | ✓ |
| PL-034 | Gap Over-Claiming | ✓ |
| PL-035 | Gap Over-Claiming | ✓ |

**Total:** 35 probes, 10 categories, 0 orphans, 0 double-counts.
