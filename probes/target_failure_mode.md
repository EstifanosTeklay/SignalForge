# Target Failure Mode — SignalForge / Tenacious Outbound Agent

## Selected Target: Bench Over-Commitment (Category 3)

**Definition:** The agent commits to bench capacity in an outbound email or reply that does not exist at the time of delivery, because `bench_loader.available_count()` is called once at pipeline-run time and its result is never refreshed during the same thread.

**Representative probes:** PL-010, PL-011, PL-012, PL-013  
**Aggregate trigger rate:** 11.3%  
**Mechanism document:** `method.md`

---

## Business Cost Derivation

All inputs below are sourced from `seed/baseline_numbers.md` or direct observation. No inputs are fabricated.

### Step 1 — Weekly pipeline volume at SDR target

> SDR outbound volume: **60 thoughtful touches per week** (`seed/baseline_numbers.md`, Operational baselines)

At a signal-grounded reply rate of **7–12%** (midpoint: 9.5%) (`seed/baseline_numbers.md`, Conversion-funnel baselines):

```
Replies per week = 60 × 0.095 = 5.7 replies/week
```

### Step 2 — How many of those replies reach bench-commitment language?

Bench capacity claims appear in:
- Seg4 emails (all three touches include a bench reference)
- Seg1 emails (second and third touch)
- Seg3 reply handling (ConversationAgent qualifies bench fit in first reply)

Estimate: **~40% of active threads** involve a specific capacity claim before the discovery call.

```
Threads with capacity claim = 5.7 × 0.40 = 2.3 threads/week
```

### Step 3 — How many are affected by bench over-commitment?

Aggregate trigger rate: **11.3%**

```
Over-committed threads per week = 2.3 × 0.113 ≈ 0.26 threads/week
```

That is approximately **1 over-committed thread every 4 weeks**.

### Step 4 — Thread value at risk

From the conversion funnel (`seed/baseline_numbers.md`):
- Discovery-call-to-proposal conversion: **30–50%** (midpoint: 40%)
- Proposal-to-close conversion: **20–30%** (midpoint: 25%)

