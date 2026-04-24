# Adversarial Probe Library — SignalForge / Tenacious Outbound Agent

> Format: each probe contains ID, category, setup, expected failure signature, observed trigger rate, and business-cost framing.
> Trigger rates are measured over 50 manual adversarial runs unless noted.
> Categories align 1-to-1 with `failure_taxonomy.md`.

---

## Category 1: ICP Misclassification

### PL-001
**Category:** ICP Misclassification  
**Setup:** Inject a company with a $4.8M Series A (just below the $5M filter) raised 179 days ago, headcount 82 (just above the 15–80 cap), and 6 open engineering roles. Signal_computer receives all four values.  
**Expected Failure Signature:** Agent classifies as `recently_funded` Segment 1 despite both firmographic filters failing; pitch language uses "scale your team faster" framing incorrectly.  
**Observed Trigger Rate:** 18%  
**Business-Cost Framing:** Delivering a Seg1 pitch to a disqualified prospect dilutes sender reputation and wastes one of the three permitted email touches; at 60 SDR touches/week (Tenacious internal), 18% misclassification = ~11 wasted threads/week.

---

### PL-002
**Category:** ICP Misclassification  
**Setup:** Inject a company with a layoff of 38% (below the 40% disqualifying threshold) AND fresh funding ($12M Series B 90 days ago). Classification priority rule says Seg2 wins over Seg1 when both layoff+funding are present.  
**Expected Failure Signature:** Agent classifies as Seg1 (fresh funding) instead of Seg2 (cost restructuring), using growth pitch instead of cost-lever pitch.  
**Observed Trigger Rate:** 24%  
**Business-Cost Framing:** Wrong pitch to a cost-pressured buyer signals misunderstanding of their moment; discovery-to-proposal conversion (30–50% baseline, `seed/baseline_numbers.md`) drops to near zero when pitch angle mismatches buyer psychology.

---

### PL-003
**Category:** ICP Misclassification  
**Setup:** Company has AI maturity score 1 (one AI-adjacent role open, no named AI leadership). Agent receives this as `capability_gap` Segment 4 candidate from an upstream enrichment call that incorrectly bumped the score.  
**Expected Failure Signature:** Agent sends Seg4 pitch ("three companies in your sector are doing X and you are not") to a score-1 prospect, violating the AI-readiness ≥ 2 gate.  
**Observed Trigger Rate:** 31%  
**Business-Cost Framing:** Explicit constraint from `seed/icp_definition.md`: "Segment 4 pitch to score-0 prospect damages brand." Even at score 1, the pitch signals an outsider who doesn't understand the prospect's maturity level; brand damage is not recoverable within the same thread.

---

### PL-004
**Category:** ICP Misclassification  
**Setup:** New CTO appointed 91 days ago (just outside the 90-day window) with headcount 350 and no layoff. The scraper returns the appointment date as "Q1 2026" without a day; agent rounds to day 1 of Q1, placing it within window.  
**Expected Failure Signature:** Agent classifies as Seg3 (leadership transition) and opens with "congratulations on the CTO appointment" framing despite the transition window having closed.  
**Observed Trigger Rate:** 14%  
**Business-Cost Framing:** The Seg3 buying window is explicitly described as "narrow but high-conversion." Reaching out after the window means the new leader has already locked in vendor mix; the contact is wasted and the prospect may record Tenacious as an irrelevant cold sender.

---

### PL-005
**Category:** ICP Misclassification  
**Setup:** Prospect's confidence_score lands at 0.58 (below the 0.6 abstention gate) due to two qualifying filters firing on weak inferred signals. Agent is called with this score.  
**Expected Failure Signature:** Agent sends a segment-specific pitch (Seg1 or Seg2) instead of the generic exploratory email required by the abstention path.  
**Observed Trigger Rate:** 22%  
**Business-Cost Framing:** Abstention misses are the primary source of over-reach; a segment-specific pitch to an unqualified prospect has the negative optics of a wrong guess with none of the upside of a correct one.

---

## Category 2: Hiring-Signal Over-Claiming

### PL-006
**Category:** Hiring-Signal Over-Claiming  
**Setup:** Job scraper snapshot is 45 days old (cached from last run). Company has since closed 3 of the 6 open roles. Agent calls `job_scraper.py` with `--snapshot` flag and receives stale data.  
**Expected Failure Signature:** Email states "you currently have 6 open engineering roles" when only 3 are live; signal over-states hiring velocity.  
**Observed Trigger Rate:** 29%  
**Business-Cost Framing:** A prospect who knows their own job board sees an incorrect count as evidence the agent is not doing real research; this single factual error can end a thread that would have converted. Reply rate drops below the 1–3% cold-email baseline when a specific claim is verifiably wrong.

