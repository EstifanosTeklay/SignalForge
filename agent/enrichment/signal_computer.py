"""
agent/enrichment/signal_computer.py

Pure-logic signal computation — no LLM, no I/O.
Takes a populated Company and returns a HiringSignalBrief.

ICP classification priority order (per icp_definition.md):
  1. Layoff (last 120d) AND fresh funding → Segment 2 (cost pressure dominates)
  2. Leadership change (last 90d)         → Segment 3 (transition window dominates)
  3. AI-readiness ≥ 2 + capability signal → Segment 4
  4. Fresh funding (last 180d)            → Segment 1
  5. Otherwise                            → ABSTAIN (generic exploratory)

Abstention threshold: segment_confidence < 0.6 → ABSTAIN regardless of segment.

Qualification filter checks applied per segment (from icp_definition.md):
  Segment 1: headcount 15–80, ≥5 open roles, Series A/B, funding $5–30M, ICP geographies
  Segment 2: layoff ≤ 40%, headcount 200–2000, ≥3 open roles post-layoff
  Segment 3: headcount 50–500, CTO/VP Eng role specifically
  Segment 4: AI-readiness ≥ 2, bench-feasible

Disqualifiers applied before classification:
  - Layoff > 15% headcount in last 90d on a Segment 1 candidate → shift to Segment 2
  - Layoff > 40% → Segment 2 disqualified (company in survival mode)
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from agent.enrichment.bench_loader import check_bench_match, stack_names
from agent.models.company import Company
from agent.models.signals import (
    ABSTAIN_THRESHOLD,
    CONFIDENCE_SCORE,
    AIMaturitySignal,
    FundingSignal,
    HiringSignalBrief,
    HiringSignal,
    ICPClassification,
    ICPSegment,
    LayoffSignal,
    LeadershipChangeSignal,
    SignalConfidence,
)

# Window constants (days)
FUNDING_WINDOW = 180
LAYOFF_WINDOW = 120
LEADERSHIP_WINDOW = 90
LAYOFF_DISQUALIFY_WINDOW = 90       # for Segment 1 disqualifier

# Segment headcount bounds (from icp_definition.md)
SEG1_HC_MIN, SEG1_HC_MAX = 15, 80
SEG2_HC_MIN, SEG2_HC_MAX = 200, 2000
SEG3_HC_MIN, SEG3_HC_MAX = 50, 500

# Funding amount bounds for Segment 1 ($5M–$30M)
SEG1_FUNDING_MIN_USD = 5_000_000
SEG1_FUNDING_MAX_USD = 30_000_000

# Minimum open roles per segment
SEG1_MIN_ROLES = 5
SEG2_MIN_ROLES = 3

# Segment 2 layoff disqualifier threshold
SEG2_LAYOFF_DQ_PCT = 0.40     # > 40% headcount cut → company in survival mode

# Segment 1 → Segment 2 shift threshold
SEG1_LAYOFF_SHIFT_PCT = 0.15  # > 15% layoff in last 90d forces Segment 2

# AI-adjacent role keywords (case-insensitive substring match)
_AI_ROLE_KEYWORDS = [
    "ml engineer", "machine learning engineer", "machine learning",
    "applied scientist", "applied science",
    "llm engineer", "ai engineer", "ai/ml",
    "data scientist", "research scientist",
    "ai product manager", "ai pm",
    "data platform engineer", "mlops", "ml platform",
    "computer vision", "nlp engineer",
]

# Modern ML stack technologies
_ML_STACK_KEYWORDS = [
    "dbt", "snowflake", "databricks", "weights & biases", "wandb",
    "ray", "vllm", "mlflow", "airflow", "spark", "pytorch", "tensorflow",
    "hugging face", "sagemaker", "vertex ai", "kubeflow",
]

# Named AI/ML leadership role keywords
_AI_LEADERSHIP_KEYWORDS = [
    "head of ai", "vp of ai", "vp ai", "chief scientist", "chief ai",
    "vp data", "head of data", "chief data", "head of ml",
    "director of ai", "director of ml", "ai lead",
]

# GitHub AI activity markers — org names, repo patterns, or tech mentions
# that indicate the company has a public AI-oriented GitHub presence.
_GITHUB_AI_MARKERS = [
    "github.com",
    "open-source", "open source",
    "hugging face", "huggingface",
    "model card", "model weights",
    "mlops", "llm", "rag pipeline", "fine-tuning",
]

# ICP-qualified geographies for Segment 1 (North America, UK, Germany, France, Nordics, Ireland)
_SEG1_COUNTRIES = {
    "us", "usa", "united states", "ca", "canada",
    "gb", "uk", "united kingdom",
    "de", "germany",
    "fr", "france",
    "ie", "ireland",
    "se", "sweden", "no", "norway", "dk", "denmark", "fi", "finland",
}


def _days_ago(d: Optional[date]) -> Optional[int]:
    if d is None:
        return None
    return (date.today() - d).days


def _band_midpoint(band: Optional[str]) -> Optional[int]:
    """Return approximate headcount midpoint from a band string like '51-200'."""
    if not band:
        return None
    band = band.strip()
    if band.endswith("+"):
        try:
            return int(band[:-1]) + 500
        except ValueError:
            return None
    parts = band.replace("–", "-").split("-")
    if len(parts) == 2:
        try:
            lo, hi = int(parts[0]), int(parts[1])
            return (lo + hi) // 2
        except ValueError:
            return None
    return None


def _headcount_in_range(company: Company, hc_min: int, hc_max: int) -> Optional[bool]:
    """
    Returns True/False/None.
    None means the band is ambiguous — caller should apply a confidence penalty
    rather than disqualify outright.
    """
    if company.employee_count_exact is not None:
        return hc_min <= company.employee_count_exact <= hc_max
    mid = _band_midpoint(company.employee_count_band)
    if mid is None:
        return None
    # Give a ±20% margin before calling it ambiguous
    if mid >= hc_min * 0.8 and mid <= hc_max * 1.2:
        return True
    if mid < hc_min * 0.5 or mid > hc_max * 2.0:
        return False
    return None  # genuinely ambiguous


def _geo_qualifies_seg1(company: Company) -> bool:
    """Check if company HQ is in an ICP-qualified geography for Segment 1."""
    country = (company.hq_country or "").lower().strip()
    if not country:
        return True  # unknown → don't disqualify, just reduce confidence
    return country in _SEG1_COUNTRIES


# ── Individual signal computers ───────────────────────────────────────────────

def _compute_funding(company: Company) -> FundingSignal:
    lf = company.latest_funding
    if lf is None or lf.announced_date is None:
        return FundingSignal(
            has_recent_funding=False,
            days_since_funding=None,
            round_type=None,
            amount_usd=None,
            confidence=SignalConfidence.NONE,
            justification="No funding event found in Crunchbase data.",
        )

    days = _days_ago(lf.announced_date)
    if days is None or days > FUNDING_WINDOW:
        return FundingSignal(
            has_recent_funding=False,
            days_since_funding=days,
            round_type=lf.round_type,
            amount_usd=lf.amount_usd,
            confidence=SignalConfidence.LOW,
            justification=(
                f"Most recent round ({lf.round_type}) was {days} days ago, "
                f"outside the {FUNDING_WINDOW}-day active window."
            ),
        )

    amount_str = f"${lf.amount_usd:,}" if lf.amount_usd else "undisclosed amount"
    confidence = SignalConfidence.HIGH if lf.amount_usd else SignalConfidence.MEDIUM
    return FundingSignal(
        has_recent_funding=True,
        days_since_funding=days,
        round_type=lf.round_type,
        amount_usd=lf.amount_usd,
        confidence=confidence,
        justification=(
            f"{lf.round_type} of {amount_str} announced {days} days ago "
            f"(Crunchbase, {lf.announced_date})."
        ),
    )


def _compute_hiring(
    company: Company,
    prior_snapshot: Optional[dict] = None,
) -> HiringSignal:
    """
    Compute hiring signal.

    velocity_ratio is a 60-day delta: (current_count - prior_count) / prior_count.
    Requires prior_snapshot = {"open_roles_count": int, "snapshot_date": "YYYY-MM-DD"}.
    When prior_snapshot is absent, velocity_ratio is None and confidence is capped
    at LOW regardless of current count.  The caller (ResearchAgent) should pass a
    prior snapshot from job_snapshot.json when available so velocity is real.
    """
    count = company.open_roles_count or 0
    titles = [t.lower() for t in company.open_role_titles]

    ai_count = sum(
        1 for title in titles
        if any(kw in title for kw in _AI_ROLE_KEYWORDS)
    )

    if count == 0:
        return HiringSignal(
            open_roles_count=0,
            velocity_ratio=None,
            has_ai_adjacent_roles=False,
            ai_adjacent_role_count=0,
            confidence=SignalConfidence.NONE,
            justification="No open roles found in public job post snapshot.",
        )

    # ── 60-day velocity delta ─────────────────────────────────────────────────
    velocity_ratio: Optional[float] = None
    velocity_note = ""

    if prior_snapshot:
        prior_count = prior_snapshot.get("open_roles_count", 0)
        prior_date_str = prior_snapshot.get("snapshot_date", "")
        try:
            prior_date = date.fromisoformat(prior_date_str)
            today = date.today()
            days_elapsed = (today - prior_date).days
            if days_elapsed > 0 and prior_count > 0:
                # Normalise to a 60-day window
                raw_delta = count - prior_count
                velocity_ratio = round((raw_delta / prior_count) * (60 / days_elapsed), 3)
                direction = "up" if raw_delta > 0 else "down" if raw_delta < 0 else "flat"
                velocity_note = (
                    f"Velocity {velocity_ratio:+.1%} over {days_elapsed}d "
                    f"(normalised to 60-day window, {direction}: "
                    f"{prior_count}→{count} roles)."
                )
            elif prior_count == 0:
                velocity_ratio = 1.0  # new roles appeared from zero
                velocity_note = f"First hiring signal detected; {count} roles vs zero prior."
        except (ValueError, TypeError):
            pass  # malformed snapshot_date — skip velocity

    has_ai = ai_count > 0

    if velocity_ratio is not None:
        confidence = SignalConfidence.HIGH if abs(velocity_ratio) > 0.5 else SignalConfidence.MEDIUM
    elif count >= 5:
        confidence = SignalConfidence.MEDIUM
    else:
        confidence = SignalConfidence.LOW

    parts = [f"{count} open role{'s' if count != 1 else ''} found in public job snapshot."]
    if has_ai:
        parts.append(f"{ai_count} AI-adjacent role{'s' if ai_count != 1 else ''} detected.")
    if velocity_note:
        parts.append(velocity_note)
    elif count >= 5:
        parts.append(
            "Volume suggests active hiring phase. "
            "No prior snapshot — 60-day velocity requires a second data point."
        )

    return HiringSignal(
        open_roles_count=count,
        velocity_ratio=velocity_ratio,
        has_ai_adjacent_roles=has_ai,
        ai_adjacent_role_count=ai_count,
        confidence=confidence,
        justification=" ".join(parts),
    )


def _compute_layoff(company: Company) -> LayoffSignal:
    if not company.layoff_events:
        return LayoffSignal(
            has_recent_layoff=False,
            days_since_layoff=None,
            headcount_cut=None,
            percentage_cut=None,
            confidence=SignalConfidence.NONE,
            justification="No layoff event found in layoffs.fyi within active window.",
        )

    most_recent = max(company.layoff_events, key=lambda e: e.event_date)
    days = _days_ago(most_recent.event_date)

    if days is None or days > LAYOFF_WINDOW:
        return LayoffSignal(
            has_recent_layoff=False,
            days_since_layoff=days,
            headcount_cut=most_recent.headcount_cut,
            percentage_cut=most_recent.percentage_cut,
            confidence=SignalConfidence.LOW,
            justification=(
                f"Most recent layoff was {days} days ago, "
                f"outside the {LAYOFF_WINDOW}-day active window."
            ),
        )

    pct_str = (
        f"{most_recent.percentage_cut * 100:.0f}%"
        if most_recent.percentage_cut
        else "unknown %"
    )
    count_str = (
        f"{most_recent.headcount_cut:,} people"
        if most_recent.headcount_cut
        else "unknown headcount"
    )

    return LayoffSignal(
        has_recent_layoff=True,
        days_since_layoff=days,
        headcount_cut=most_recent.headcount_cut,
        percentage_cut=most_recent.percentage_cut,
        confidence=SignalConfidence.HIGH,
        justification=(
            f"Layoff of {count_str} ({pct_str}) reported {days} days ago "
            f"(layoffs.fyi, {most_recent.event_date})."
        ),
    )


def _compute_leadership(company: Company) -> LeadershipChangeSignal:
    if not company.leadership_changes:
        return LeadershipChangeSignal(
            has_recent_change=False,
            role=None,
            days_since_appointment=None,
            confidence=SignalConfidence.NONE,
            justification="No leadership change detected in Crunchbase or press data.",
        )

    cutoff = date.today() - timedelta(days=LEADERSHIP_WINDOW)
    recent = [
        lc for lc in company.leadership_changes
        if lc.announced_date and lc.announced_date >= cutoff
    ]

    if not recent:
        oldest = min(
            (lc for lc in company.leadership_changes if lc.announced_date),
            key=lambda lc: lc.announced_date,
            default=None,
        )
        return LeadershipChangeSignal(
            has_recent_change=False,
            role=oldest.role if oldest else None,
            days_since_appointment=_days_ago(oldest.announced_date) if oldest else None,
            confidence=SignalConfidence.LOW,
            justification=f"Leadership change detected but outside {LEADERSHIP_WINDOW}-day window.",
        )

    latest = max(recent, key=lambda lc: lc.announced_date)
    days = _days_ago(latest.announced_date)
    name_str = f" ({latest.name})" if latest.name else ""
    return LeadershipChangeSignal(
        has_recent_change=True,
        role=latest.role,
        days_since_appointment=days,
        confidence=SignalConfidence.HIGH,
        justification=(
            f"New {latest.role}{name_str} appointed {days} days ago "
            f"({latest.announced_date}). Vendor-reassessment window is open."
        ),
    )


def _compute_ai_maturity(company: Company) -> AIMaturitySignal:
    """
    Score AI maturity 0–3 from public signals.
    Score contributions (additive, capped at 3):
      - AI-adjacent open roles (fraction of total): 0–1 pt  (HIGH weight)
      - Named AI/ML leadership:                        1 pt  (HIGH weight)
      - Exec AI commentary in public_ai_mentions:    0.5 pt  (MEDIUM weight)
      - GitHub AI activity:                          0.5 pt  (MEDIUM weight)
      - Modern ML stack detected:                    0.5 pt  (LOW weight)
    """
    score_float = 0.0
    evidence: list[str] = []

    titles_lower = [t.lower() for t in company.open_role_titles]
    total_roles = len(titles_lower)
    ai_count = sum(1 for t in titles_lower if any(kw in t for kw in _AI_ROLE_KEYWORDS))

    if total_roles > 0:
        ai_fraction = ai_count / total_roles
        if ai_fraction >= 0.3 or ai_count >= 3:
            score_float += 1.0
            evidence.append(
                f"{ai_count}/{total_roles} open roles are AI-adjacent "
                f"({ai_fraction:.0%} of engineering openings)."
            )
        elif ai_count > 0:
            score_float += 0.5
            evidence.append(
                f"{ai_count} AI-adjacent role{'s' if ai_count > 1 else ''} "
                f"detected ({ai_fraction:.0%} of engineering openings)."
            )

    all_text = " ".join(company.public_ai_mentions).lower()
    has_ai_leadership = any(kw in all_text for kw in _AI_LEADERSHIP_KEYWORDS)
    if has_ai_leadership:
        score_float += 1.0
        evidence.append("Named AI/ML leadership role detected in public profile or press.")

    tech_lower = [t.lower() for t in company.detected_technologies]
    ml_stack_hits = [kw for kw in _ML_STACK_KEYWORDS if any(kw in t for t in tech_lower)]
    if ml_stack_hits:
        score_float += 0.5
        evidence.append(f"Modern ML stack detected: {', '.join(ml_stack_hits[:3])}.")

    ai_keywords = ["artificial intelligence", " ai ", " ml ", "machine learning", "llm", "genai"]
    ai_mention_count = sum(
        1 for mention in company.public_ai_mentions
        if any(kw in mention.lower() for kw in ai_keywords)
    )
    if ai_mention_count >= 2:
        score_float += 0.5
        evidence.append(f"Executive AI commentary found in {ai_mention_count} public sources.")
    elif ai_mention_count == 1:
        score_float += 0.25
        evidence.append("Single public AI mention from executive or press.")

    # ── GitHub AI activity (MEDIUM weight, 0.5 pt) ───────────────────────────
    # Detected from: company website domain (github.com org), detected_technologies,
    # or public_ai_mentions containing GitHub/open-source AI markers.
    # A future enhancement can replace this with a live GitHub API check.
    all_tech = " ".join(company.detected_technologies).lower()
    all_mentions = " ".join(company.public_ai_mentions).lower()
    website_lower = (company.website or "").lower()

    github_signals: list[str] = []
    if "github.com" in website_lower:
        github_signals.append("company GitHub org linked from website")
    if any(m in all_tech for m in _GITHUB_AI_MARKERS):
        matched = [m for m in _GITHUB_AI_MARKERS if m in all_tech]
        github_signals.append(f"AI-related tech markers in stack: {', '.join(matched[:3])}")
    if any(m in all_mentions for m in _GITHUB_AI_MARKERS):
        matched = [m for m in _GITHUB_AI_MARKERS if m in all_mentions]
        github_signals.append(f"Open-source/GitHub AI activity in public mentions: {', '.join(matched[:3])}")

    has_github_ai_activity = bool(github_signals)
    if has_github_ai_activity:
        score_float += 0.5
        evidence.append(f"GitHub/open-source AI activity detected: {'; '.join(github_signals)}.")

    final_score = min(3, round(score_float))

    high_weight_count = sum([ai_count >= 3, has_ai_leadership])
    medium_weight_count = sum([ai_mention_count >= 2, bool(ml_stack_hits), has_github_ai_activity])

    if high_weight_count >= 2 or (high_weight_count >= 1 and medium_weight_count >= 1):
        confidence = SignalConfidence.HIGH
    elif high_weight_count >= 1 or medium_weight_count >= 2:
        confidence = SignalConfidence.MEDIUM
    elif score_float > 0:
        confidence = SignalConfidence.LOW
    else:
        confidence = SignalConfidence.NONE

    return AIMaturitySignal(
        score=final_score,
        confidence=confidence,
        ai_adjacent_role_count=ai_count,
        has_named_ai_leadership=has_ai_leadership,
        has_github_ai_activity=has_github_ai_activity,
        has_exec_ai_commentary=ai_mention_count > 0,
        has_modern_ml_stack=bool(ml_stack_hits),
        evidence_notes=evidence,
    )


# ── ICP Classification ────────────────────────────────────────────────────────

def _classify_icp(
    company: Company,
    funding: FundingSignal,
    hiring: HiringSignal,
    layoff: LayoffSignal,
    leadership: LeadershipChangeSignal,
    ai_maturity: AIMaturitySignal,
) -> tuple[ICPClassification, list[str]]:
    """
    Assign ICP segment per icp_definition.md priority rules.
    Returns (ICPClassification, honesty_flags).

    Priority order:
      1. Layoff (120d) + funding → Segment 2  (cost pressure over fresh budget)
      2. Leadership change (90d) → Segment 3  (transition window dominates)
      3. AI maturity ≥ 2 + capability signal → Segment 4
      4. Fresh funding (180d)    → Segment 1
      5. Abstain
    """
    flags: list[str] = []

    # ── Pre-checks: is there a recent layoff in last 90d above 15%? ───────────
    # If yes, a Segment 1 candidate shifts to Segment 2 (ICP disqualifier rule)
    recent_heavy_layoff = False
    if layoff.has_recent_layoff and layoff.days_since_layoff is not None:
        if layoff.days_since_layoff <= LAYOFF_DISQUALIFY_WINDOW:
            if layoff.percentage_cut and layoff.percentage_cut > SEG1_LAYOFF_SHIFT_PCT:
                recent_heavy_layoff = True
                flags.append("layoff_overrides_funding")

    # ── Priority 1: Layoff + funding → Segment 2 ──────────────────────────────
    if layoff.has_recent_layoff and funding.has_recent_funding:
        flags.append("conflicting_segment_signals")
        # Segment 2 disqualifier: layoff > 40% → survival mode
        if layoff.percentage_cut and layoff.percentage_cut > SEG2_LAYOFF_DQ_PCT:
            return ICPClassification(
                segment=ICPSegment.ABSTAIN,
                confidence=SignalConfidence.LOW,
                confidence_score=0.30,
                disqualified=True,
                disqualification_reason=(
                    f"Layoff of {layoff.percentage_cut * 100:.0f}% exceeds the 40% "
                    "threshold — company likely in survival mode, not a Segment 2 buyer."
                ),
                justification="Disqualified: deep restructuring signals survival mode.",
            ), flags + ["conflicting_segment_signals"]

        hc_ok = _headcount_in_range(company, SEG2_HC_MIN, SEG2_HC_MAX)
        roles_ok = (hiring.open_roles_count or 0) >= SEG2_MIN_ROLES
        base_conf = layoff.confidence
        conf_score = CONFIDENCE_SCORE[base_conf.value]
        if hc_ok is False:
            conf_score = max(0.0, conf_score - 0.20)
            flags.append("weak_hiring_velocity_signal")
        if not roles_ok:
            conf_score = max(0.0, conf_score - 0.15)
        if conf_score < ABSTAIN_THRESHOLD:
            flags.append("conflicting_segment_signals")
        return ICPClassification(
            segment=ICPSegment.COST_RESTRUCTURING,
            confidence=base_conf,
            confidence_score=conf_score,
            justification=(
                f"Layoff of {layoff.headcount_cut or 'unknown'} people "
                f"{layoff.days_since_layoff}d ago, combined with "
                f"{funding.round_type} funding. Cost pressure dominates the "
                "buying window per ICP classification rule 1."
            ),
        ), flags

    # ── Priority 2: Leadership change → Segment 3 ─────────────────────────────
    if leadership.has_recent_change:
        hc_ok = _headcount_in_range(company, SEG3_HC_MIN, SEG3_HC_MAX)
        conf_score = CONFIDENCE_SCORE[leadership.confidence.value]
        if hc_ok is False:
            conf_score = max(0.0, conf_score - 0.20)
        if hc_ok is None:
            conf_score = max(0.0, conf_score - 0.05)
        return ICPClassification(
            segment=ICPSegment.LEADERSHIP_CHANGE,
            confidence=leadership.confidence,
            confidence_score=conf_score,
            justification=(
                f"New {leadership.role} within {leadership.days_since_appointment}d. "
                "Vendor-reassessment window is typically open for 6 months post-appointment."
            ),
        ), flags

    # ── Priority 3: Capability gap (AI ≥ 2) → Segment 4 ──────────────────────
    # Also requires: bench-feasible, not just AI maturity
    if ai_maturity.score >= 2:
        # Infer required stacks from detected tech + AI role signals
        required = _infer_required_stacks(company)
        bench = check_bench_match(required)
        if not bench["bench_available"] and bench["gaps"]:
            flags.append("bench_gap_detected")
            return ICPClassification(
                segment=ICPSegment.ABSTAIN,
                confidence=SignalConfidence.LOW,
                confidence_score=0.35,
                disqualified=True,
                disqualification_reason=(
                    f"Bench gap detected for stacks: {', '.join(bench['gaps'])}. "
                    "Segment 4 engagement is not bench-feasible at this time."
                ),
                justification="Bench does not cover required stacks — cannot pitch Segment 4.",
            ), flags

        conf_score = CONFIDENCE_SCORE[ai_maturity.confidence.value]
        if ai_maturity.confidence == SignalConfidence.LOW:
            flags.append("weak_ai_maturity_signal")
        return ICPClassification(
            segment=ICPSegment.CAPABILITY_GAP,
            confidence=ai_maturity.confidence,
            confidence_score=conf_score,
            justification=(
                f"AI maturity score {ai_maturity.score}/3 with "
                f"{ai_maturity.confidence.value} confidence. "
                "Specialized AI/ML build likely — bench match confirmed."
            ),
        ), flags

    # ── Priority 4: Fresh funding alone → Segment 1 ───────────────────────────
    if funding.has_recent_funding:
        # Segment 1 disqualifier: heavy layoff in last 90d → shift to Segment 2
        if recent_heavy_layoff:
            hc_ok = _headcount_in_range(company, SEG2_HC_MIN, SEG2_HC_MAX)
            conf_score = CONFIDENCE_SCORE[layoff.confidence.value] * 0.8
            return ICPClassification(
                segment=ICPSegment.COST_RESTRUCTURING,
                confidence=layoff.confidence,
                confidence_score=conf_score,
                justification=(
                    f"Layoff of >{SEG1_LAYOFF_SHIFT_PCT * 100:.0f}% headcount "
                    f"in last {LAYOFF_DISQUALIFY_WINDOW}d shifts this Segment 1 "
                    "candidate to Segment 2 (cost pressure)."
                ),
            ), flags

        segment_1_rounds = {"series a", "series b", "seed"}
        is_preferred_round = (
            funding.round_type and funding.round_type.lower() in segment_1_rounds
        )
        amount_in_range = (
            funding.amount_usd is not None
            and SEG1_FUNDING_MIN_USD <= funding.amount_usd <= SEG1_FUNDING_MAX_USD
        )
        amount_unknown = funding.amount_usd is None
        geo_ok = _geo_qualifies_seg1(company)
        hc_ok = _headcount_in_range(company, SEG1_HC_MIN, SEG1_HC_MAX)
        roles_ok = (hiring.open_roles_count or 0) >= SEG1_MIN_ROLES

        conf_score = CONFIDENCE_SCORE[funding.confidence.value]

        # Confidence penalties for failed qualifying filters
        if not is_preferred_round:
            conf_score = max(0.0, conf_score - 0.25)
        if not amount_unknown and not amount_in_range:
            conf_score = max(0.0, conf_score - 0.20)
        if not geo_ok:
            conf_score = max(0.0, conf_score - 0.15)
        if hc_ok is False:
            conf_score = max(0.0, conf_score - 0.20)
        if hc_ok is None:
            conf_score = max(0.0, conf_score - 0.05)
        if not roles_ok:
            conf_score = max(0.0, conf_score - 0.10)
            flags.append("weak_hiring_velocity_signal")

        tier = (
            SignalConfidence.HIGH if conf_score >= 0.80
            else SignalConfidence.MEDIUM if conf_score >= 0.60
            else SignalConfidence.LOW
        )

        return ICPClassification(
            segment=ICPSegment.RECENTLY_FUNDED,
            confidence=tier,
            confidence_score=conf_score,
            justification=(
                f"{funding.round_type} closed {funding.days_since_funding}d ago. "
                "Fresh budget and runway pressure make engineering capacity a near-term decision."
            ),
        ), flags

    # ── Priority 5: Abstain ────────────────────────────────────────────────────
    return ICPClassification(
        segment=ICPSegment.ABSTAIN,
        confidence=SignalConfidence.LOW,
        confidence_score=0.30,
        justification=(
            "Insufficient signal to assign a segment with confidence ≥ 0.6. "
            "Generic exploratory outreach recommended."
        ),
    ), flags


def _infer_required_stacks(company: Company) -> list[str]:
    """
    Infer the tech stacks required for a Segment 4 engagement from detected
    technologies and AI-adjacent role titles.
    """
    bench_stacks = set(stack_names())
    required: set[str] = set()

    tech_lower = {t.lower() for t in company.detected_technologies}
    titles_lower = [t.lower() for t in company.open_role_titles]

    # Map detected technologies → bench stack names
    tech_to_stack = {
        "python": "python", "django": "python", "fastapi": "python", "flask": "python",
        "react": "frontend", "next.js": "frontend", "typescript": "frontend",
        "go": "go", "golang": "go",
        "dbt": "data", "snowflake": "data", "databricks": "data", "airflow": "data",
        "pytorch": "ml", "tensorflow": "ml", "langchain": "ml", "mlflow": "ml",
        "terraform": "infra", "kubernetes": "infra", "docker": "infra",
        "nestjs": "fullstack_nestjs",
    }
    for tech, stack in tech_to_stack.items():
        if any(tech in t for t in tech_lower):
            if stack in bench_stacks:
                required.add(stack)

    # AI-adjacent titles → ml stack
    if any(any(kw in title for kw in _AI_ROLE_KEYWORDS) for title in titles_lower):
        if "ml" in bench_stacks:
            required.add("ml")
        if "data" in bench_stacks:
            required.add("data")

    return list(required) if required else ["python"]  # default to python if nothing inferred


def _compute_honesty_flags(
    hiring: HiringSignal,
    ai_maturity: AIMaturitySignal,
    company: Company,
    icp: ICPClassification,
    existing_flags: list[str],
) -> list[str]:
    """
    Compile final honesty_flags list for the HiringSignalBrief.
    These flags gate the agent's language in outreach — agent must respect them.
    """
    flags = list(existing_flags)

    if hiring.open_roles_count is not None and hiring.open_roles_count < 5:
        if "weak_hiring_velocity_signal" not in flags:
            flags.append("weak_hiring_velocity_signal")

    if ai_maturity.score >= 2 and ai_maturity.confidence in (
        SignalConfidence.LOW, SignalConfidence.NONE
    ):
        if "weak_ai_maturity_signal" not in flags:
            flags.append("weak_ai_maturity_signal")

    if company.detected_technologies:
        flags.append("tech_stack_inferred_not_confirmed")

    if icp.segment == ICPSegment.ABSTAIN and "conflicting_segment_signals" not in flags:
        pass  # abstain alone is not a flag — it's a segment outcome

    return list(dict.fromkeys(flags))  # deduplicate preserving order


# ── Public API ────────────────────────────────────────────────────────────────

def compute_signals(company: Company) -> HiringSignalBrief:
    """
    Compute the full HiringSignalBrief from a populated Company record.
    Pure logic — no network calls, no LLM.
    """
    funding = _compute_funding(company)
    hiring = _compute_hiring(company)
    layoff = _compute_layoff(company)
    leadership = _compute_leadership(company)
    ai_maturity = _compute_ai_maturity(company)
    icp, raw_flags = _classify_icp(company, funding, hiring, layoff, leadership, ai_maturity)

    # Apply abstention gate: if confidence_score < threshold, override segment to ABSTAIN
    if icp.confidence_score < ABSTAIN_THRESHOLD and icp.segment not in (
        ICPSegment.ABSTAIN,
    ) and not icp.disqualified:
        icp = ICPClassification(
            segment=ICPSegment.ABSTAIN,
            confidence=SignalConfidence.LOW,
            confidence_score=icp.confidence_score,
            justification=(
                f"Segment {icp.segment.value} detected but confidence "
                f"{icp.confidence_score:.2f} is below the 0.6 abstention threshold. "
                "Generic exploratory outreach recommended."
            ),
        )

    honesty_flags = _compute_honesty_flags(hiring, ai_maturity, company, icp, raw_flags)

    # Staleness check
    stale = False
    stale_reason = None
    if company.open_roles_snapshot_date:
        snapshot_age = (date.today() - company.open_roles_snapshot_date).days
        if snapshot_age > 7:
            stale = True
            stale_reason = (
                f"Job post snapshot is {snapshot_age} days old. "
                "Hiring velocity numbers may not reflect current state."
            )

    domain = company.website
    if domain and domain.startswith("http"):
        from urllib.parse import urlparse
        domain = urlparse(domain).netloc or domain

    return HiringSignalBrief(
        company_name=company.name,
        generated_at=date.today(),
        funding=funding,
        hiring=hiring,
        layoff=layoff,
        leadership=leadership,
        ai_maturity=ai_maturity,
        icp=icp,
        prospect_domain=domain,
        honesty_flags=honesty_flags,
        data_is_stale=stale,
        stale_reason=stale_reason,
    )
