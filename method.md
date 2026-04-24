# Mechanism Design — Bench Validation Gate

**Target failure mode:** Bench Over-Commitment (Category 3, `probes/target_failure_mode.md`)  
**Root cause addressed:** `bench_loader.BENCH` is loaded as a module-level singleton at import time and never refreshed during a process lifetime; `GuardrailAgent` uses a hardcoded `_DEFAULT_BENCH` dict that is not connected to `bench_loader` at all.  
**Aggregate trigger rate (pre-mechanism):** 11.3% across probes PL-010–PL-013

---

## 1. What the Mechanism Does

The **Multi-Stage Bench Validation Gate** replaces the current "load once at import" pattern with a TTL-based cache that is re-evaluated at three specific pipeline stage gates:

1. **Pre-send gate (MessageAgent → GuardrailAgent):** Before any capacity claim is written into an outbound email, `bench_loader.reload_if_stale()` is called. If the cache is older than `BENCH_TTL_SECONDS`, it re-reads `bench_summary.json` from disk. The fresh bench dict is passed to `GuardrailAgent.check()` as a parameter (replacing the hardcoded `_DEFAULT_BENCH`).

2. **Reply-handling gate (ConversationAgent):** When `ConversationAgent.handle_reply()` processes an inbound email or SMS that mentions capacity, stack requirements, or headcount, `bench_loader.reload_if_stale()` is called again before generating any response that references engineers.

3. **Confirmation gate (post-booking):** After `calendar_handler.book_discovery_call()` succeeds, the pipeline calls `bench_loader.reload_if_stale()` a final time and writes the refreshed bench snapshot into the HubSpot contact note. This anchors the confirmed capacity claim to a timestamp and the actual bench state at booking time.

In all three gates, if `check_bench_match(required_stacks)` returns `bench_available: False` (any required stack has zero available engineers), the mechanism triggers a **segment fallback**: a Seg4 prospect is re-classified as Seg1 if they also have funding signal, or to `abstain` if no fallback segment is available. The email is regenerated for the fallback segment without any capacity claim.

---

## 2. Re-Implementation Instructions

An engineer can re-implement this mechanism from the following steps:

### 2a. Add TTL-based refresh to `bench_loader.py`

Add two module-level variables and a refresh function:

```python
import time

_LOADED_AT: float = 0.0       # epoch seconds of last load
BENCH_TTL_SECONDS: int = 300  # hyperparameter — see Section 3

def reload_if_stale() -> dict:
    global BENCH, _LOADED_AT
    now = time.time()
    if now - _LOADED_AT > BENCH_TTL_SECONDS:
        BENCH = _load()
        _LOADED_AT = now
    return BENCH
```

Change the module-level singleton initialisation:

```python
# Before:  BENCH: dict = _load()
# After:
BENCH: dict = _load()
_LOADED_AT: float = time.time()
```

### 2b. Remove `_DEFAULT_BENCH` from `guardrail_agent.py`

Delete lines 35–41 of `guardrail_agent.py` (the hardcoded `_DEFAULT_BENCH` dict).

Change the `GuardrailAgent.__init__` signature to accept a `bench` dict explicitly:

```python
from agent.enrichment.bench_loader import reload_if_stale

def __init__(self, llm_tier: str = "dev", bench: Optional[dict] = None):
    self.llm_tier = llm_tier
    self.bench = bench if bench is not None else reload_if_stale().get("stacks", {})
```

### 2c. Add reload calls at the three stage gates

**In `agent/agents/message_agent.py`** (pre-send gate), before calling `GuardrailAgent.check()`:
```python
from agent.enrichment.bench_loader import reload_if_stale
fresh_bench = reload_if_stale()
guardrail = GuardrailAgent(bench=fresh_bench.get("stacks", {}))
verdict = guardrail.check(draft_email, brief_dict=brief_dict)
```