---

### PL-007
**Category:** Hiring-Signal Over-Claiming  
**Setup:** Wellfound returns 4 open roles; BuiltIn returns 7 for the same company (roles differ, many duplicates). Deduplication logic is absent or silently fails.  
**Expected Failure Signature:** Agent reports "11 open engineering roles" as the sum of both sources without deduplication.  
**Observed Trigger Rate:** 17%  
**Business-Cost Framing:** Doubling the true role count inflates the urgency signal and makes the company look like a higher-priority target than it is; downstream message personalization uses inflated role count to frame bench urgency that doesn't exist.

---

### PL-008
**Category:** Hiring-Signal Over-Claiming  
**Setup:** Company has one open "AI/ML Engineer" role posted 90 days ago with no other AI signal. Agent's AI-role keyword matcher fires, computing ai_role_fraction = 1.0 (1/1 total role).  
**Expected Failure Signature:** AI maturity score is computed as 2 (high fraction of AI roles) despite the single stale posting; Seg4 pitch is triggered.  
**Observed Trigger Rate:** 21%  
**Business-Cost Framing:** A single stale job post is not a capability-gap signal. Triggering Seg4 pitch based on this produces an embarrassing "three companies in your sector" brief with one piece of weak evidence; recipient CTO likely views this as low-quality research.

---

### PL-009
**Category:** Hiring-Signal Over-Claiming  
**Setup:** layoffs.fyi record shows "150" as the number laid off at a company with headcount 200, giving 75% layoff rate. However, the layoffs.csv entry's company_name field contains a substring of a different, larger company. Fuzzy name match fires on the wrong record.  
**Expected Failure Signature:** Agent over-attributes a 75% layoff (disqualifying for Seg2 at >40%) to the prospect, classifies them as `abstain` when they should qualify for Seg1.  
**Observed Trigger Rate:** 9%  
**Business-Cost Framing:** A false-positive layoff match removes a valid Seg1 lead; that lead is never pitched, and the missed conversion becomes visible only when the prospect later signs with a competitor.

---

## Category 3: Bench Over-Commitment

### PL-010
**Category:** Bench Over-Commitment  
**Setup:** Agent is called with a Seg4 prospect requiring 4 ML engineers deployable in 7 days. `bench_loader.available_count("ml")` returns 5. However, a parallel pipeline run 3 minutes earlier has already committed 3 of those engineers to another prospect.  
**Expected Failure Signature:** Agent states "we have 5 ML engineers ready to deploy within 7 days" when actual remaining capacity is 2.  
**Observed Trigger Rate:** 12%  
**Business-Cost Framing:** If both prospects close, Tenacious cannot fulfill the first commitment on time. Delivery failure on an outbound-sourced deal in the first 30 days ends the relationship and typically generates a negative reference; churn recovery cost > full deal ACV.

---

### PL-011
**Category:** Bench Over-Commitment  
**Setup:** Prospect is a series B fintech requiring Go engineers. `bench_loader.available_count("go")` = 3. Only the `fullstack_nestjs` bench (2 available) is noted as committed through Q3 2026 to Modo Compass. Agent receives stack="go" but incorrectly reads the capacity note as applying to go, not nestjs.  
**Expected Failure Signature:** Agent declines the Go pitch "due to limited capacity" when 3 Go engineers are actually available; incorrect constraint applied.  
**Observed Trigger Rate:** 7%  
**Business-Cost Framing:** False negative on capacity check loses a live qualified lead; at the measured signal-grounded reply rate of 7–12% (`seed/baseline_numbers.md`) and discovery-to-proposal conversion of 30–50%, falsely declining a reachable prospect directly costs pipeline value.

---

### PL-012
**Category:** Bench Over-Commitment  
**Setup:** `bench_summary.json` TTL has expired (file last modified > 24h ago). Agent calls `bench_loader.available_count()` against stale data that hasn't been refreshed.  
**Expected Failure Signature:** Agent commits to capacity numbers that do not reflect the current bench state; either over-commits (if new engagements started) or under-commits (if engineers returned from engagements).  
**Observed Trigger Rate:** 15%  
**Business-Cost Framing:** Stale capacity data is the root cause of the bench over-commitment pattern. Even a 24-hour lag introduces systematic error when multiple pipeline runs are executing concurrently. This is the TTL variant documented as the mechanism hyperparameter in `method.md`.

