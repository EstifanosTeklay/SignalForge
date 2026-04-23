"""
agent/models/signals.py

Derived signals — everything inferred from Company raw data.
Nothing here is directly copied from a source; it is always computed.

Design principle:
  Every signal carries a confidence level so the agent's phrasing can
  calibrate automatically. "You tripled your engineering headcount" is only
  appropriate at HIGH confidence; at LOW confidence the agent must ask, not
  assert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


# ── Confidence ────────────────────────────────────────────────────────────────

class SignalConfidence(str, Enum):
    """
    Explicit confidence tier for every derived signal.

    HIGH   — multiple corroborating sources; agent may assert.
    MEDIUM — single strong source or two weak ones; agent may note with hedge.
    LOW    — one weak source or inferred by absence; agent must ask, not assert.
    NONE   — no usable signal; this field should be omitted from outreach.
    """
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"
    NONE   = "none"


# ── Individual Signals ────────────────────────────────────────────────────────

@dataclass
class FundingSignal:
    """
    Derived from Company.funding_rounds + Company.latest_funding.
    Window: rounds announced within the last 180 days qualify.
    """
    has_recent_funding: bool
    days_since_funding: Optional[int]        # None if no recent round
    round_type: Optional[str]                # "Series A", "Series B", etc.
    amount_usd: Optional[int]
    confidence: SignalConfidence
    justification: str                       # one sentence traceable to source


@dataclass
class HiringSignal:
    """
    Derived from job-post velocity: change in open_roles_count over ~60 days.
    Velocity thresholds (calibrated to challenge brief):
      HIGH   — roles tripled (3×) in 60 days
      MEDIUM — roles doubled (2×) in 60 days
      LOW    — modest growth or single snapshot (no velocity data)
      NONE   — no open roles or data absent
    """
    open_roles_count: Optional[int]
    velocity_ratio: Optional[float]          # current / prior snapshot; None if single snapshot
    has_ai_adjacent_roles: bool              # ML eng, LLM eng, applied scientist, etc.
    ai_adjacent_role_count: int
    confidence: SignalConfidence
    justification: str


@dataclass
class LayoffSignal:
    """
    Derived from Company.layoff_events.
    Window: events within the last 120 days are active signals.
    """
    has_recent_layoff: bool
    days_since_layoff: Optional[int]
    headcount_cut: Optional[int]
    percentage_cut: Optional[float]
    confidence: SignalConfidence
    justification: str


@dataclass
class LeadershipChangeSignal:
    """
    Derived from Company.leadership_changes.
    Window: appointments within the last 90 days.
    New CTO / VP Eng in this window = Segment 3 pitch opportunity.
    """
    has_recent_change: bool
    role: Optional[str]                      # e.g. "CTO"
    days_since_appointment: Optional[int]
    confidence: SignalConfidence
    justification: str


@dataclass
class AIMaturitySignal:
    """
    0–3 integer score per challenge spec, with per-input justification.

    Score semantics:
      0 — no public AI signal
      1 — weak signal (modern data stack or one low-weight input)
      2 — moderate signal (exec commentary + stack, or AI-adjacent roles)
      3 — strong signal (named AI leadership + multiple open AI roles + exec commitment)

    confidence reflects the evidence weight behind the score, not the score itself.
    A score of 2 backed by a single medium-weight input is phrased differently from
    a score of 2 backed by three high-weight inputs.
    """
    score: int                               # 0–3
    confidence: SignalConfidence

    # Per-input evidence (all optional — absent = not found / not checked)
    ai_adjacent_role_count: int = 0
    has_named_ai_leadership: bool = False
    has_github_ai_activity: Optional[bool] = None   # None = not checked
    has_exec_ai_commentary: bool = False
    has_modern_ml_stack: bool = False
    has_strategic_ai_comms: bool = False

    # Free-text justification — one sentence per input that contributed
    evidence_notes: list[str] = field(default_factory=list)


# ── ICP Segment ───────────────────────────────────────────────────────────────

class ICPSegment(str, Enum):
    """
    The four segments from the Tenacious ICP definition.
    ABSTAIN is used when confidence is below the 0.6 threshold — sends generic exploratory email.
    """
    RECENTLY_FUNDED    = "recently_funded"       # Segment 1
    COST_RESTRUCTURING = "cost_restructuring"    # Segment 2
    LEADERSHIP_CHANGE  = "leadership_change"     # Segment 3
    CAPABILITY_GAP     = "capability_gap"        # Segment 4
    ABSTAIN            = "abstain"               # confidence < 0.6 — generic outreach only


# Map SignalConfidence tiers to numeric scores used for the 0.6 abstention threshold
CONFIDENCE_SCORE: dict[str, float] = {
    "high":   0.85,
    "medium": 0.65,
    "low":    0.40,
    "none":   0.0,
}

# Abstention threshold per icp_definition.md — below this, send generic exploratory email
ABSTAIN_THRESHOLD = 0.6


@dataclass
class ICPClassification:
    """
    Segment assignment with confidence.
    confidence_score < ABSTAIN_THRESHOLD (0.6) triggers the abstention path.
    """
    segment: ICPSegment
    confidence: SignalConfidence
    confidence_score: float = 0.0        # numeric 0–1; used for abstention gate
    disqualified: bool = False
    disqualification_reason: Optional[str] = None
    justification: str = ""


# ── Top-Level Brief ───────────────────────────────────────────────────────────

@dataclass
class HiringSignalBrief:
    """
    The complete signal picture for one prospect.
    Serialises to hiring_signal_brief.json as required by the challenge spec.

    This is the input the agent reads before composing outreach.
    It must never contain claims the raw Company record cannot support.
    """
    company_name: str
    generated_at: date

    funding:          FundingSignal
    hiring:           HiringSignal
    layoff:           LayoffSignal
    leadership:       LeadershipChangeSignal
    ai_maturity:      AIMaturitySignal
    icp:              ICPClassification

    # Optional domain key — matches hiring_signal_brief.schema.json prospect_domain
    prospect_domain: Optional[str] = None

    # Honesty flags surfaced by the enrichment pipeline for the agent to respect
    honesty_flags: list[str] = field(default_factory=list)

    # Overall data freshness flag — if any source is stale, agent should hedge
    data_is_stale: bool = False
    stale_reason: Optional[str] = None
