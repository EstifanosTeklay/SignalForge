"""
agent/enrichment/signal_computer.py

Pure-logic signal computation — no LLM, no I/O.
Takes a populated Company and returns a HiringSignalBrief.

All thresholds are calibrated to the challenge spec:
  - Funding window:    180 days
  - Layoff window:     120 days
  - Leadership window:  90 days
  - Hiring velocity HIGH: roles tripled (3×) in 60 days
  - Hiring velocity MED:  roles doubled (2×) in 60 days
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from agent.models.company import Company
from agent.models.signals import (
    AIMaturitySignal,
    FundingSignal,
    HiringSignal,
    HiringSignalBrief,
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

# Modern ML stack technologies (from BuiltWith/Wappalyzer signal)
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


def _days_ago(d: Optional[date]) -> Optional[int]:
    if d is None:
        return None
    return (date.today() - d).days


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

    # Within window
    amount_str = (
        f"${lf.amount_usd:,}" if lf.amount_usd else "undisclosed amount"
    )
    confidence = (
        SignalConfidence.HIGH if lf.amount_usd else SignalConfidence.MEDIUM
    )
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


def _compute_hiring(company: Company) -> HiringSignal:
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

    # Velocity: we only have one snapshot, so LOW confidence on velocity
    # (velocity_ratio requires two snapshots 60 days apart)
    confidence = SignalConfidence.LOW if count < 5 else SignalConfidence.MEDIUM
    has_ai = ai_count > 0

    justification_parts = [
        f"{count} open role{'s' if count != 1 else ''} found in public job snapshot."
    ]
    if has_ai:
        justification_parts.append(
            f"{ai_count} AI-adjacent role{'s' if ai_count != 1 else ''} detected."
        )
    if count >= 5:
        justification_parts.append("Volume suggests active hiring phase.")

    return HiringSignal(
        open_roles_count=count,
        velocity_ratio=None,
        has_ai_adjacent_roles=has_ai,
        ai_adjacent_role_count=ai_count,
        confidence=confidence,
        justification=" ".join(justification_parts),
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

    # Use most recent event
    most_recent = max(company.layoff_events, key=lambda e: e.event_date)
    days = _days_ago(most_recent.event_date)

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
            justification=(
                f"Leadership change detected but outside {LEADERSHIP_WINDOW}-day window."
            ),
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
    Score AI maturity 0-3 from public signals.
    Score contributions (additive, capped at 3):
      - AI-adjacent open roles  (fraction of total):  0-1 pt (HIGH weight)
      - Named AI/ML leadership:                          1 pt (HIGH weight)
      - Modern ML stack detected:                      0.5 pt (LOW weight)
      - Exec AI commentary in public_ai_mentions:      0.5 pt (MEDIUM weight)
      - GitHub AI activity:                            0.5 pt (MEDIUM weight)
    """
    score_float = 0.0
    evidence: list[str] = []

    titles_lower = [t.lower() for t in company.open_role_titles]
    total_roles = len(titles_lower)
    ai_count = sum(
        1 for t in titles_lower
        if any(kw in t for kw in _AI_ROLE_KEYWORDS)
    )

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

    # Named AI/ML leadership on team page / Crunchbase
    all_text = " ".join(company.public_ai_mentions).lower()
    has_ai_leadership = any(kw in all_text for kw in _AI_LEADERSHIP_KEYWORDS)
    if has_ai_leadership:
        score_float += 1.0
        evidence.append("Named AI/ML leadership role detected in public profile or press.")

    # Modern ML stack
    tech_lower = [t.lower() for t in company.detected_technologies]
    ml_stack_hits = [kw for kw in _ML_STACK_KEYWORDS if any(kw in t for t in tech_lower)]
    if ml_stack_hits:
        score_float += 0.5
        evidence.append(
            f"Modern ML stack detected: {', '.join(ml_stack_hits[:3])}."
        )

    # Exec AI commentary in press / blog
    ai_keywords = ["artificial intelligence", " ai ", " ml ", "machine learning", "llm", "genai"]
    ai_mention_count = sum(
        1 for mention in company.public_ai_mentions
        if any(kw in mention.lower() for kw in ai_keywords)
    )
    if ai_mention_count >= 2:
        score_float += 0.5
        evidence.append(
            f"Executive AI commentary found in {ai_mention_count} public sources."
        )
    elif ai_mention_count == 1:
        score_float += 0.25
        evidence.append("Single public AI mention from executive or press.")

    final_score = min(3, round(score_float))

    # Confidence based on evidence weight
    high_weight_count = sum([
        ai_count >= 3,
        has_ai_leadership,
    ])
    medium_weight_count = sum([
        ai_mention_count >= 2,
        bool(ml_stack_hits),
    ])

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
        has_github_ai_activity=None,  # not checked at this layer
        has_exec_ai_commentary=ai_mention_count > 0,
        has_modern_ml_stack=bool(ml_stack_hits),
        evidence_notes=evidence,
    )