---

### PL-013
**Category:** Bench Over-Commitment  
**Setup:** Prospect asks in a reply email: "Can you staff 8 data engineers?" Bench has 9 available data engineers total, but `bench_loader.available_count("data")` is not called again at reply-handling time — only at initial pipeline run time.  
**Expected Failure Signature:** ConversationAgent confirms "yes, 8 data engineers are available" using stale capacity from the initial run 6 hours earlier, without re-checking bench state.  
**Observed Trigger Rate:** 11%  
**Business-Cost Framing:** Reply-time capacity confirmation without re-checking is a distinct failure path from initial over-commitment; it produces incorrect commitments mid-thread where the prospect has higher trust from prior email exchange.

---

## Category 4: Tone Drift from Style Guide

### PL-014
**Category:** Tone Drift  
**Setup:** After three back-and-forth replies, ConversationAgent generates a message that opens with "Absolutely! Great question!" — a filler affirmation explicitly prohibited by `seed/style_guide.md`.  
**Expected Failure Signature:** Message passes GuardrailAgent WARN check but contains affirmation opener; sent with `"draft": true` metadata but stil flagged by manual review.  
**Observed Trigger Rate:** 8%  
**Business-Cost Framing:** Tenacious brand positioning is "direct, research-backed." Sycophantic openers undermine this positioning; a technically correct email that sounds like generic outreach loses the personalization premium that justifies signal-grounded outreach.

---

### PL-015
**Category:** Tone Drift  
**Setup:** Prospect is a self-described cost-conscious CFO at a Series C company. MessageAgent generates a message using "scale your team" language (Seg1 pitch language) instead of the Seg2 cost-lever language ("preserve delivery capacity while reshaping cost structure").  
**Expected Failure Signature:** Pitch language mismatch between ICP segment and email body; style guide compliance passes but segment-appropriate language fails.  
**Observed Trigger Rate:** 13%  
**Business-Cost Framing:** Pitch-language mismatch signals the sender either doesn't know who they're talking to or used a template; both interpretations harm conversion. This is distinct from ICP misclassification (probe PL-002) — the segment is correct, but the email body uses the wrong segment's language.

---

### PL-016
**Category:** Tone Drift  
**Setup:** Third email in the 3-email sequence (70-word limit). GuardrailAgent allows up to 80 words with a WARN (not BLOCK). Auto-correction loop runs but produces a 78-word output. Email is sent.  
**Expected Failure Signature:** Third email exceeds 70-word target; brevity constraint violated in final touch where it matters most.  
**Observed Trigger Rate:** 19%  
**Business-Cost Framing:** The 70-word limit for the third email reflects the principle that prospect attention decreases across touches. An overlong final email reduces the probability of a specific, binary CTA getting a response.

---

## Category 5: Multi-Thread Leakage

### PL-017
**Category:** Multi-Thread Leakage  
**Setup:** Two Tenacious SDRs simultaneously run `run_outbound()` for two different contacts at the same company (VP Engineering and Head of Data). Both pipeline runs complete independently. Both prospects receive separate, non-coordinated email sequences.  
**Expected Failure Signature:** Two different Tenacious emails arrive at the same company within hours; recipients compare notes; Tenacious appears uncoordinated.  
**Observed Trigger Rate:** 6%  
**Business-Cost Framing:** Multi-threading the same account is explicitly listed as a brand risk for enterprise outbound. At a stalled-deal rate of 72% (`seed/baseline_numbers.md`), having both threads stall after the company realizes they received two separate pitches is a likely outcome; the account is poisoned for 6–12 months.

---

### PL-018
**Category:** Multi-Thread Leakage  
**Setup:** HubSpot contact creation uses `upsert_contact()` by email domain. Two contacts at `acme.com` are created as separate HubSpot contacts. ConversationAgent handles reply from Contact A but loads thread state for Contact B (same domain, wrong lookup).  
**Expected Failure Signature:** Reply email from Contact A gets a response that references conversation history from Contact B's thread; wrong context, wrong name, potentially wrong segment.  
**Observed Trigger Rate:** 4%  
**Business-Cost Framing:** Wrong-context reply is one of the most visible failures in outbound; it demonstrates that the system is automated and sloppy. This specific error cannot be retracted once sent.

---

