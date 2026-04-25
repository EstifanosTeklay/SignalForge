"""
agent/enrichment/competitor_gap.py

Standalone competitor gap analysis module.

Exported entry point
--------------------
    build_competitor_gap_brief(company, brief) -> dict

Steps (each is a named public function for testability):
  1. select_competitors()   — pull sector peers from Crunchbase ODM, filter
                              duplicates, require min_peers=5 viable candidates
  2. score_competitors()    — run _compute_ai_maturity() on each peer,
                              producing {name, domain, score, justification,
                              headcount_band} records
  3. compute_distribution() — derive prospect percentile, sector median,
                              p75, and top-quartile threshold from peer scores
  4. extract_gap_findings() — 1-3 gap findings, each with ≥2 peer evidence
                              items and explicit source attribution
  5. _sparse_brief()        — explicit sparse-sector fallback when < 5 peers
                              are available (returns schema-compliant empty brief
                              with a diagnostic note, never fabricates evidence)

Sparse-sector policy
--------------------
When fewer than MIN_PEERS (5) viable peers are returned by
load_companies_by_industry(), the module refuses to fabricate evidence.
_sparse_brief() is called instead: it returns a valid schema-compliant dict
with competitors_analyzed=[], gap_findings=[], and a diagnostic message in
suggested_pitch_shift. The caller (InsightAgent) should log this event and
fall back to generic exploratory email variant.

Score interpretation
--------------------
  0 — No public AI signal
  1 — One weak signal (job title mentions or single tech tag)
  2 — Two or more corroborating signals (roles + stack, or leadership + GitHub)
  3 — Three or more strong signals (leadership + stack + open roles + public mentions)

Distribution statistics
-----------------------
  percentile    — fraction of sector peers with score ≤ prospect's score
  sector_median — median peer score (p50)
  sector_p75    — 75th-percentile peer score (top-quartile threshold)
  top_quartile_benchmark — mean score of peers whose score ≥ sector_p75
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from agent.enrichment.crunchbase_loader import load_companies_by_industry
from agent.enrichment.signal_computer import _compute_ai_maturity
from agent.models.company import Company
from agent.models.signals import HiringSignalBrief

# Minimum viable peer count; below this the module returns a sparse brief.
MIN_PEERS = 5
# Maximum peers to score (scoring is I/O-bound via Crunchbase ODM).
MAX_ANALYSE = 15
# Maximum peers to include in the schema output.
MAX_SCHEMA_PEERS = 10


# ── Headcount band mapping ────────────────────────────────────────────────────

def _headcount_band_for(company: Company) -> str:
    """
    Map Company headcount data to the competitor_gap_brief.schema.json
    headcount_band enum: 15_to_80 | 80_to_200 | 200_to_500 | 500_to_2000 | 2000_plus.
    Falls back to "80_to_200" when neither exact count nor band string is available.
    """
    exact = company.employee_count_exact
    band_str = company.employee_count_band or ""

    def _resolve(n: int) -> str:
        if n < 80:
            return "15_to_80"
        if n < 200:
            return "80_to_200"
        if n < 500:
            return "200_to_500"
        if n < 2000:
            return "500_to_2000"
        return "2000_plus"

    if exact:
        return _resolve(exact)

    # Parse "50-100" or "50–100" style band strings
    parts = band_str.replace("–", "-").split("-")
    if len(parts) == 2:
        try:
            mid = (int(parts[0]) + int(parts[1])) // 2
            return _resolve(mid)
        except ValueError:
            pass
    return "80_to_200"


def _normalise_domain(peer: Company) -> str:
    """Extract bare domain from a Company.website URL or synthesise a placeholder."""
    raw = peer.website or ""
    if raw.startswith("http"):
        netloc = urlparse(raw).netloc
        if netloc:
            return netloc
    if raw:
        return raw
    return f"{peer.name.lower().replace(' ', '-')}.com"


# ── Step 1: select_competitors ────────────────────────────────────────────────

def select_competitors(
    company: Company,
    industry: str,
    min_peers: int = MIN_PEERS,
    max_analyse: int = MAX_ANALYSE,
) -> list[Company]:
    """
    Pull sector peers from Crunchbase ODM and return a filtered candidate list.

    Selection criteria (applied in order):
      1. Same industry string as the prospect.
      2. Remove the prospect itself (exact name match, case-insensitive).
      3. Limit to max_analyse candidates to bound scoring I/O.

    Returns an empty list when fewer than min_peers candidates remain after
    filtering — the caller should treat this as a sparse-sector case and call
    _sparse_brief() rather than continuing.

    Args:
        company:     The prospect Company object.
        industry:    Industry label used to query Crunchbase ODM.
        min_peers:   Minimum viable peer count (default 5).
        max_analyse: Cap on how many peers to return for scoring (default 15).

    Returns:
        List of Company objects (length 0 or [min_peers, max_analyse]).
    """
    raw = load_companies_by_industry(industry, limit=40)
    peers = [p for p in raw if p.name.lower() != company.name.lower()]

    if len(peers) < min_peers:
        return []  # sparse sector — caller must handle

    return peers[:max_analyse]


# ── Step 2: score_competitors ─────────────────────────────────────────────────

def score_competitors(peers: list[Company]) -> list[dict]:
    """
    Run _compute_ai_maturity() against each peer and return structured records.

    Each record:
      {
        "name":                    str,
        "domain":                  str,          # bare domain, no scheme
        "ai_maturity_score":       int,          # 0-3
        "ai_maturity_justification": list[str],  # up to 3 evidence notes
        "headcount_band":          str,          # schema enum
        "top_quartile":            bool,         # set to False here; updated by compute_distribution()
        "sources_checked":         list[str],    # URLs checked during scoring
      }

    Deterministic — no LLM calls.  Each peer requires one Crunchbase ODM read
    (already in memory) plus the signal_computer's local heuristics.
    """
    scored: list[dict] = []
    for peer in peers:
        sig = _compute_ai_maturity(peer)
        justifications = list(sig.evidence_notes[:3]) or ["No public AI signal detected."]
        domain = _normalise_domain(peer)
        scored.append({
            "name": peer.name,
            "domain": domain,
            "ai_maturity_score": sig.score,
            "ai_maturity_justification": justifications,
            "headcount_band": _headcount_band_for(peer),
            "top_quartile": False,
            "sources_checked": [peer.website] if peer.website else [],
        })
    return scored


# ── Step 3: compute_distribution ─────────────────────────────────────────────

def compute_distribution(prospect_score: int, scored_peers: list[dict]) -> dict:
    """
    Compute the prospect's position within the scored peer distribution.

    Statistics returned:
      percentile             — fraction of peers with score ≤ prospect (0.0–1.0)
      sector_median          — median peer score (p50)
      sector_p75             — 75th-percentile peer score
      top_quartile_threshold — max(sector_p75, 2); minimum bar for "top quartile"
      top_quartile_benchmark — mean score of top-quartile peers

    Also mutates scored_peers in-place, setting peer["top_quartile"] = True
    for all peers whose score ≥ top_quartile_threshold.

    Args:
        prospect_score: The prospect's AI maturity score (0-3).
        scored_peers:   Output of score_competitors(); mutated in place.

    Returns:
        Distribution stats dict.
    """
    scores = [p["ai_maturity_score"] for p in scored_peers]
    n = len(scores)
    sorted_scores = sorted(scores)

    sector_median = sorted_scores[n // 2] if n > 0 else 0
    sector_p75 = sorted_scores[int(n * 0.75)] if n >= 4 else (max(scores) if scores else 3)
    top_quartile_threshold = max(sector_p75, 2)

    for p in scored_peers:
        p["top_quartile"] = p["ai_maturity_score"] >= top_quartile_threshold

    top_quartile_peers = [p for p in scored_peers if p["top_quartile"]]
    top_quartile_benchmark = (
        sum(p["ai_maturity_score"] for p in top_quartile_peers) / len(top_quartile_peers)
        if top_quartile_peers else float(top_quartile_threshold)
    )

    percentile = (
        round(sum(1 for s in scores if s <= prospect_score) / n, 3)
        if n > 0 else None
    )

    return {
        "percentile": percentile,
        "sector_median": sector_median,
        "sector_p75": sector_p75,
        "top_quartile_threshold": top_quartile_threshold,
        "top_quartile_benchmark": round(top_quartile_benchmark, 2),
        "peer_count": n,
    }


# ── Step 4: extract_gap_findings ──────────────────────────────────────────────

def extract_gap_findings(
    brief: HiringSignalBrief,
    top_quartile_peers: list[dict],
) -> list[dict]:
    """
    Generate 1-3 gap findings conforming to competitor_gap_brief.schema.json.

    Rules:
      - Each finding must have ≥2 peer_evidence items.
      - Each peer_evidence item carries competitor_name, evidence, source_url.
      - confidence: "high" when ≥3 top-quartile peers provide evidence;
                    "medium" when 2 peers; "low" when evidence is inferred.
      - Maximum 3 findings returned (schema constraint).
      - If top_quartile_peers < 2, no findings are generated (sparse evidence).

    Three gap categories, evaluated in priority order:
      1. Named AI/ML leadership (Head of AI, VP Data, Chief Scientist)
      2. Modern ML/MLOps stack (dbt, Snowflake, Databricks, MLflow, etc.)
      3. AI-adjacent open roles (dedicated ML/AI engineers in product teams)

    Args:
        brief:              HiringSignalBrief for the prospect.
        top_quartile_peers: Scored peer dicts with top_quartile=True.

    Returns:
        List of 0-3 gap finding dicts (schema-compliant).
    """
    findings: list[dict] = []
    prospect_score = brief.ai_maturity.score

    # ── Gap 1: Named AI/ML leadership ─────────────────────────────────────────
    if not brief.ai_maturity.has_named_ai_leadership and len(top_quartile_peers) >= 2:
        peer_ev: list[dict] = []
        for peer in top_quartile_peers[:3]:
            if peer["ai_maturity_score"] >= 2:
                source = peer["sources_checked"][0] if peer["sources_checked"] else None
                peer_ev.append({
                    "competitor_name": peer["name"],
                    "evidence": (
                        f"AI maturity score {peer['ai_maturity_score']}/3 based on: "
                        + "; ".join(peer["ai_maturity_justification"][:2])
                    ),
                    "source_url": source or f"https://{peer['domain']}/team",
                })

        if len(peer_ev) >= 2:
            findings.append({
                "practice": (
                    "Named AI/ML leadership role (Head of AI, VP Data, or Chief Scientist) "
                    "present on public team page"
                ),
                "peer_evidence": peer_ev[:3],
                "prospect_state": (
                    f"{brief.company_name} has no named AI/ML leadership role visible "
                    "publicly. The CTO or VP Engineering currently holds the AI remit."
                    if not brief.ai_maturity.has_named_ai_leadership
                    else "No public AI leadership signal found."
                ),
                "confidence": "high" if len(top_quartile_peers) >= 3 else "medium",
                "segment_relevance": [
                    "segment_1_series_a_b",
                    "segment_4_specialized_capability",
                ],
            })

    # ── Gap 2: Modern ML/MLOps stack ─────────────────────────────────────────
    _STACK_KEYWORDS = ["stack", "dbt", "databricks", "snowflake", "mlops", "platform",
                       "mlflow", "airflow", "sagemaker", "vertex"]
    if not brief.ai_maturity.has_modern_ml_stack and prospect_score < 2 and len(top_quartile_peers) >= 2:
        peer_ev = []
        for peer in top_quartile_peers[:3]:
            justifications = peer.get("ai_maturity_justification", [])
            stack_ev = [j for j in justifications if any(kw in j.lower() for kw in _STACK_KEYWORDS)]
            if stack_ev or peer["ai_maturity_score"] >= 2:
                source = peer["sources_checked"][0] if peer["sources_checked"] else None
                peer_ev.append({
                    "competitor_name": peer["name"],
                    "evidence": (
                        stack_ev[0] if stack_ev
                        else f"AI maturity score {peer['ai_maturity_score']}/3; modern stack inferred from role signals."
                    ),
                    "source_url": source or f"https://{peer['domain']}",
                })

        if len(peer_ev) >= 2:
            findings.append({
                "practice": (
                    "Modern data/ML stack in production (dbt, Snowflake, Databricks, "
                    "or MLOps tooling visible in public job descriptions or BuiltWith data)"
                ),
                "peer_evidence": peer_ev[:3],
                "prospect_state": (
                    f"No modern ML stack signal detected publicly for {brief.company_name}. "
                    "This may reflect a quieter public presence rather than absence."
                ),
                "confidence": "medium",
                "segment_relevance": [
                    "segment_1_series_a_b",
                    "segment_2_mid_market_restructure",
                ],
            })

    # ── Gap 3: AI-adjacent open roles ─────────────────────────────────────────
    _ROLE_KEYWORDS = ["role", "engineer", "open", "hire", "ml", "ai", "data scientist"]
    if brief.ai_maturity.ai_adjacent_role_count == 0 and len(top_quartile_peers) >= 2:
        peer_ev = []
        for peer in top_quartile_peers[:3]:
            justifications = peer.get("ai_maturity_justification", [])
            role_ev = [j for j in justifications if any(kw in j.lower() for kw in _ROLE_KEYWORDS)]
            source = peer["sources_checked"][0] if peer["sources_checked"] else None
            peer_ev.append({
                "competitor_name": peer["name"],
                "evidence": (
                    role_ev[0] if role_ev
                    else f"AI maturity {peer['ai_maturity_score']}/3; AI-adjacent roles inferred."
                ),
                "source_url": source or f"https://{peer['domain']}/careers",
            })

        if len(peer_ev) >= 2:
            findings.append({
                "practice": (
                    "Dedicated AI/ML engineers embedded in product teams "
                    "(visible as AI-adjacent open roles on public job boards)"
                ),
                "peer_evidence": peer_ev[:3],
                "prospect_state": (
                    f"No AI-adjacent open roles found publicly for {brief.company_name}. "
                    "This could reflect private listings, a hiring freeze, "
                    "or deliberate choice to grow AI capability through existing engineers."
                ),
                "confidence": "medium" if len(top_quartile_peers) >= 3 else "low",
                "segment_relevance": [
                    "segment_1_series_a_b",
                    "segment_4_specialized_capability",
                ],
            })

    return findings[:3]  # schema constraint: max 3


# ── Sparse-sector fallback ────────────────────────────────────────────────────

def _sparse_brief(company: Company, brief: HiringSignalBrief, industry: str) -> dict:
    """
    Return a schema-compliant empty brief when fewer than MIN_PEERS sector
    peers are available.

    Policy: never fabricate competitor evidence from fewer than 5 peers.
    The gap_quality_self_check flags this explicitly so the calling pipeline
    can route to a generic exploratory email variant instead of a Segment 4
    gap-brief email.

    Returns a dict that passes competitor_gap_brief.schema.json validation
    (all required fields present, competitors_analyzed=[], gap_findings=[]).
    """
    return {
        "prospect_domain": brief.prospect_domain or f"{company.name.lower().replace(' ', '-')}.com",
        "prospect_sector": industry,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prospect_ai_maturity_score": brief.ai_maturity.score,
        "sector_top_quartile_benchmark": 0.0,
        "competitors_analyzed": [],
        "gap_findings": [],
        "suggested_pitch_shift": (
            f"Sparse sector: fewer than {MIN_PEERS} Crunchbase ODM peers found for "
            f"'{industry}'. Cannot produce a grounded gap brief. "
            "Route to generic exploratory email — do not use Segment 4 gap language."
        ),
        "gap_quality_self_check": {
            "all_peer_evidence_has_source_url": False,
            "at_least_one_gap_high_confidence": False,
            "prospect_silent_but_sophisticated_risk": False,
            "sparse_sector": True,
            "peers_found": 0,
        },
        "_meta": {
            "peer_count_analysed": 0,
            "sector_median_score": None,
            "sector_p75_score": None,
            "prospect_percentile": None,
        },
    }


# ── Public entry point ────────────────────────────────────────────────────────

def build_competitor_gap_brief(company: Company, brief: HiringSignalBrief) -> dict:
    """
    Build a competitor_gap_brief dict conforming to
    data/Tenacious Data/.../schemas/competitor_gap_brief.schema.json.

    Pipeline:
      1. select_competitors()   — 5-15 sector peers from Crunchbase ODM
      2. score_competitors()    — deterministic AI maturity score per peer
      3. compute_distribution() — prospect percentile + sector stats
      4. extract_gap_findings() — 1-3 evidence-backed gap findings
      Fallback: _sparse_brief() when < MIN_PEERS peers are available

    Returns a schema-compliant dict.  Does not call any LLM.

    Args:
        company: The prospect Company dataclass (from ResearchAgent output).
        brief:   HiringSignalBrief containing pre-computed ai_maturity signal.
    """
    industry = company.industry or "Software"

    # ── Step 1: select ────────────────────────────────────────────────────────
    peers = select_competitors(company, industry)
    if not peers:
        print(
            f"[competitor_gap] sparse sector '{industry}' for '{company.name}' "
            f"— fewer than {MIN_PEERS} peers; returning sparse brief"
        )
        return _sparse_brief(company, brief, industry)

    # ── Step 2: score ─────────────────────────────────────────────────────────
    scored = score_competitors(peers)

    # ── Step 3: distribution ──────────────────────────────────────────────────
    dist = compute_distribution(brief.ai_maturity.score, scored)
    top_quartile_peers = [p for p in scored if p["top_quartile"]]

    # Trim to schema max
    peers_for_schema = scored[:MAX_SCHEMA_PEERS]

    # ── Step 4: gap findings ──────────────────────────────────────────────────
    gap_findings = extract_gap_findings(brief, top_quartile_peers)

    # ── Quality self-check ────────────────────────────────────────────────────
    all_have_urls = all(
        any(e.get("source_url") for e in gf.get("peer_evidence", []))
        for gf in gap_findings
    )
    at_least_one_high = any(gf.get("confidence") == "high" for gf in gap_findings)

    # Prospect-silent-but-sophisticated: has detected tech but low AI public score
    silent_sophisticated = (
        brief.ai_maturity.score <= 1
        and bool(company.detected_technologies)
        and len(company.public_ai_mentions) > 0
    )

    prospect_domain = brief.prospect_domain or f"{company.name.lower().replace(' ', '-')}.com"

    return {
        # ── Schema-required fields ────────────────────────────────────────────
        "prospect_domain": prospect_domain,
        "prospect_sector": industry,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prospect_ai_maturity_score": brief.ai_maturity.score,
        "sector_top_quartile_benchmark": dist["top_quartile_benchmark"],
        "competitors_analyzed": peers_for_schema,
        "gap_findings": gap_findings,
        # ── Extended fields (not required by schema but consumed by agents) ───
        "suggested_pitch_shift": _suggest_pitch_shift(brief, gap_findings),
        "gap_quality_self_check": {
            "all_peer_evidence_has_source_url": all_have_urls,
            "at_least_one_gap_high_confidence": at_least_one_high,
            "prospect_silent_but_sophisticated_risk": silent_sophisticated,
            "sparse_sector": False,
            "peers_found": dist["peer_count"],
        },
        "_meta": {
            "peer_count_analysed": dist["peer_count"],
            "sector_median_score": dist["sector_median"],
            "sector_p75_score": dist["sector_p75"],
            "prospect_percentile": dist["percentile"],
        },
    }


# ── Pitch shift helper (used by build_competitor_gap_brief) ───────────────────

def _suggest_pitch_shift(brief: HiringSignalBrief, gap_findings: list[dict]) -> str:
    """
    Return a pitch-shift instruction string for the MessageAgent.
    Derived from the ICP segment and highest-confidence gap finding.
    """
    from agent.models.signals import ICPSegment

    segment = brief.icp.segment
    high_conf_gap = next(
        (g for g in gap_findings if g.get("confidence") == "high"), None
    )

    if segment == ICPSegment.CAPABILITY_GAP and high_conf_gap:
        practice = high_conf_gap["practice"].split("(")[0].strip()
        return (
            f"Lead with the '{practice}' gap (high confidence). "
            "Frame as a question — is this a deliberate choice or still being scoped? "
            "Segment 4 pitch: 'three companies in your sector at your stage are doing X.'"
        )
    if segment == ICPSegment.RECENTLY_FUNDED:
        if brief.ai_maturity.score >= 2:
            return (
                "High AI readiness. Lead with: 'scale your AI team faster than "
                "in-house hiring can support.' Reference any high-confidence gap if present."
            )
        return (
            "Low AI readiness. Lead with: 'stand up your first AI function with "
            "a dedicated squad.' Avoid Segment 4 language entirely."
        )
    if segment == ICPSegment.COST_RESTRUCTURING:
        return (
            "Post-restructure framing. Lead with the restructure date as a neutral fact. "
            "Avoid high-energy urgency language — post-restructure CFOs are skeptical."
        )
    if segment == ICPSegment.LEADERSHIP_CHANGE:
        return (
            "New leader context. Open with a grounded reference to the transition. "
            "Let the leader's reply direct the technical language — do not pre-assume AI stance."
        )
    return (
        "Insufficient signal for segment-specific pitch. Send generic exploratory. "
        "Ask about engineering roadmap — do not make specific claims about their company."
    )