**In `agent/agents/conversation_agent.py`** (reply-handling gate), at the top of `handle_reply()`:
```python
from agent.enrichment.bench_loader import reload_if_stale, check_bench_match
reload_if_stale()
if "stacks" in prospect_requirements:
    capacity = check_bench_match(prospect_requirements["stacks"])
    if not capacity["bench_available"]:
        return self._fallback_to_segment(contact_id, company, insight, capacity)
```

**In `agent/pipeline.py`** (confirmation gate), after `book_discovery_call()` succeeds:
```python
from agent.enrichment.bench_loader import reload_if_stale, bench_description
reload_if_stale()
bench_snapshot = bench_description()
crm_handler.log_email_event(contact_id, "discovery_call_booked",
                             extra_note=f"Bench at booking time: {bench_snapshot}")
```

### 2d. Add `_fallback_to_segment()` to `ConversationAgent`

```python
def _fallback_to_segment(self, contact_id, company, insight, capacity_result) -> dict:
    """Downgrade Seg4 to Seg1 or abstain when bench gap detected."""
    gaps = capacity_result.get("gaps", [])
    has_funding = bool(insight.get("funding_signal"))
    fallback_segment = "recently_funded" if has_funding else "abstain"
    note = (
        f"Bench insufficient for {gaps} at reply time. "
        f"Downgraded to {fallback_segment}. Bench state: {capacity_result['counts']}."
    )
    return {
        "action": "segment_fallback",
        "fallback_segment": fallback_segment,
        "bench_gap": gaps,
        "note": note,
    }
```

---

## 3. Hyperparameters

| Parameter | Value | Where Set | Effect |
|---|---|---|---|
| `BENCH_TTL_SECONDS` | `300` | `bench_loader.py`, module-level | Cache age (seconds) after which `reload_if_stale()` reads disk. 300s chosen to be shorter than any reasonable pipeline concurrency window while avoiding per-call disk I/O. |
| `BENCH_MIN_BUFFER` | `1` | `check_bench_match()` call in conversation_agent | Minimum available engineers above zero before a capacity claim is allowed. Set to 1 to account for parallel pipeline runs; prevents committing the last available engineer. Applied as: `n >= BENCH_MIN_BUFFER` rather than `n > 0`. |
| `MAX_CORRECTION_LOOPS` | `3` | `guardrail_agent.py`, auto-correct loop | Maximum number of LLM regeneration attempts when WARN verdict is returned. Existing parameter; unchanged by this mechanism. |
| `BENCH_OVER_CLAIM_BLOCKING_PATTERNS` | `["always available", "guarantee", "will definitely", "ensures availability"]` | `guardrail_agent.py`, `_OVER_CLAIM_PATTERNS` | Patterns that trigger BLOCK (not WARN) on bench claims. Existing list augmented with `"always available"` per `_OVER_CLAIM_PATTERNS` already in code. |
| `SEGMENT_FALLBACK_ORDER` | `["recently_funded", "abstain"]` | `ConversationAgent._fallback_to_segment()` | Priority order for fallback segment when Seg4 bench check fails. Seg1 is preferred if funding signal is present; `abstain` otherwise. |

---

## 4. Ablation Variants

### Variant A — No Gate (Baseline)

**What is removed:** All three `reload_if_stale()` calls are omitted. `GuardrailAgent` uses the hardcoded `_DEFAULT_BENCH` dict. `ConversationAgent` does not call `check_bench_match()`.

**What this tests:** Whether the module-level singleton + hardcoded bench dict (current state) is materially different from the full mechanism. This is the Day-1 baseline condition.

**Expected outcome:** Bench over-commitment trigger rate remains at ~11.3%. Capacity claims in email and reply-handling reflect bench state at pipeline-startup time, not at message-generation time.

**Contrast with main:** Variant A has no synchronisation between concurrent pipeline runs; Variant A's GuardrailAgent bench is never connected to `bench_summary.json` state.

---