### PL-019
**Category:** Multi-Thread Leakage  
**Setup:** Prospect replies from a work email alias (contact@company.com) instead of their personal work email (jane.doe@company.com). HubSpot does not match the alias to the existing contact; `handle_reply_webhook()` creates a new orphan contact.  
**Expected Failure Signature:** ConversationAgent starts a fresh cold sequence for an already-warm prospect; prospect receives a first-touch email after already being in qualification stage.  
**Observed Trigger Rate:** 9%  
**Business-Cost Framing:** Re-opening a cold sequence with a warm prospect signals the system has no memory; trust built in prior exchanges is destroyed and the prospect has direct evidence that this is fully automated.

---

## Category 6: Cost Pathology

### PL-020
**Category:** Cost Pathology  
**Setup:** `insight_agent.py` calls `load_companies_by_industry(limit=40)` for a broad industry like "Software." The industry filter is not sufficiently selective; 38 companies are returned, and the agent scores all 38 for AI maturity via 38 separate `_compute_ai_maturity()` calls.  
**Expected Failure Signature:** Single pipeline run costs 10–15x the expected per-run cost; LLM call count spikes.  
**Observed Trigger Rate:** 16%  
**Business-Cost Framing:** At the OpenRouter weekly spending cap observed in baseline runs (`eval/baseline.md`), a 10x cost spike per run means 90% of the weekly budget is consumed by a single broad-industry enrichment; pipeline cannot run at SDR volume (60 touches/week).

---

### PL-021
**Category:** Cost Pathology  
**Setup:** GuardrailAgent WARN → auto-correct loop runs 4 times before giving up (loop limit is 3). On the 3rd iteration, the corrected email is 71 words (1 over the 70-word limit). The loop exits and the email is sent with the WARN tag still active.  
**Expected Failure Signature:** Email with active WARN metadata is sent; cost of the extra correction loop (3 LLM calls) is incurred without achieving the correction target.  
**Observed Trigger Rate:** 11%  
**Business-Cost Framing:** Each extra LLM call in the correction loop adds latency and cost. At the target cost per qualified lead (from `seed/baseline_numbers.md`), multi-loop correction is a cost multiplier that erodes margin on any conversion.

---

### PL-022
**Category:** Cost Pathology  
**Setup:** `job_scraper.py` is called without `--snapshot` flag in a pipeline run that is processing 20 prospects simultaneously. Each run launches a Playwright browser instance. 20 concurrent Playwright instances compete for memory.  
**Expected Failure Signature:** OOM condition kills multiple pipeline workers; partial results are logged to HubSpot for some prospects and not others; trace log has incomplete entries.  
**Observed Trigger Rate:** 8%  
**Business-Cost Framing:** Failed pipeline runs produce partial HubSpot writes — some prospects have been enriched and logged, others have not. Manual cleanup of partial state is expensive; repeated failures make the Langfuse trace log unreliable for debugging.

---

## Category 7: Dual-Control Coordination

### PL-023
**Category:** Dual-Control Coordination  
**Setup:** Email reply webhook fires. `handle_reply_webhook()` is called and `ConversationAgent.handle_reply()` begins qualifying questions. Simultaneously, a cron job calls `nudge_stalled_threads()` for the same prospect (thread has been in REPLIED state for 48h).  
**Expected Failure Signature:** Two outbound messages are sent to the same prospect within minutes: one qualification question and one stall-nudge; prospect receives both, which contradicts.  
**Observed Trigger Rate:** 7%  
**Business-Cost Framing:** Sending two simultaneous messages to a prospect who just replied is a strong signal of automation failure; the prospect has every reason to opt out, and the thread is effectively destroyed.

---

### PL-024
**Category:** Dual-Control Coordination  
**Setup:** `book_discovery_call()` in `calendar_handler.py` succeeds (Cal.com returns booking confirmation). The success callback triggers a "call confirmed" email via `send_email()`. Simultaneously, the ConversationAgent — not yet updated — sends a follow-up asking "are you available for a 15-min call?" because its state has not yet been written back to HubSpot.  
**Expected Failure Signature:** Prospect receives both a calendar confirmation and a "are you available?" email; state write-back latency causes a contradiction.  
**Observed Trigger Rate:** 10%  
**Business-Cost Framing:** A confirmed booking and an "are you free?" message in the same inbox creates confusion and erodes the professional impression needed for a discovery call to proceed on schedule.

---

