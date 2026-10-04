"""PRISMA-style flow counts derived from stored records and audit decisions."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from filtertool.models import Paper, PaperStatus


def _status_value(status: str | PaperStatus) -> str:
    return status.value if isinstance(status, PaperStatus) else str(status)


def _latest_decision(paper: Paper, stage: str) -> dict[str, Any] | None:
    for decision in reversed(paper.screening_decisions):
        if decision.get("stage") == stage:
            return decision
    return None


def select_search_events(
    events: list[dict[str, Any]], run_id: str | None, allow_partial: bool = False
) -> list[dict[str, Any]]:
    """Select search events, optionally excluding failed requests from a partial run."""
    if not run_id:
        raise ValueError("No valid search run is recorded; run collect before exporting PRISMA counts")
    selected = [
        event for event in events
        if event.get("run_id") == run_id and event.get("activity") == "search"
    ]
    if not selected:
        raise ValueError(f"Search run {run_id!r} has no search-log events")
    if allow_partial:
        selected = [
            event for event in selected
            if not str(event.get("error") or "").strip()
        ]
        if not selected:
            raise ValueError(f"Search run {run_id!r} has no successful search events")
    elif any(str(event.get("error") or "").strip() for event in selected):
        raise ValueError(f"Search run {run_id!r} contains failed requests and is not valid for PRISMA")
    return selected


def build_prisma_counts(
    papers: list[Paper],
    snowballing: list[dict[str, Any]] | None = None,
    search_events: list[dict[str, Any]] | None = None,
    search_provenance_available: bool | None = None,
) -> dict[str, Any]:
    """Build transparent flow counts; automated scores are not human exclusions."""
    statuses = [_status_value(paper.status) for paper in papers]
    status_counts = {status: statuses.count(status) for status in sorted(set(statuses))}
    provenance_available = (
        bool(search_events) if search_provenance_available is None
        else search_provenance_available
    )
    search_events = search_events or []
    identified_by_source: dict[str, int] = {}
    for event in search_events:
        source = str(event.get("source") or "unknown")
        try:
            count = int(event.get("n_returned") or 0)
        except (TypeError, ValueError):
            count = 0
        identified_by_source[source] = identified_by_source.get(source, 0) + count
    identified_via_citation = sum(int(item.get("found") or 0) for item in (snowballing or []))
    source_hits = sum(identified_by_source.values())
    identified_records = source_hits + identified_via_citation

    title_decisions = {
        paper.id: _latest_decision(paper, "human_screening:title_abstract")
        for paper in papers
    }
    full_text_decisions = {
        paper.id: _latest_decision(paper, "human_screening:full_text")
        for paper in papers
    }
    title_decisions = {key: value for key, value in title_decisions.items() if value}
    full_text_decisions = {key: value for key, value in full_text_decisions.items() if value}

    duplicates_removed = status_counts.get(PaperStatus.DUPLICATE.value, 0)
    title_excluded = sum(d.get("decision") == "exclude" for d in title_decisions.values())
    title_included = sum(d.get("decision") == "include" for d in title_decisions.values())
    full_text_excluded = sum(d.get("decision") == "exclude" for d in full_text_decisions.values())
    full_text_not_retrieved = sum(d.get("decision") == "not_retrieved" for d in full_text_decisions.values())
    reports_assessed = sum(d.get("decision") != "not_retrieved" for d in full_text_decisions.values())
    full_text_included = sum(d.get("decision") == "include" for d in full_text_decisions.values())
    full_text_unsure = sum(d.get("decision") == "unsure" for d in full_text_decisions.values())
    title_unsure = sum(d.get("decision") == "unsure" for d in title_decisions.values())

    title_exclusion_reasons: dict[str, int] = {}
    full_text_exclusion_reasons: dict[str, int] = {}
    for decision in title_decisions.values():
        if decision.get("decision") == "exclude":
            code = decision.get("reason_code") or "UNSPECIFIED"
            title_exclusion_reasons[code] = title_exclusion_reasons.get(code, 0) + 1
    for decision in full_text_decisions.values():
        if decision and decision.get("decision") == "exclude":
            code = decision.get("reason_code") or "UNSPECIFIED"
            full_text_exclusion_reasons[code] = full_text_exclusion_reasons.get(code, 0) + 1

    awaiting_title = sum(
        _status_value(paper.status) != PaperStatus.DUPLICATE.value
        and paper.id not in title_decisions
        for paper in papers
    )
    awaiting_full_text = sum(
        _status_value(paper.status) == PaperStatus.HUMAN_INCLUDED.value
        and paper.id not in full_text_decisions
        for paper in papers
    )
    title_awaiting_resolution = awaiting_title + title_unsure
    full_text_awaiting_resolution = awaiting_full_text + full_text_unsure
    included = status_counts.get(PaperStatus.INCLUDED.value, 0)
    records_after_deduplication = max(0, len(papers) - duplicates_removed)
    search_hits_not_added = sum(
        max(0, int(event.get("n_returned") or 0) - int(event.get("n_new") or 0))
        for event in search_events
    )

    checks = {
        "search_provenance_available": provenance_available,
        "dedup_duplicates_do_not_exceed_database_records": duplicates_removed <= len(papers),
        "title_decisions_reconcile": title_excluded + title_included + title_unsure == len(title_decisions),
        "full_text_decisions_reconcile": reports_assessed + full_text_not_retrieved == len(full_text_decisions),
        "final_inclusions_match_full_text_decisions": included == full_text_included,
        "no_unresolved_unsure_decisions": title_unsure == 0 and full_text_unsure == 0,
        "reason_counts_match_exclusions": sum(title_exclusion_reasons.values()) == title_excluded
        and sum(full_text_exclusion_reasons.values()) == full_text_excluded,
    }

    return {
        "source_records_returned": source_hits,
        "search_provenance_available": provenance_available,
        "identified_records": identified_records,
        "identified_by_source": identified_by_source,
        "identified_via_citation_expansion": identified_via_citation,
        "duplicates_removed": duplicates_removed,
        "search_hits_not_added_or_merged": search_hits_not_added,
        "unique_records_in_database": len(papers) - duplicates_removed,
        "records_after_deduplication": records_after_deduplication,
        "title_abstract_screened": len(title_decisions),
        "title_abstract_excluded": title_excluded,
        "title_abstract_included_for_full_text": title_included,
        "title_abstract_unsure": sum(d.get("decision") == "unsure" for d in title_decisions.values()),
        "title_abstract_awaiting_resolution": title_awaiting_resolution,
        "awaiting_title_abstract_screening": awaiting_title,
        "full_text_not_retrieved": full_text_not_retrieved,
        "reports_assessed_full_text": reports_assessed,
        "full_text_included": full_text_included,
        "full_text_unsure": full_text_unsure,
        "full_text_awaiting_resolution": full_text_awaiting_resolution,
        "full_text_excluded": full_text_excluded,
        "studies_included": included,
        "awaiting_full_text_screening": awaiting_full_text,
        "status_counts": status_counts,
        "title_abstract_exclusions_by_reason": title_exclusion_reasons,
        "full_text_exclusions_by_reason": full_text_exclusion_reasons,
        "snowballing_by_seed": snowballing or [],
        "consistency_checks": checks,
        "counts_are_consistent": all(checks.values()),
    }


def write_prisma_counts(
    output_dir: str | Path,
    papers: list[Paper],
    snowballing: list[dict[str, Any]] | None = None,
    search_events: list[dict[str, Any]] | None = None,
    search_provenance_available: bool | None = None,
) -> tuple[Path, Path]:
    """Write machine-readable JSON and flat CSV PRISMA summaries."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    search_events = list(search_events or [])
    if search_provenance_available is None:
        search_provenance_available = bool(search_events)
    counts = build_prisma_counts(
        papers, snowballing, search_events, search_provenance_available
    )
    json_path = output_dir / "prisma_counts.json"
    csv_path = output_dir / "prisma_counts.csv"
    json_path.write_text(json.dumps(counts, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    for source, count in counts["identified_by_source"].items():
        rows.append({"group": "identified_by_source", "name": source, "count": count})
    for key in (
        "source_records_returned", "identified_records", "identified_via_citation_expansion",
        "duplicates_removed", "search_hits_not_added_or_merged", "unique_records_in_database",
        "records_after_deduplication", "title_abstract_screened", "title_abstract_excluded",
        "title_abstract_included_for_full_text", "title_abstract_unsure",
        "title_abstract_awaiting_resolution", "awaiting_title_abstract_screening", "full_text_not_retrieved",
        "reports_assessed_full_text", "full_text_included", "full_text_unsure",
        "full_text_awaiting_resolution", "full_text_excluded", "studies_included",
        "awaiting_full_text_screening",
    ):
        rows.append({"group": "flow", "name": key, "count": counts[key]})
    for group, reasons in (
        ("title_abstract_exclusion_reason", counts["title_abstract_exclusions_by_reason"]),
        ("full_text_exclusion_reason", counts["full_text_exclusions_by_reason"]),
    ):
        rows.extend({"group": group, "name": code, "count": count} for code, count in reasons.items())
    for item in counts["snowballing_by_seed"]:
        rows.append({
            "group": "snowballing",
            "name": f"{item.get('seed_id', '')}:{item.get('direction', '')}",
            "count": item.get("new", 0),
            "details": json.dumps(item, ensure_ascii=False),
        })

    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["group", "name", "count", "details"])
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path