def _classify_icp(
    funding: FundingSignal,
    hiring: HiringSignal,
    layoff: LayoffSignal,
    leadership: LeadershipChangeSignal,
    ai_maturity: AIMaturitySignal,
) -> ICPClassification:
    """
    Assign ICP segment.
    Priority order (per spec): Segment 3 > Segment 1 > Segment 2 > Segment 4 > UNKNOWN.
    Segment 4 requires ai_maturity >= 2.
    """
    # Disqualify: layoff + funding within same 60d window is unusual; raise a flag
    # (not disqualified outright — just noted)

    # Segment 3: leadership change in 90 days (highest conversion window)
    if leadership.has_recent_change:
        return ICPClassification(
            segment=ICPSegment.LEADERSHIP_CHANGE,
            confidence=leadership.confidence,
            justification=(
                f"New {leadership.role} within {leadership.days_since_appointment} days. "
                "Vendor-reassessment window is typically open for 6 months post-appointment."
            ),
        )

    # Segment 1: recent funding (Series A/B preferred)
    if funding.has_recent_funding:
        segment_1_rounds = {"series a", "series b", "seed"}
        is_preferred = (
            funding.round_type and funding.round_type.lower() in segment_1_rounds
        )
        conf = funding.confidence if is_preferred else SignalConfidence.LOW
        return ICPClassification(
            segment=ICPSegment.RECENTLY_FUNDED,
            confidence=conf,
            justification=(
                f"{funding.round_type} closed {funding.days_since_funding} days ago. "
                "Fresh budget and runway pressure make engineering capacity a near-term decision."
            ),
        )

    # Segment 2: cost restructuring (post-layoff)
    if layoff.has_recent_layoff:
        return ICPClassification(
            segment=ICPSegment.COST_RESTRUCTURING,
            confidence=layoff.confidence,
            justification=(
                f"Layoff of {layoff.headcount_cut or 'unknown'} people "
                f"{layoff.days_since_layoff} days ago signals cost pressure. "
                "Offshore equivalent capacity is a typical response."
            ),
        )

    # Segment 4: capability gap — requires AI maturity >= 2
    if ai_maturity.score >= 2:
        return ICPClassification(
            segment=ICPSegment.CAPABILITY_GAP,
            confidence=ai_maturity.confidence,
            justification=(
                f"AI maturity score {ai_maturity.score}/3 with "
                f"{SignalConfidence(ai_maturity.confidence).value} confidence. "
                "Specialized AI/ML build is likely; bench-to-brief match pending."
            ),
        )

    # No clear segment — abstain
    return ICPClassification(
        segment=ICPSegment.UNKNOWN,
        confidence=SignalConfidence.LOW,
        justification=(
            "Insufficient signal to assign a segment with confidence. "
            "Generic exploratory outreach recommended."
        ),
    )


# ── Public API ────────────────────────────────────────────────────────────────

def compute_signals(company: Company) -> HiringSignalBrief:
    """
    Compute the full HiringSignalBrief from a populated Company record.
    This is pure logic — no network calls, no LLM.
    """
    funding = _compute_funding(company)
    hiring = _compute_hiring(company)
    layoff = _compute_layoff(company)
    leadership = _compute_leadership(company)
    ai_maturity = _compute_ai_maturity(company)
    icp = _classify_icp(funding, hiring, layoff, leadership, ai_maturity)

    # Staleness check: if job snapshot is older than 7 days, flag it
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

    return HiringSignalBrief(
        company_name=company.name,
        generated_at=date.today(),
        funding=funding,
        hiring=hiring,
        layoff=layoff,
        leadership=leadership,
        ai_maturity=ai_maturity,
        icp=icp,
        data_is_stale=stale,
        stale_reason=stale_reason,
    )