### Variant B — TTL Refresh Only (No GuardrailAgent Integration)

**What is changed:** `reload_if_stale()` is added to `bench_loader.py` and called at the pre-send gate and reply-handling gate. However, `GuardrailAgent` is not updated — it still uses its own `_DEFAULT_BENCH` dict (not the refreshed bench from `bench_loader`).

**What this tests:** Whether the TTL refresh alone reduces over-commitment, or whether the GuardrailAgent integration (replacing `_DEFAULT_BENCH`) is necessary. Isolates the value of fixing the two bugs independently.

**Expected outcome:** Reply-handling gate failures (PL-013) are partially addressed because `ConversationAgent` uses fresh bench data. Pre-send gate failures (PL-010, PL-012) are NOT addressed because GuardrailAgent's bench check still uses the stale hardcoded dict. Predicted trigger rate reduction: ~40% of full mechanism benefit.

**Contrast with main:** Variant B eliminates only one of the two root causes (module-level singleton in `bench_loader`) without fixing the second (disconnected `_DEFAULT_BENCH` in `guardrail_agent`).

---

### Variant C — Hard Block Without Segment Fallback

**What is changed:** All three stage gates are implemented with TTL refresh and GuardrailAgent integration. However, `_fallback_to_segment()` is removed; when `check_bench_match()` returns `bench_available: False`, the pipeline returns `{"action": "drop", "reason": "bench_insufficient"}` with no email sent and no fallback segment attempted.

**What this tests:** Whether the segment fallback (Seg4 → Seg1 → abstain) adds pipeline value, or whether a hard drop is equally effective at preventing over-commitment at lower implementation cost.

**Expected outcome:** Over-commitment trigger rate falls to near-zero (same as full mechanism). However, the pipeline will drop Seg4 prospects who have funding signal, losing them entirely instead of recapturing them as Seg1. Expected loss: ~30% of Seg4 threads that would have converted as Seg1 under the full mechanism.

**Contrast with main:** Variant C is equally effective at preventing bench over-commitment but destroys pipeline value by discarding convertible leads. The full mechanism is better on ROI despite higher implementation complexity.

---

## 5. Statistical Test Plan

**Goal:** Determine whether the full mechanism (main) produces a statistically significant reduction in bench over-commitment trigger rate compared to Variant A (no gate / baseline).

**Dataset:** The sealed held-out partition of 20 tasks from the tau^2-Bench retail task suite, run with 5 trials each (100 runs total), using `claude-sonnet-4-6` (eval tier). The held-out partition is sealed and has not been run during mechanism development.

**Test:** McNemar's test on paired binary outcomes (bench-over-commitment event: yes/no) across the 20 held-out tasks, run once under Variant A conditions and once under the full mechanism. McNemar's test is appropriate because outcomes are paired (same task set, same eval infrastructure) and binary.

**Comparison:** `p(over-commitment | Variant A)` vs `p(over-commitment | full mechanism)`, one-sided (mechanism is expected to reduce, not increase, trigger rate).

**p-value threshold:** p < 0.05 (one-sided). The test is powered at n = 100 runs; at an expected pre-mechanism rate of 11.3% and a target post-mechanism rate of ≤ 2%, power is approximately 0.80 at p < 0.05.

**Secondary metric:** Pass@1 on the held-out partition (same metric as the Day-1 baseline: 0.5133, 95% CI [0.4333, 0.5933]). The mechanism should not reduce overall pass@1; if the full mechanism's pass@1 95% CI lower bound exceeds 0.5133, the mechanism is confirmed to improve or maintain task completion without regression.

**Result storage:** Held-out run outputs are saved to `eval/held_out_traces.jsonl` and summary statistics to `eval/ablation_results.json` with keys `variant_a`, `variant_b`, `variant_c`, `full_mechanism`, each containing `pass_at_1`, `ci_95`, `bench_over_commitment_rate`, and `mcnemar_p`.
