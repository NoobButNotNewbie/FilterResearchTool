"""Data models for FilterTool."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PaperStatus(str, Enum):
    """Lifecycle status of a paper in the pipeline.
    
    Papers are NEVER deleted. Status tracks the furthest stage reached.
    Old screening decisions are preserved in screening_decisions list.
    """
    NEW = "new"
    DEDUPED = "deduped"             # survived dedup
    DUPLICATE = "duplicate"         # removed by dedup (link to canonical)
    RULE_INCLUDED = "rule_included" # passed rule filter
    RULE_EXCLUDED = "rule_excluded" # failed rule filter
    REVIEW_NEEDED = "review_needed"
    SEMANTIC_HIGH = "semantic_high"
    SEMANTIC_REVIEW = "semantic_review"
    SEMANTIC_LOW = "semantic_low"
    VERIFIED = "verified"
    UNVERIFIED = "unverified"       # couldn't reach APIs
    VERIFY_ERROR = "verify_error"   # API error ≠ paper doesn't exist
    HUMAN_INCLUDED = "human_included"
    HUMAN_EXCLUDED = "human_excluded"
    FULLTEXT_NOT_RETRIEVED = "fulltext_not_retrieved"
    FULLTEXT_ASSESSED = "fulltext_assessed"
    FULLTEXT_EXCLUDED = "fulltext_excluded"
    INCLUDED = "included"
    CLASSIFIED = "classified"
    ERROR = "error"


class Source(str, Enum):
    SEMANTIC_SCHOLAR = "semantic_scholar"
    OPENALEX = "openalex"
    CROSSREF = "crossref"
    DBLP = "dblp"
    ARXIV = "arxiv"
    CITATION = "citation"           # discovered via citation expansion
    MANUAL = "manual"


# ---------------------------------------------------------------------------
# Screening decision record
# ---------------------------------------------------------------------------

@dataclass
class ScreeningDecision:
    """Immutable record of a single screening decision."""
    stage: str          # e.g. "rule_filter", "semantic_filter", "verification"
    decision: str       # e.g. "included", "excluded", "high", "review", "low"
    reason: str         # human-readable explanation
    score: float | None = None
    reason_code: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ScreeningDecision:
        return cls(**d)


# ---------------------------------------------------------------------------
# Paper
# ---------------------------------------------------------------------------

@dataclass
class Paper:
    """A single literature entry tracked through the pipeline.
    
    Design principles:
    - Papers are never deleted, only status changes
    - Every screening decision is recorded with reason + score
    - Multiple sources can contribute to the same paper
    - Classification supports multi-label (a paper can be jailbreak + prompt_optimization)
    """
    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # --- Core metadata ---
    title: str = ""
    title_normalized: str = ""          # lowered, stripped, for dedup
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    doi_normalized: str | None = None  # lowered, prefix-stripped
    abstract: str | None = None
    url: str | None = None
    venue: str | None = None
    publication_type: str | None = None  # journal, conference, preprint

    # --- Source tracking ---
    sources: list[str] = field(default_factory=list)
    source_ids: dict[str, str] = field(default_factory=dict)
    # e.g. {"semantic_scholar": "abc123", "openalex": "W456"}

    # --- Pipeline status ---
    status: str = PaperStatus.NEW.value
    duplicate_of: str | None = None  # ID of canonical paper if duplicate

    # --- Screening audit trail ---
    screening_decisions: list[dict] = field(default_factory=list)
    # Each entry is a ScreeningDecision.to_dict()

    # --- Semantic filtering ---
    semantic_score: float | None = None
    semantic_label: str | None = None  # HIGH / REVIEW / LOW

    # --- Keyword match info ---
    keyword_hits: list[str] = field(default_factory=list)
    keyword_hit_count: int = 0

    # --- Human screening ---
    screening_stage: str | None = None
    human_decision: str | None = None
    reason_code: str | None = None
    screening_note: str | None = None
    reviewer: str | None = None
    review_date: str | None = None

    # --- Verification ---
    verification_results: dict[str, dict] = field(default_factory=dict)
    # source -> {"verified": bool, "matched_fields": [...], "mismatched_fields": [...], "error": null}

    # --- Classification (multi-label) ---
    attack_methods: list[str] = field(default_factory=list)
    attack_stages: list[str] = field(default_factory=list)
    optimization_techniques: list[str] = field(default_factory=list)
    attack_methods_auto: list[str] = field(default_factory=list)
    attack_methods_manual: list[str] = field(default_factory=list)
    attack_stages_auto: list[str] = field(default_factory=list)
    attack_stages_manual: list[str] = field(default_factory=list)
    optimization_techniques_auto: list[str] = field(default_factory=list)
    optimization_techniques_manual: list[str] = field(default_factory=list)
    optimization_objectives_auto: list[str] = field(default_factory=list)
    optimization_objectives_manual: list[str] = field(default_factory=list)
    baseline_compared: str | None = None
    measured: str | None = None

    # --- Citation expansion ---
    reference_dois: list[str] = field(default_factory=list)
    cited_by_dois: list[str] = field(default_factory=list)
    citation_discoveries: list[dict] = field(default_factory=list)
    citation_expanded: bool = False

    # --- Timestamps ---
    added_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # ----- Methods -----

    def add_screening_decision(self, stage: str, decision: str, reason: str,
                                score: float | None = None,
                                reason_code: str | None = None) -> None:
        """Append a screening decision to the audit trail."""
        sd = ScreeningDecision(
            stage=stage, decision=decision, reason=reason, score=score,
            reason_code=reason_code,
        )
        self.screening_decisions.append(sd.to_dict())
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def add_source(self, source: str, external_id: str = "") -> None:
        """Register that this paper was found via a particular source."""
        if source not in self.sources:
            self.sources.append(source)
        if external_id:
            self.source_ids[source] = external_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Paper:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def get_text_for_analysis(self) -> str:
        """Return title + abstract combined for keyword/semantic analysis."""
        parts = []
        if self.title:
            parts.append(self.title)
        if self.abstract:
            parts.append(self.abstract)
        return " ".join(parts)