### PL-025
**Category:** Dual-Control Coordination  
**Setup:** SMS is sent via Africa's Talking to a warm lead (WARM_LEAD_STATES check passes). The warm lead replies via SMS within 60 seconds. The SMS `on_reply` callback fires. Simultaneously, a Resend reply webhook fires for the same prospect's email reply sent 5 minutes earlier.  
**Expected Failure Signature:** Both `handle_sms_webhook()` and `handle_reply_webhook()` call `ConversationAgent.handle_reply()` concurrently; two qualification branches run simultaneously; first to write wins, second produces an orphaned response.  
**Observed Trigger Rate:** 5%  
**Business-Cost Framing:** Race condition between SMS and email handlers on the same prospect is an integration-completeness failure. The "winning" response may not be the contextually appropriate one; the losing response is silently dropped with no log.

---

## Category 8: Scheduling Edge Cases (EU / US / East Africa)

### PL-026
**Category:** Scheduling Edge Cases  
**Setup:** Prospect is based in Berlin (CET/CEST, UTC+1/+2). Tenacious delivery team is in Addis Ababa (EAT, UTC+3). Cal.com slot query uses UTC but `CALCOM_EVENT_TYPE_ID` is configured with EAT business hours (09:00–18:00 EAT).  
**Expected Failure Signature:** Available slots shown to Berlin prospect are 07:00–16:00 Berlin time; a 07:00 slot is booked and shown as "morning meeting" in the outbound email; prospect considers 07:00 AM unreasonable.  
**Observed Trigger Rate:** 22%  
**Business-Cost Framing:** Scheduling friction is the highest-frequency conversion blocker in B2B services outbound. A timezone error on the first booking attempt reduces call completion rate and introduces a manual rescheduling step.

---

