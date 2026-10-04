import logging

import requests
from requests.exceptions import RequestException

from filtertool.models import Paper, PaperStatus, Source
from filtertool.normalize import normalize_doi, normalize_title
from filtertool.rate_limit import get_rate_limiter, parse_retry_after
from filtertool.search.base import get_semantic_scholar_api_key

logger = logging.getLogger(__name__)

def expand_citations(
    papers: list[Paper], config: dict,
    expansion_log: list[dict] | None = None,
) -> list[Paper]:
    citation_config = config.get("citation_expansion", {})
    if not citation_config.get("enabled", True):
        return papers
    expand_references = citation_config.get("expand_references", True)
    expand_cited_by = citation_config.get("expand_cited_by", True)
    max_seed_papers = citation_config.get("max_seed_papers", 10)
    max_per_seed = citation_config.get("max_per_seed", 20)
    
    api_config = config.get("search", {}).get("api", {})
    timeout = api_config.get("timeout_seconds", 30)
    retries = api_config.get("retry_attempts", 3)
    retry_delay = api_config.get("retry_delay_seconds", 5)
    api_key = get_semantic_scholar_api_key(config)
    headers = {"x-api-key": api_key} if api_key else None

    eligible_papers = [
        p for p in papers
        if p.status in (PaperStatus.HUMAN_INCLUDED, PaperStatus.INCLUDED)
        and not p.citation_expanded
    ]
    seed_papers = sorted(eligible_papers, key=lambda p: p.semantic_score or 0.0, reverse=True)[:max_seed_papers]
    existing_by_doi = {normalize_doi(p.doi): p for p in papers if p.doi}
    existing_by_title = {normalize_title(p.title): p for p in papers if p.title}
    new_papers: dict[str, Paper] = {}

    for seed in seed_papers:
        if not seed.doi:
            continue
        seed_id = f"DOI:{seed.doi}"
        directions = []
        if expand_references:
            directions.append(("references", "citedPaper"))
        if expand_cited_by:
            directions.append(("cited_by", "citingPaper"))

        for direction, paper_key in directions:
            endpoint = "references" if direction == "references" else "citations"
            url = f"https://api.semanticscholar.org/graph/v1/paper/{seed_id}/{endpoint}?fields=title,authors,year,externalIds,abstract,venue,url&limit={max_per_seed}"
            found_papers, found_count, error = _fetch_papers(
                url, paper_key, timeout, retries, retry_delay, config, headers
            )
            new_count = 0
            known_count = 0
            for candidate in found_papers:
                doi_key = normalize_doi(candidate.doi)
                title_key = normalize_title(candidate.title)
                existing = existing_by_doi.get(doi_key) if doi_key else None
                existing = existing or (existing_by_title.get(title_key) if title_key else None)
                candidate_key = doi_key or title_key
                event = {
                    "seed_id": seed.id,
                    "seed_title": seed.title,
                    "direction": direction,
                    "is_new": existing is None and candidate_key not in new_papers,
                }
                if existing:
                    existing.add_source(Source.CITATION.value)
                    existing.citation_discoveries.append(event)
                    known_count += 1
                elif candidate_key in new_papers:
                    new_papers[candidate_key].citation_discoveries.append(event)
                    known_count += 1
                else:
                    candidate.add_source(Source.CITATION.value)
                    candidate.citation_discoveries.append(event)
                    new_papers[candidate_key] = candidate
                    if doi_key:
                        existing_by_doi[doi_key] = candidate
                    if title_key:
                        existing_by_title[title_key] = candidate
                    new_count += 1

            record = {
                "seed_id": seed.id,
                "seed_title": seed.title,
                "direction": direction,
                "found": found_count,
                "new": new_count,
                "already_known": known_count,
                "new_after_filter": None,
                "error": error,
            }
            if expansion_log is not None:
                expansion_log.append(record)
            if error:
                logger.warning("Citation expansion failed for %s (%s): %s", seed.id, direction, error)

        seed.citation_expanded = True

    return papers + list(new_papers.values())


def _fetch_papers(url: str, paper_key: str, timeout: int, retries: int,
                  retry_delay: float, config: dict,
                  headers: dict | None) -> tuple[list[Paper], int, str]:
    limiter = get_rate_limiter(url, config)
    for attempt in range(retries):
        try:
            limiter.wait()
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            limiter.record_success()
            data = response.json()
            items = data.get("data") or []
            papers = []
            for item in items:
                paper_data = item.get(paper_key) or {}
                title = paper_data.get("title")
                if not title:
                    continue
                external_ids = paper_data.get("externalIds") or {}
                doi = external_ids.get("DOI")
                authors = [
                    author.get("name") for author in (paper_data.get("authors") or [])
                    if isinstance(author, dict) and author.get("name")
                ]
                paper = Paper(
                    title=title,
                    authors=authors,
                    year=paper_data.get("year"),
                    doi=doi,
                    abstract=paper_data.get("abstract") or "",
                    venue=paper_data.get("venue") or "",
                    url=paper_data.get("url") or "",
                )
                paper.doi_normalized = normalize_doi(doi)
                paper.title_normalized = normalize_title(title)
                paper.add_source(Source.SEMANTIC_SCHOLAR.value, paper_data.get("paperId", ""))
                paper.add_screening_decision(
                    "citation_expansion", "candidate", "Discovered via citation expansion"
                )
                papers.append(paper)
            return papers, len(items), ""
        except (
            RequestException, ValueError, KeyError, TypeError, IndexError, AttributeError,
        ) as error:
            if attempt + 1 < retries:
                response = getattr(error, "response", None)
                retry_after = parse_retry_after(
                    response.headers.get("Retry-After") if response is not None else None
                )
                limiter.record_throttle(retry_delay * (2 ** attempt), retry_after)
            else:
                return [], 0, f"{type(error).__name__}: {error}"
    return [], 0, "request failed"
