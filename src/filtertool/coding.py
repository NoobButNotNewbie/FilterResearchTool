"""Import manual taxonomy codes and measure agreement with suggestions."""

from __future__ import annotations

from collections import Counter
from typing import Any

from filtertool.models import Paper, PaperStatus

_DIMENSIONS = {
    "attack_methods": "attack_methods",
    "attack_stages": "attack_stages",
    "optimization_techniques": "optimization_techniques",
    "optimization_objectives": "optimization_objectives",
}


def _status_value(status: str | PaperStatus) -> str:
    return status.value if isinstance(status, PaperStatus) else str(status)


def _codes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (int, float)):
        value = str(value)
    return list(dict.fromkeys(
        part.strip() for part in str(value).replace("\n", ";").replace("|", ";").split(";")
        if part.strip()
    ))


def import_manual_coding(papers: list[Paper], rows: list[dict[str, Any]], config: dict) -> int:
    """Validate a coding workbook then update only explicitly supplied fields."""
    by_id = {paper.id: paper for paper in papers}
    taxonomy = config.get("taxonomy", {})
    allowed = {
        dimension: set(taxonomy.get(dimension, {}))
        for dimension in _DIMENSIONS.values()
    }
    allowed["optimization_techniques"].add("none_detected")
    allowed["optimization_objectives"].add("other_improvement")
    prepared = []
    seen = set()

    for row_number, row in enumerate(rows, 2):
        paper_id = str(row.get("id") or "").strip()
        if not paper_id:
            continue
        if paper_id not in by_id:
            raise ValueError(f"row {row_number}: unknown paper id {paper_id}")
        if paper_id in seen:
            raise ValueError(f"row {row_number}: duplicate paper id {paper_id}")
        seen.add(paper_id)
        paper = by_id[paper_id]
        if _status_value(paper.status) not in {
            PaperStatus.HUMAN_INCLUDED.value,
            PaperStatus.INCLUDED.value,
        }:
            raise ValueError(f"row {row_number}: paper is not human-included")

        changes = {}
        for dimension in _DIMENSIONS:
            column = f"{dimension}_manual"
            if column not in row:
                continue
            codes = _codes(row[column])
            invalid = sorted(set(codes) - allowed[dimension])
            if invalid:
                raise ValueError(f"row {row_number}: unknown {dimension} code(s): {', '.join(invalid)}")
            changes[column] = codes

        for field in ("baseline_compared", "measured"):
            if field in row:
                value = str(row[field] or "").strip()
                if field == "measured" and value and value.lower() not in {"yes", "no"}:
                    raise ValueError(f"row {row_number}: measured must be yes or no")
                changes[field] = value or None
        if changes:
            prepared.append((paper, changes))

    for paper, changes in prepared:
        for field, value in changes.items():
            setattr(paper, field, value)
        paper.add_screening_decision(
            stage="manual_coding",
            decision="updated",
            reason="Manual code fields imported",
        )
    return len(prepared)


def auto_manual_agreement(papers: list[Paper]) -> dict[str, dict[str, Any]]:
    """Compare automatic suggestions with one human coder; this is not inter-rater reliability."""
    results = {}
    for dimension in _DIMENSIONS:
        pairs = []
        for paper in papers:
            if _status_value(paper.status) not in {
                PaperStatus.HUMAN_INCLUDED.value,
                PaperStatus.INCLUDED.value,
            }:
                continue
            manual = getattr(paper, f"{dimension}_manual", []) or []
            automatic = getattr(paper, f"{dimension}_auto", []) or []
            if manual and automatic:
                pairs.append((frozenset(manual), frozenset(automatic)))

        n = len(pairs)
        if not n:
            results[dimension] = {
                "n": 0, "exact_agreement_auto_manual": None,
                "cohen_kappa_auto_manual": None,
            }
            continue
        observed = sum(manual == automatic for manual, automatic in pairs) / n
        manual_counts = Counter(manual for manual, _ in pairs)
        auto_counts = Counter(automatic for _, automatic in pairs)
        categories = set(manual_counts) | set(auto_counts)
        expected = sum(manual_counts[item] * auto_counts[item] for item in categories) / (n * n)
        kappa = (observed - expected) / (1 - expected) if expected < 1 else (1.0 if observed == 1 else None)
        results[dimension] = {
            "n": n,
            "exact_agreement_auto_manual": observed,
            "cohen_kappa_auto_manual": kappa,
        }
    return results