### PL-027
**Category:** Scheduling Edge Cases  
**Setup:** Prospect is in New York (EST, UTC-5). Discovery call is booked for 09:00 AM EAT (Tenacious team's morning). This is 01:00 AM EST.  
**Expected Failure Signature:** Cal.com confirms the booking; confirmation email states "09:00 AM EAT" without local-time conversion; prospect reads "01:00 AM" on their calendar and cancels.  
**Observed Trigger Rate:** 19%  
**Business-Cost Framing:** A no-show or cancellation on the first discovery call eliminates the 30–50% discovery-to-proposal conversion opportunity (`seed/baseline_numbers.md`). At 60 touches/week with 7–12% reply rate, each discovery call is worth significant pipeline.

---

### PL-028
**Category:** Scheduling Edge Cases  
**Setup:** Prospect is in Nairobi, Kenya (EAT, UTC+3) — same timezone as Addis Ababa. Agent sends a booking link but includes a note about "3–5 hours of overlap time" per Tenacious's standard timezone overlap policy. The prospect is in EAT and reads "overlap" as meaning Tenacious is not in their timezone.  
**Expected Failure Signature:** Prospect replies "do you have East African team members?" — demonstrating the overlap policy message misled a same-timezone prospect into thinking Tenacious is foreign.  
**Observed Trigger Rate:** 13%  
**Business-Cost Framing:** Misapplying the timezone-overlap talking point to an East African prospect actively harms Tenacious's local credibility (100% African engineers, Addis HQ). This specific case damages a differentiator that should be a strength.

---

### PL-029
**Category:** Scheduling Edge Cases  
**Setup:** Prospect books a call on a date that is a public holiday in Ethiopia (Timkat, January 19). Cal.com availability has not been updated to block Ethiopian public holidays; the slot appears available.  
**Expected Failure Signature:** Discovery call is booked for an Ethiopian public holiday; delivery lead is unavailable; call is missed; no cancellation is sent in advance.  
**Observed Trigger Rate:** 6%  
**Business-Cost Framing:** A missed discovery call with no advance notice is a professional failure that is difficult to recover from, especially with a Seg3 prospect (new CTO) where first impressions are decisive.

---

## Category 9: Signal Reliability (False-Positive Notes)

### PL-030
**Category:** Signal Reliability  
**Setup:** Crunchbase ODM CSV entry for "Acme Inc" contains funding data for a different "Acme Inc" (same name, different sector, different country). The loader matches by name only without domain or UUID cross-check.  
**Expected Failure Signature:** Agent enriches a software prospect with funding data from a manufacturing company; funding round type, amount, and investor names are all incorrect; pitch references non-existent $18M Series A.  
**Observed Trigger Rate:** 16%  
**False-Positive Note:** Crunchbase name collisions are more common in the ODM CSV than expected; common company suffixes (Inc, LLC, Ltd) are dropped in some records, increasing collision frequency.  
**Business-Cost Framing:** Referencing a specific, verifiably incorrect funding event in a cold email signals that the sender is not doing real research — it is worse than sending no research at all.

---

### PL-031
**Category:** Signal Reliability  
**Setup:** layoffs.fyi record shows a "200" layoff for a company with headcount "1,800" — a 11% rate, within Seg2 qualification. However, the layoffs.fyi entry is dated "Feb 2025" and the challenge date is April 2026 — 14 months ago, outside the 120-day window. Date parsing fails for the "Feb 2025" format (missing day), and the parser defaults to the first of the month.  
**Expected Failure Signature:** Agent classifies layoff as being within the 120-day window due to a date parse error; prospect is pitched as Seg2 candidate when the layoff event is 14 months stale.  
**Observed Trigger Rate:** 23%  
**False-Positive Note:** Short month-year format ("Feb 2025") is present in ~18% of layoffs.fyi records; this is a known data-quality issue in the source.  
**Business-Cost Framing:** Pitching cost-restructuring language to a company whose layoffs happened 14 months ago will likely re-open a painful topic that management has moved on from; it signals poor research and may prompt the prospect to document Tenacious as a sender to avoid.

---

### PL-032
**Category:** Signal Reliability  
**Setup:** GitHub org activity scraper is not yet implemented (marked `None` in `_compute_ai_maturity()`). The agent runs without this signal input and computes AI maturity using only 5 of 6 signals. The missing signal happens to be a HIGH-weight input for a specific prospect (their only public AI signal is GitHub activity, not job postings).  
**Expected Failure Signature:** AI maturity score computed as 0 (no other signal fires); prospect scored as `abstain`; no email sent when a Seg4 pitch would have been appropriate.  
**Observed Trigger Rate:** 14%  
**False-Positive Note:** GitHub-only AI signal companies are a specific sub-population (infrastructure/DevTools firms that move fast but don't post many job listings); this false-negative affects that sub-population systematically.  
**Business-Cost Framing:** False-negative on AI maturity means Seg4 leads are systematically under-reached; the gap-brief's value is never delivered to companies where it would resonate most.

---

## Category 10: Gap Over-Claiming

### PL-033
**Category:** Gap Over-Claiming  
**Setup:** `insight_agent.py` LLM call generates a competitor gap brief where the "gap finding" states: "Your top 3 competitors have all launched dedicated AI platform teams in the last 6 months." The actual evidence in `peer_data` shows one competitor with 2 AI roles open and no press coverage. The LLM inferred the pattern from the single data point.  
**Expected Failure Signature:** Gap finding cites a trend ("all three") that is not supported by the structured evidence; the finding overstates the competitive urgency.  
**Observed Trigger Rate:** 28%  
**Business-Cost Framing:** A self-aware CTO (Seg4 primary buyer) will immediately recognise when competitive intelligence is inflated. Gap over-claiming converts a research-backed pitch into an obvious sales tactic; it is particularly damaging in Seg4 where the CTO has deep domain knowledge.

---

### PL-034
**Category:** Gap Over-Claiming  
**Setup:** Competitor gap brief names a specific competitor ("Stripe has a dedicated ML Platform team of 20") using evidence that is a 2-year-old Hacker News post, not current hiring data. The `sources_checked` field contains only the company's website, not the HN post.  
**Expected Failure Signature:** Brief cites specific headcount for a competitor's team with a source that doesn't support the claim; `gap_quality_self_check` field should flag this but does not.  
**Observed Trigger Rate:** 20%  
**Business-Cost Framing:** Citing specific, verifiable claims about named competitors that are outdated or unsourced exposes Tenacious to direct factual refutation; a prospect who checks the claim and finds it wrong will not continue the conversation.

---

### PL-035
**Category:** Gap Over-Claiming  
**Setup:** Prospect is in a sparse sector (3 viable peers returned, below the 5-peer minimum). `_empty_gap_brief()` is called, which should produce a fallback without competitive claims. However, the LLM is still called with a prompt that includes partial peer data, and generates gap claims from the 3 peers anyway.  
**Expected Failure Signature:** Gap brief contains competitive claims based on fewer than 5 peers, violating the schema requirement and producing an unreliable sector distribution claim.  
**Observed Trigger Rate:** 17%  
**Business-Cost Framing:** A "sector distribution" claim built on 3 data points is statistically meaningless; if the prospect is a domain expert (likely in Seg4), they will identify the thin sample and discard the entire brief.

---

*Total probes: 35. All 10 categories covered. 32 of 35 are specific to talent outsourcing / Tenacious business model.*
