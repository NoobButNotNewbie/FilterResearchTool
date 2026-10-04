"""Pipeline orchestrator — runs the full literature screening pipeline.

Pipeline stages:
1. Search (multi-source discovery)
2. Normalize (DOI + title normalization)
3. Deduplicate (DOI exact + fuzzy title)
4. Rule Filter (keyword inclusion/exclusion)
5. Semantic Filter (Sentence Transformer similarity)
6. Verification (cross-check metadata via APIs)
7. Human title/abstract screening
8. Human full-text screening
9. Manual coding, analysis, and export

Each stage is idempotent and can be re-run.
Papers are NEVER deleted — only status changes.
"""

from __future__ import annotations

import csv
import json
import platform
import time
import uuid
from pathlib import Path
from typing import Any

from requests.exceptions import RequestException

from filtertool.config import get_nested, load_config
from filtertool.models import Paper, PaperStatus
from filtertool.normalize import normalize_doi, normalize_title
from filtertool.provenance import append_search_log, config_hash, utc_timestamp
from filtertool.storage import PaperStore

_SEARCH_FAILURES = (
    RequestException, ValueError, KeyError, TypeError, IndexError, AttributeError,
)


class Pipeline:
    """Main pipeline orchestrator."""

    def __init__(self, config_path: str | Path, no_cache: bool = False):
        self.config = load_config(config_path)
        self.no_cache = no_cache
        self.run_id = uuid.uuid4().hex
        self.started_at = utc_timestamp()
        self.config_hash = config_hash(self.config)
        self.protocol_version = str(self.config.get("protocol_version", "unspecified"))
        self.run_errors: list[dict[str, str]] = []
        self.run_warnings: list[dict[str, str]] = []
        self.stages_completed: list[str] = []
        self.snowballing_log: list[dict[str, Any]] = []
        self.last_successful_search_run_id: str | None = None
        db_path = get_nested(self.config, "storage", "database_file", default="./data/papers.json")
        cache_dir = get_nested(self.config, "storage", "cache_dir", default="./data/cache")
        self.store = PaperStore(db_path=db_path, cache_dir=cache_dir)
        self.output_dir = Path(get_nested(self.config, "export", "output_dir", default="./output"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        prior_manifest = self.output_dir / "run_manifest.json"
        if prior_manifest.exists():
            try:
                previous = json.loads(prior_manifest.read_text(encoding="utf-8"))
                if previous.get("config_hash") == self.config_hash:
                    self.snowballing_log = previous.get("snowballing_by_seed", [])
                self.last_successful_search_run_id = previous.get("last_successful_search_run_id")
                if (
                    not self.last_successful_search_run_id
                    and previous.get("status") in {"complete", "partial"}
                    and {"search", "collect"}.intersection(previous.get("stages_completed", []))
                ):
                    self.last_successful_search_run_id = previous.get("run_id")
            except (OSError, ValueError):
                pass
        self.write_output_guide()

    def write_output_guide(self) -> Path:
        """Write a fresh description of output files for this run."""
        path = self.output_dir / "OUTPUT_GUIDE.txt"
        content = f"""FILTERTOOL - HƯỚNG DẪN CÁC FILE OUTPUT
Run ID: {self.run_id}
Thời điểm khởi tạo: {self.started_at}

File này được tạo lại mỗi khi chạy một lệnh FilterTool có sử dụng pipeline.
Một số file bên dưới chỉ xuất hiện khi chạy stage/lệnh tương ứng.

CÁC FILE VÀ CHỨC NĂNG

search_log.csv
  Nhật ký truy vấn theo nguồn, query, số kết quả, cache, run và lỗi API.
  Được bổ sung khi search, collect, pilot hoặc mở rộng citation.

raw_search_results.jsonl
  Metadata paper đã chuẩn hóa nhận từ các nguồn, trước dedup và filter.
  Mỗi dòng là một record kèm run ID, nguồn, query và thông tin cache.

screening_sheet.xlsx
  Workbook để người nghiên cứu review title/abstract và nhập quyết định,
  reason code, ghi chú và thông tin reviewer.

fulltext_screening_sheet.xlsx
  Workbook review full-text cho các paper được chuyển tiếp từ vòng title/abstract.

auto_suggestions.xlsx
  Danh sách candidate và taxonomy tự động để tham khảo; không phải quyết định
  include/exclude của con người.

classified_papers.xlsx
  Workbook coding cho paper đã được human include; tách gợi ý tự động và
  các cột coding thủ công.

all_papers.json
  Toàn bộ paper trong database dưới dạng JSON, bao gồm metadata và audit trail.

crosstab.xlsx
  Bảng chéo optimization technique với attack method và optimization objective.
  Các sparse cell chỉ là ứng viên cần validation, không tự động kết luận research gap.

prisma_counts.csv
  Flow counts PRISMA-style ở dạng bảng phẳng để lọc/tổng hợp.

prisma_counts.json
  Cùng các flow counts và thông tin provenance ở dạng JSON có cấu trúc.

pilot_recall.csv
  Kết quả đối chiếu các title kỳ vọng với từng search source trong lệnh pilot.

auto_manual_agreement.json
  So sánh gợi ý auto với coding thủ công của một coder; không phải inter-rater reliability.

run_manifest.json
  Trạng thái run, stage đã chạy, hash config, phiên bản, số lượng paper,
  cảnh báo và lỗi.

OUTPUT_GUIDE.txt
  Hướng dẫn này; được ghi mới cho mỗi lần khởi tạo pipeline.
"""
        path.write_text(content, encoding="utf-8")
        return path

    def _record_search(self, source: str, query: str, n_returned: int, n_new: int,
                       cache_used: bool, error: str = "", activity: str = "search") -> None:
        search_config = self.config.get("search", {})
        year_from = search_config.get("year_from")
        year_to = search_config.get("year_to")
        year_filter = f"{year_from or ''}-{year_to or ''}".strip("-")
        append_search_log(self.output_dir / "search_log.csv", {
            "timestamp": utc_timestamp(),
            "run_id": self.run_id,
            "activity": activity,
            "source": source,
            "query": query,
            "year_filter": year_filter,
            "n_returned": n_returned,
            "n_new": n_new,
            "cache_used": str(bool(cache_used)).lower(),
            "protocol_version": self.protocol_version,
            "config_hash": self.config_hash,
            "error": error,
        })

    def _paper_matches_year_filter(self, paper: Paper) -> bool:
        search_config = self.config.get("search", {})
        year_from = search_config.get("year_from")
        year_to = search_config.get("year_to")
        if year_from is None and year_to is None:
            return True
        if type(paper.year) is not int:
            return False
        return (
            (year_from is None or paper.year >= year_from)
            and (year_to is None or paper.year <= year_to)
        )

    def _append_raw_records(self, source: str, query: str, papers: list[Paper], cache_used: bool) -> None:
        path = self.output_dir / "raw_search_results.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            for paper in papers:
                stream.write(json.dumps({
                    "run_id": self.run_id,
                    "timestamp": utc_timestamp(),
                    "protocol_version": self.protocol_version,
                    "config_hash": self.config_hash,
                    "source": source,
                    "query": query,
                    "cache_used": cache_used,
                    "record": paper.to_dict(),
                }, ensure_ascii=False) + "\n")

    def run_prepare_review(self) -> None:
        """Process already collected local records and export human-review inputs."""
        self.run_normalize()
        self.run_dedup()
        self.run_rule_filter()
        self.run_semantic_filter()
        self.run_classification(include_candidates=True)
        self.run_screening_export("title_abstract")
        if self.last_successful_search_run_id:
            self.run_prisma()
        else:
            print("[PRISMA SKIPPED] No successful search run is recorded; counts were not generated.")
        self.run_export()

    def run_report(self) -> None:
        """Refresh automatic suggestions and reports from saved human decisions/codes."""
        self.run_classification()
        self.run_analysis()
        self.run_prisma()
        self.run_export()

    def write_run_manifest(self) -> Path:
        status_counts: dict[str, int] = {}
        for paper in self.store.get_all():
            status = paper.status.value if isinstance(paper.status, PaperStatus) else str(paper.status)
            status_counts[status] = status_counts.get(status, 0) + 1
        manifest = {
            "run_id": self.run_id,
            "status": "invalid" if self.run_errors else "partial" if self.run_warnings else "complete",
            "valid": not self.run_errors,
            "started_at": self.started_at,
            "finished_at": utc_timestamp(),
            "tool_version": "0.1.0",
            "python_version": platform.python_version(),
            "protocol_version": self.protocol_version,
            "config_hash": self.config_hash,
            "sources": self.config.get("search", {}).get("sources", []),
            "semantic_model": self.config.get("semantic_filter", {}).get("model_name"),
            "cache_bypassed": self.no_cache,
            "stages_completed": self.stages_completed,
            "last_successful_search_run_id": self.last_successful_search_run_id,
            "status_counts": status_counts,
            "errors": self.run_errors,
            "warnings": self.run_warnings,
            "snowballing_by_seed": self.snowballing_log,
        }
        path = self.output_dir / "run_manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def run_screening_export(self, stage: str = "title_abstract") -> None:
        from filtertool.screening import export_screening_sheet

        filename = "screening_sheet.xlsx" if stage == "title_abstract" else "fulltext_screening_sheet.xlsx"
        path = export_screening_sheet(self.store.get_all(), self.output_dir / filename, stage=stage)
        print(f"[SCREENING SHEET] {path} ({stage})")

    def run_prisma(self, run_id: str | None = None) -> None:
        from filtertool.prisma import select_search_events, write_prisma_counts

        selected_run_id = run_id or self.last_successful_search_run_id
        if not selected_run_id:
            print("[PRISMA SKIPPED] No successful search run is recorded; counts were not generated.")
            return

        search_log = self.output_dir / "search_log.csv"
        all_events = []
        if search_log.exists():
            with search_log.open("r", encoding="utf-8-sig", newline="") as stream:
                all_events = list(csv.DictReader(stream))
        partial_search = any(
            warning.get("stage") == "search"
            and warning.get("run_id") == selected_run_id
            for warning in self.run_warnings
        )
        search_events = select_search_events(
            all_events, selected_run_id, allow_partial=partial_search
        )

        if not self.snowballing_log:
            grouped: dict[tuple[str, str], dict[str, Any]] = {}
            candidate_statuses = {
                PaperStatus.RULE_INCLUDED.value, PaperStatus.REVIEW_NEEDED.value,
                PaperStatus.SEMANTIC_HIGH.value, PaperStatus.SEMANTIC_REVIEW.value,
                PaperStatus.SEMANTIC_LOW.value,
            }
            for paper in self.store.get_all():
                for discovery in paper.citation_discoveries:
                    key = (str(discovery.get("seed_id", "")), str(discovery.get("direction", "")))
                    entry = grouped.setdefault(key, {
                        "seed_id": key[0], "direction": key[1], "seed_title": "",
                        "found": 0, "new": 0, "already_known": 0, "new_after_filter": 0,
                        "error": "",
                    })
                    entry["found"] += 1
                    entry["new"] += int(bool(discovery.get("is_new")))
                    entry["already_known"] += int(not discovery.get("is_new"))
                    entry["new_after_filter"] += int(
                        bool(discovery.get("is_new")) and paper.status in candidate_statuses
                    )
            if grouped:
                self.snowballing_log = list(grouped.values())

        json_path, csv_path = write_prisma_counts(
            self.output_dir, self.store.get_all(), self.snowballing_log,
            search_events=search_events,
            search_provenance_available=search_log.exists(),
        )
        print(f"[PRISMA COUNTS] {json_path}; {csv_path} (search run {selected_run_id})")

    def _record_citation_errors(self, expansions: list[dict[str, Any]]) -> None:
        known = {
            (
                error.get("seed_id", ""), error.get("direction", ""),
                error.get("error", ""),
            )
            for error in self.run_errors
            if error.get("stage") == "citation_expansion"
        }
        for expansion in expansions:
            message = expansion.get("error")
            if not message:
                continue
            key = (
                str(expansion.get("seed_id", "")),
                str(expansion.get("direction", "")),
                str(message),
            )
            if key in known:
                continue
            self.run_errors.append({
                "stage": "citation_expansion",
                "source": "semantic_scholar",
                "error": key[2],
                "seed_id": key[0],
                "direction": key[1],
            })
            known.add(key)

    # ------------------------------------------------------------------
    # Stage 1: Search
    # ------------------------------------------------------------------

    def run_search(self) -> None:
        """Search all configured sources for papers."""
        from filtertool.search import get_adapter

        self.last_successful_search_run_id = None
        sources = get_nested(self.config, "search", "sources", default=[])
        queries = get_nested(self.config, "search", "queries", default=[])
        max_results = get_nested(self.config, "search", "max_results_per_query", default=100)

        total_new = 0
        successful_queries = 0
        for source_name in sources:
            try:
                adapter = get_adapter(source_name, self.config)
            except ValueError as e:
                print(f"[WARN] Skipping unknown source: {source_name} — {e}")
                self.run_errors.append({"stage": "search", "source": source_name, "error": str(e)})
                for query in queries:
                    self._record_search(source_name, query, 0, 0, False, str(e))
                continue

            for query in queries:
                cache_key = self.store.cache_key(
                    "search", f"{self.config_hash}:{source_name}:{query}"
                )
                cache_used = False
                error = ""
                try:
                    if not self.no_cache and self.store.has_cache(cache_key):
                        print(f"  [CACHE] {source_name} / '{query}' — using cached results")
                        cached = self.store.get_cache(cache_key)
                        if cached is None:
                            raise ValueError("cache entry could not be decoded")
                        papers = [Paper.from_dict(item) for item in cached]
                        cache_used = True
                    else:
                        print(f"  [SEARCH] {source_name} / '{query}'")
                        papers = adapter.search(query, max_results=max_results)
                except _SEARCH_FAILURES as e:
                    error = f"{type(e).__name__}: {e}"
                    print(f"  [WARN] {source_name} / '{query}': {error}")
                    self.run_warnings.append({
                        "stage": "search", "source": source_name,
                        "query": query, "error": error, "run_id": self.run_id,
                    })
                    papers = []
                else:
                    successful_queries += 1

                unfiltered_count = len(papers)
                papers = [paper for paper in papers if self._paper_matches_year_filter(paper)]
                rejected_by_year = unfiltered_count - len(papers)
                if rejected_by_year:
                    print(f"[YEAR FILTER] Excluded {rejected_by_year} records outside the configured year range")
                if not error:
                    self.store.set_cache(cache_key, [p.to_dict() for p in papers])

                if papers:
                    self._append_raw_records(source_name, query, papers, cache_used)

                # Add to store (skip if DOI already known)
                added = 0
                for paper in papers:
                    if paper.doi_normalized and self.store.get_by_doi(paper.doi_normalized):
                        # Merge source info into existing paper
                        existing = self.store.get_by_doi(paper.doi_normalized)
                        existing.add_source(source_name, paper.source_ids.get(source_name, ""))
                        self.store.update(existing)
                    else:
                        self.store.add(paper)
                        added += 1

                total_new += added
                print(f"    → {len(papers)} found, {added} new")
                self._record_search(source_name, query, len(papers), added, cache_used, error)

        self.store.save()
        if successful_queries and not self.run_errors:
            self.last_successful_search_run_id = self.run_id
        print(f"\n[SEARCH DONE] {total_new} new papers. Total: {self.store.count()}")

    def run_pilot(self, title_file: str | Path) -> Path:
        """Check whether known-relevant titles are discoverable from each source."""
        from rapidfuzz import fuzz

        from filtertool.search import get_adapter

        titles = [
            line.strip() for line in Path(title_file).read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not titles:
            raise ValueError("pilot title file contains no titles")

        threshold = self.config.get("pilot", {}).get("title_match_threshold", 90)
        rows = []
        for source in self.config.get("search", {}).get("sources", []):
            try:
                adapter = get_adapter(source, self.config)
            except ValueError as error:
                message = f"{type(error).__name__}: {error}"
                self.run_errors.append({"stage": "pilot", "source": source, "error": message})
                for title in titles:
                    rows.append({"expected_title": title, "source": source, "found": False, "error": message})
                    self._record_search(source, title, 0, 0, False, message, activity="pilot")
                continue

            for title in titles:
                error_text = ""
                try:
                    results = adapter.search(title, max_results=20)
                    best = max(
                        results,
                        key=lambda paper: fuzz.ratio(normalize_title(title), normalize_title(paper.title)),
                        default=None,
                    )
                    score = fuzz.ratio(normalize_title(title), normalize_title(best.title)) if best else 0
                    rows.append({
                        "expected_title": title,
                        "source": source,
                        "found": score >= threshold,
                        "matched_title": best.title if best else "",
                        "match_score": score,
                        "returned": len(results),
                        "error": "",
                    })
                except _SEARCH_FAILURES as error:
                    error_text = f"{type(error).__name__}: {error}"
                    rows.append({
                        "expected_title": title, "source": source,
                        "found": False, "matched_title": "", "match_score": 0,
                        "returned": 0, "error": error_text,
                    })
                    self.run_errors.append({
                        "stage": "pilot", "source": source,
                        "query": title, "error": error_text,
                    })
                self._record_search(
                    source, title, len(results) if not error_text else 0,
                    0, False, error_text, activity="pilot",
                )

        path = self.output_dir / "pilot_recall.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            fields = ["expected_title", "source", "found", "matched_title", "match_score", "returned", "error"]
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        if not self.run_errors:
            self.stages_completed.append("pilot")
        self.write_run_manifest()
        print(f"[PILOT] {path} ({len(titles)} expected titles, {len(rows)} source checks)")
        if self.run_errors:
            raise RuntimeError("pilot API errors recorded; inspect run_manifest.json and pilot_recall.csv")
        return path

    # ------------------------------------------------------------------
    # Stage 2: Normalize
    # ------------------------------------------------------------------

    def run_normalize(self) -> None:
        """Normalize DOI and title for all papers."""
        papers = self.store.get_all()
        for paper in papers:
            if not paper.doi_normalized and paper.doi:
                paper.doi_normalized = normalize_doi(paper.doi)
            if not paper.title_normalized and paper.title:
                paper.title_normalized = normalize_title(paper.title)
            self.store.update(paper)
        self.store.save()
        print(f"[NORMALIZE DONE] {len(papers)} papers normalized")

    # ------------------------------------------------------------------
    # Stage 3: Deduplicate
    # ------------------------------------------------------------------

    def run_dedup(self) -> None:
        """Deduplicate papers by DOI and fuzzy title."""
        from filtertool.dedup import deduplicate

        papers = self.store.get_all()
        papers = deduplicate(papers, self.config)
        for p in papers:
            self.store.update(p)
        self.store.save()

        counts = self.store.count_by_status()
        n_dup = counts.get(PaperStatus.DUPLICATE.value, 0)
        n_unique = self.store.count() - n_dup
        print(f"[DEDUP DONE] {n_unique} unique, {n_dup} duplicates")

    # ------------------------------------------------------------------
    # Stage 4: Rule Filter
    # ------------------------------------------------------------------

    def run_rule_filter(self) -> None:
        """Apply keyword-based inclusion/exclusion filter."""
        from filtertool.filter.rule_filter import apply_rule_filter

        papers = self.store.get_all()
        papers = apply_rule_filter(papers, self.config)
        for p in papers:
            self.store.update(p)
        self.store.save()

        counts = self.store.count_by_status()
        n_candidates = counts.get(PaperStatus.RULE_INCLUDED.value, 0)
        n_review = counts.get(PaperStatus.REVIEW_NEEDED.value, 0)
        print(f"[RULE FILTER DONE] {n_candidates} candidates, {n_review} need human review; no final exclusions")

    # ------------------------------------------------------------------
    # Stage 5: Semantic Filter
    # ------------------------------------------------------------------

    def run_semantic_filter(self) -> None:
        """Apply semantic similarity filter."""
        from filtertool.filter.semantic_filter import apply_semantic_filter

        papers = self.store.get_all()
        papers = apply_semantic_filter(papers, self.config)
        for p in papers:
            self.store.update(p)
        self.store.save()

        counts = self.store.count_by_status()
        n_high = counts.get(PaperStatus.SEMANTIC_HIGH.value, 0)
        n_review = counts.get(PaperStatus.SEMANTIC_REVIEW.value, 0)
        n_low = counts.get(PaperStatus.SEMANTIC_LOW.value, 0)
        print(f"[SEMANTIC FILTER DONE] HIGH={n_high}, REVIEW={n_review}, LOW={n_low}")

    # ------------------------------------------------------------------
    # Stage 6: Verification
    # ------------------------------------------------------------------

    def run_verification(self) -> None:
        """Cross-check paper metadata via external APIs."""
        from filtertool.verify import verify_papers

        papers = self.store.get_all()
        papers = verify_papers(papers, self.config)
        for p in papers:
            self.store.update(p)
        self.store.save()

        counts = self.store.count_by_status()
        n_ver = counts.get(PaperStatus.VERIFIED.value, 0)
        n_unv = counts.get(PaperStatus.UNVERIFIED.value, 0)
        n_err = counts.get(PaperStatus.VERIFY_ERROR.value, 0)
        print(f"[VERIFICATION DONE] verified={n_ver}, unverified={n_unv}, error={n_err}")

    # ------------------------------------------------------------------
    # Stage 7: Citation Expansion
    # ------------------------------------------------------------------

    def run_citation_expansion(self) -> None:
        """Expand references and cited-by for top papers."""
        from filtertool.citation import expand_citations

        papers = self.store.get_all()
        previous_expansion_count = len(self.snowballing_log)
        papers = expand_citations(papers, self.config, self.snowballing_log)
        self._record_citation_errors(self.snowballing_log[previous_expansion_count:])

        # Add new papers to store
        existing_ids = {paper.id for paper in self.store.get_all()}
        rejected_by_year = 0
        for p in papers:
            if p.id not in existing_ids and not self._paper_matches_year_filter(p):
                rejected_by_year += 1
                continue
            if not self.store.get(p.id):
                self.store.add(p)
            else:
                self.store.update(p)
        if rejected_by_year:
            print(f"[CITATION EXPANSION] Filtered out {rejected_by_year} records outside the configured year range")
        self.store.save()

        # Citation discoveries re-enter candidate screening, never final inclusion.
        self.run_normalize()
        self.run_dedup()
        self.run_rule_filter()
        self.run_semantic_filter()

        candidates = self.store.get_all()
        candidate_statuses = {
            PaperStatus.RULE_INCLUDED.value,
            PaperStatus.REVIEW_NEEDED.value,
            PaperStatus.SEMANTIC_HIGH.value,
            PaperStatus.SEMANTIC_REVIEW.value,
            PaperStatus.SEMANTIC_LOW.value,
        }
        for expansion in self.snowballing_log:
            expansion["new_after_filter"] = sum(
                any(
                    discovery.get("is_new")
                    and discovery.get("seed_id") == expansion.get("seed_id")
                    and discovery.get("direction") == expansion.get("direction")
                    for discovery in paper.citation_discoveries
                )
                and paper.status in candidate_statuses
                for paper in candidates
            )
        self.run_screening_export("title_abstract")
        self.run_prisma()

        print(f"[CITATION EXPANSION DONE] Total papers: {self.store.count()}")

    # ------------------------------------------------------------------
    # Stage 8: Classification
    # ------------------------------------------------------------------

    def run_classification(self, include_candidates: bool = False) -> None:
        """Classify papers using taxonomy."""
        from filtertool.classify.classifier import classify_papers

        papers = self.store.get_all()
        papers = classify_papers(papers, self.config, include_candidates=include_candidates)
        for p in papers:
            self.store.update(p)
        self.store.save()

        suggestion_statuses = {
            PaperStatus.RULE_INCLUDED.value, PaperStatus.REVIEW_NEEDED.value,
            PaperStatus.SEMANTIC_HIGH.value, PaperStatus.SEMANTIC_REVIEW.value,
            PaperStatus.SEMANTIC_LOW.value, PaperStatus.HUMAN_INCLUDED.value,
            PaperStatus.INCLUDED.value,
        }
        n_suggested = sum(
            p.status in suggestion_statuses
            and bool(p.attack_methods_auto or p.optimization_techniques_auto)
            for p in papers
        )
        print(f"[AUTO LABELS DONE] Suggestions updated for {n_suggested} papers; review statuses unchanged")

    # ------------------------------------------------------------------
    # Stage 9: Analysis
    # ------------------------------------------------------------------

    def run_analysis(self) -> dict:
        """Build cross-tab and identify candidate gaps."""
        from filtertool.analysis import build_crosstab, identify_gaps
        from filtertool.export import export_crosstab_to_excel

        papers = self.store.get_all()
        crosstab = build_crosstab(papers, self.config)
        gaps = identify_gaps(crosstab)

        # Export
        ct_path = export_crosstab_to_excel(
            crosstab, self.output_dir / "crosstab.xlsx", gaps=gaps
        )

        print(f"[ANALYSIS DONE] {len(gaps)} candidate sparse cells")
        print(f"  Analysis workbook: {ct_path}")

        return {"crosstab": crosstab, "gaps": gaps}

    # ------------------------------------------------------------------
    # Stage 10: Export
    # ------------------------------------------------------------------

    def run_export(self) -> None:
        """Export the full paper list as JSON and classified papers as Excel."""
        from filtertool.export import export_to_excel, export_to_json

        papers = self.store.get_all()
        columns = get_nested(self.config, "export", "excel_columns", default=None)

        # Editable workbook separates automatic suggestions from manual coding.
        codeable = [
            p for p in papers
            if p.status in {PaperStatus.HUMAN_INCLUDED.value, PaperStatus.INCLUDED.value}
        ]
        cls_path = export_to_excel(
            codeable, self.output_dir / "classified_papers.xlsx", columns=columns
        )
        print(f"  Coding workbook: {cls_path} ({len(codeable)} rows)")

        suggestion_statuses = {
            PaperStatus.RULE_INCLUDED.value, PaperStatus.REVIEW_NEEDED.value,
            PaperStatus.SEMANTIC_HIGH.value, PaperStatus.SEMANTIC_REVIEW.value,
            PaperStatus.SEMANTIC_LOW.value, PaperStatus.HUMAN_INCLUDED.value,
            PaperStatus.INCLUDED.value,
        }
        suggestions = [paper for paper in papers if paper.status in suggestion_statuses]
        auto_columns = [
            "id", "title", "authors", "year", "doi", "url", "abstract", "sources",
            "publication_type", "status", "semantic_score", "semantic_label", "keyword_hits",
            "attack_methods_auto", "attack_stages_auto", "optimization_techniques_auto",
            "optimization_objectives_auto",
        ]
        suggestion_path = export_to_excel(
            suggestions, self.output_dir / "auto_suggestions.xlsx", columns=auto_columns
        )
        print(f"  Auto suggestions: {suggestion_path} ({len(suggestions)} rows; not final decisions)")

        json_path = export_to_json(papers, self.output_dir / "all_papers.json")
        print(f"  All papers: {json_path} ({len(papers)} records)")

        print("[EXPORT DONE]")

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------

    def run_all(self, skip_stages: list[str] | None = None) -> None:
        """Run the complete pipeline.
        
        Args:
            skip_stages: List of stage names to skip (e.g. ["search", "verification"])
        """
        skip = set(skip_stages or [])

        stages = [
            ("search", self.run_search),
            ("normalize", self.run_normalize),
            ("dedup", self.run_dedup),
            ("rule_filter", self.run_rule_filter),
            ("semantic_filter", self.run_semantic_filter),
            ("classification", lambda: self.run_classification(include_candidates=True)),
            ("screening_export", self.run_screening_export),
            ("prisma", self.run_prisma),
            ("export", self.run_export),
        ]

        print("=" * 60)
        print("FilterTool Pipeline")
        print("=" * 60)
        print(f"Database: {self.store.db_path}")
        print(f"Output: {self.output_dir}")
        print(f"Papers in DB: {self.store.count()}")
        print("=" * 60)

        start = time.time()
        for name, func in stages:
            if name in skip:
                print(f"\n[SKIP] {name}")
                continue
            print(f"\n{'─' * 40}")
            print(f"Stage: {name}")
            print(f"{'─' * 40}")
            try:
                func()
                if self.run_errors:
                    raise RuntimeError("stage produced errors; inspect run_manifest.json and search_log.csv")
                self.stages_completed.append(name)
            except Exception as e:
                if not any(error.get("stage") == name for error in self.run_errors):
                    self.run_errors.append({
                        "stage": name,
                        "error": f"{type(e).__name__}: {e}",
                    })
                self.write_run_manifest()
                print(f"[ERROR] Stage {name} failed; run stopped and marked invalid: {e}")
                raise
        self.write_run_manifest()

        elapsed = time.time() - start
        print(f"\n{'=' * 60}")
        print(f"Pipeline ended in {elapsed:.1f}s")
        print(self.store.summary())
        print(f"Run manifest: {self.output_dir / 'run_manifest.json'}")
        if self.run_errors:
            raise RuntimeError("Run marked invalid; inspect run_manifest.json and search_log.csv")
        if self.run_warnings:
            print(
                f"Run completed partially with {len(self.run_warnings)} warning(s); "
                f"inspect run_manifest.json and search_log.csv."
            )
        print(f"{'=' * 60}")

    # ------------------------------------------------------------------
    # Single-stage runners (for resume / re-run)
    # ------------------------------------------------------------------

    def run_stage(self, stage_name: str) -> None:
        """Run a single named stage."""
        stage_map = {
            "collect": self.run_search,
            "search": self.run_search,
            "prepare_review": self.run_prepare_review,
            "report": self.run_report,
            "normalize": self.run_normalize,
            "dedup": self.run_dedup,
            "rule_filter": self.run_rule_filter,
            "semantic_filter": self.run_semantic_filter,
            "verification": self.run_verification,
            "citation_expansion": self.run_citation_expansion,
            "classification": self.run_classification,
            "analysis": self.run_analysis,
            "screening_export": self.run_screening_export,
            "prisma": self.run_prisma,
            "export": self.run_export,
        }
        func = stage_map.get(stage_name)
        if not func:
            raise ValueError(f"Unknown stage: {stage_name}. Valid: {list(stage_map.keys())}")
        try:
            func()
            if self.run_errors:
                raise RuntimeError("stage produced errors; inspect run_manifest.json and search_log.csv")
            self.stages_completed.append(stage_name)
        except Exception as e:
            if not any(error.get("stage") == stage_name for error in self.run_errors):
                self.run_errors.append({
                    "stage": stage_name,
                    "error": f"{type(e).__name__}: {e}",
                })
            raise
        finally:
            self.write_run_manifest()
        if self.run_errors:
            raise RuntimeError("Run marked invalid; inspect run_manifest.json and search_log.csv")