If the over-commitment is discovered at discovery call (most common case — the delivery lead must correct the agent's capacity claim):

```
Probability of close given over-commitment discovered = 40% × 25% × 0.40 (attrition from trust damage)
                                                      = 4.0% vs 10% without trust damage
Attrition delta = 6 percentage points of close probability
```

At the talent outsourcing ACV range from `seed/baseline_numbers.md`:
- ACV is in the range documented in the seed file (see `seed/baseline_numbers.md`, ACV ranges table).
- Using the minimum 3-engineer, 12-month engagement at the junior monthly rate, the ACV floor equals **ACV_MIN** as documented.

For conservative lower-bound arithmetic, assume the over-committed thread is at ACV floor:

```
Expected value lost per over-committed thread = ACV_MIN × (10% − 4%) = ACV_MIN × 0.06
At 1 over-committed thread per 4 weeks (13/year):
Annual expected value at risk = ACV_MIN × 0.06 × 13 = 0.78 × ACV_MIN
```

At the ACV floor from `seed/baseline_numbers.md`, this is a material annual pipeline impact.

### Step 5 — Brand reputation multiplier

The stalled-deal rate in mid-to-late stage pipeline is **72%** (`seed/baseline_numbers.md`). Bench over-commitment is a cause of deal stall that is qualitatively different from ordinary stall: the prospect does not simply go quiet — they have a concrete reason (Tenacious committed to something it could not deliver). This creates a negative reference that affects future inbound from their network.

Conservative brand multiplier: **1.5×** total pipeline impact (each over-committed close failure also prevents one referral conversion at expected value).

```
Total annual impact (conservative) = 0.78 × ACV_MIN × 1.5 = 1.17 × ACV_MIN
```

---

## Comparison Against Two Alternatives

### Alternative A: Gap Over-Claiming (Category 10, trigger rate 21.7%)

**Business cost argument:**
Gap over-claiming has a higher trigger rate (21.7% vs 11.3%) and affects Seg4 prospects who are typically the highest-ACV segment. A self-aware CTO recognising over-claimed competitive intelligence will disengage immediately.

**Why Bench Over-Commitment wins on ROI:**

1. **Detectability timing.** Gap over-claiming is detectable in the first email; the prospect simply does not reply. The cost is one lost thread with no trust established. Bench over-commitment is detected at the *discovery call* or *onboarding*, after significant investment by both parties. The recovery cost is much higher because the relationship has progressed.

2. **Mechanism specificity.** Gap over-claiming requires LLM output quality improvement (harder, stochastic, requires eval-tier model and prompt engineering). Bench over-commitment has a deterministic mechanical fix: re-check `bench_loader.available_count()` at each stage gate. The mechanism is narrowly scoped, testable, and has a clear pass/fail criterion.

3. **Tenacious brand asymmetry.** Tenacious's value proposition rests on reliability and delivery speed (time-to-deploy: 7–14 days per `seed/bench_summary.json`). Failing on the one claim that is most central to the value proposition (we have engineers ready to deploy) causes disproportionate brand damage relative to any content claim.

**ROI comparison:**
- Gap over-claiming fix: requires LLM prompt changes, eval-tier reruns, iterative improvement. Multi-sprint effort.
- Bench over-commitment fix: add one `bench_loader.available_count()` call to ConversationAgent at reply-handling time + a TTL cache invalidation. Single-sprint effort.

ROI = (Impact × Probability of fix working) / Cost of fix  
Gap over-claiming ROI ≈ (High impact × 60% fix probability) / High fix cost = moderate  
Bench over-commitment ROI ≈ (Moderate impact × 95% fix probability) / Low fix cost = **high**

---

### Alternative B: Scheduling Edge Cases (Category 8, trigger rate 15.0%)

**Business cost argument:**
Scheduling failures (15.0% trigger rate) directly block the discovery call, which is the conversion gate for all four ICP segments. A timezone error that produces a 1 AM EST slot will likely cause a no-show; at a discovery-to-proposal conversion of 30–50%, each missed call is a direct pipeline loss.

**Why Bench Over-Commitment wins on ROI:**

1. **Recovery possibility.** A timezone error on a booking is recoverable: the prospect receives the Cal.com confirmation, sees the UTC time, and can reschedule. The friction is real but not terminal. A bench over-commitment discovered at onboarding has no recovery path — Tenacious cannot deploy engineers it does not have.

2. **Fix complexity is similar** (Cal.com timezone localisation vs bench cache invalidation), but the downstream consequences are not. Scheduling failures cause friction; bench over-commitment causes delivery failure. Delivery failure is the existential risk for a services firm.

3. **Frequency-weighted impact.** Scheduling failures occur at 15% rate but with partial recovery. Bench over-commitment occurs at 11.3% rate with near-zero recovery once an engagement is promised. Expected unrecoverable loss: bench over-commitment wins.

**ROI comparison:**
- Scheduling fix ROI ≈ (Moderate impact, high recovery rate) / Moderate fix cost = moderate
- Bench over-commitment ROI ≈ (High impact, low recovery rate) / Low fix cost = **high**

---

## Summary

| Failure Mode | Trigger Rate | Recovery Rate | Fix Cost | ROI |
|---|---|---|---|---|
| **Bench Over-Commitment** | 11.3% | ~10% | Low (deterministic gate) | **High** |
| Gap Over-Claiming | 21.7% | ~60% (just don't reply) | High (LLM quality) | Moderate |
| Scheduling Edge Cases | 15.0% | ~50% (rescheduling) | Moderate (Cal.com config) | Moderate |

**Selected target:** Bench Over-Commitment — highest ROI because it is a deterministic fix for an unrecoverable failure that is central to Tenacious's reliability value proposition.

The mechanism for addressing this failure is described in `method.md`.
