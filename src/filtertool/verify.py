import logging

import requests
from rapidfuzz import fuzz
from requests.exceptions import RequestException

from filtertool.models import Paper, PaperStatus
from filtertool.normalize import normalize_author, normalize_doi, normalize_title
from filtertool.rate_limit import get_rate_limiter, parse_retry_after
from filtertool.search.base import get_semantic_scholar_api_key

logger = logging.getLogger(__name__)

def verify_papers(papers: list[Paper], config: dict) -> list[Paper]:
    """Verify papers against external APIs."""
    verify_config = config.get("verification", {})
    sources = verify_config.get("sources", ["semanticscholar", "openalex", "crossref"])
    error_is_not_exclusion = verify_config.get("error_is_not_exclusion", True)
    
    api_config = config.get("search", {}).get("api", {})
    retry_attempts = api_config.get("retry_attempts", 3)
    retry_delay_seconds = api_config.get("retry_delay_seconds", 2.0)
    rate_limit_delay_seconds = api_config.get("rate_limit_delay_seconds", 5.0)
    timeout_seconds = api_config.get("timeout_seconds", 30)

    for paper in papers:
        if paper.status not in (PaperStatus.SEMANTIC_HIGH, PaperStatus.SEMANTIC_REVIEW):
            continue
            
        if not hasattr(paper, 'verification_results'):
            paper.verification_results = {}
            
        all_errors = True
        verified = False
        
        for source in sources:
            result = {"verified": False, "matched_fields": [], "mismatched_fields": [], "error": None}
            try:
                if source in {"semanticscholar", "semantic_scholar"}:
                    data = _check_semanticscholar(paper, retry_attempts, retry_delay_seconds, rate_limit_delay_seconds, timeout_seconds, config)
                elif source == "openalex":
                    data = _check_openalex(paper, retry_attempts, retry_delay_seconds, rate_limit_delay_seconds, timeout_seconds, config)
                elif source == "crossref":
                    data = _check_crossref(paper, retry_attempts, retry_delay_seconds, rate_limit_delay_seconds, timeout_seconds, config)
                else:
                    continue
                
                if data:
                    all_errors = False
                    match_result = _compare_fields(paper, data)
                    result.update(match_result)
                    if result["verified"]:
                        verified = True
                else:
                    all_errors = False
                    result["error"] = "Not found"

            except (RequestException, ValueError, KeyError, TypeError, IndexError, AttributeError) as e:
                result["error"] = str(e)

            paper.verification_results[source] = result

        if verified:
            paper.status = PaperStatus.VERIFIED
            paper.add_screening_decision(
                stage="verification",
                decision="included",
                reason="Verified by at least one source"
            )
        elif all_errors and error_is_not_exclusion:
            paper.status = PaperStatus.VERIFY_ERROR
            paper.add_screening_decision(
                stage="verification",
                decision="undecided",
                reason="All sources returned errors, keeping due to error_is_not_exclusion"
            )
        else:
            paper.status = PaperStatus.UNVERIFIED
            paper.add_screening_decision(
                stage="verification",
                decision="excluded",
                reason="Could not be verified by any source"
            )
            
    return papers

def _compare_fields(paper: Paper, data: dict) -> dict:
    matched = []
    mismatched = []
    
    # Title
    if paper.title and data.get("title"):
        norm_paper_title = normalize_title(paper.title)
        norm_data_title = normalize_title(data["title"])
        if fuzz.ratio(norm_paper_title, norm_data_title) >= 80:
            matched.append("title")
        else:
            mismatched.append("title")
            
    # Year
    if paper.year and data.get("year"):
        if int(paper.year) == int(data["year"]):
            matched.append("year")
        else:
            mismatched.append("year")
            
    # Authors
    if paper.authors and data.get("authors") and len(paper.authors) > 0 and len(data["authors"]) > 0:
        paper_first_author = normalize_author(paper.authors[0])
        data_first_author = normalize_author(data["authors"][0])
        if fuzz.partial_ratio(paper_first_author, data_first_author) >= 70:
            matched.append("authors")
        else:
            mismatched.append("authors")
            
    # DOI
    if paper.doi and data.get("doi"):
        if normalize_doi(paper.doi) == normalize_doi(data["doi"]):
            matched.append("doi")
        else:
            mismatched.append("doi")
            
    verified = len(matched) > 0 and len(mismatched) == 0
    if "title" in matched or "doi" in matched:
        verified = True
        
    return {"verified": verified, "matched_fields": matched, "mismatched_fields": mismatched}

def _make_request(
    url: str, retry_attempts: int, retry_delay: float, rate_limit_delay: float,
    timeout: int, headers: dict | None = None, config: dict | None = None,
) -> dict | None:
    limiter_config = config or {
        "search": {"api": {"rate_limit_delay_seconds": rate_limit_delay}}
    }
    limiter = get_rate_limiter(url, limiter_config)
    retryable_statuses = {408, 429, 500, 502, 503, 504}
    attempts = max(1, retry_attempts)

    for attempt in range(attempts):
        limiter.wait()
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code == 200:
                limiter.record_success()
                return response.json()
            if response.status_code == 404:
                limiter.record_success()
                return None
            if response.status_code not in retryable_statuses:
                response.raise_for_status()
            if attempt == attempts - 1:
                response.raise_for_status()
            limiter.record_throttle(
                retry_delay * (2 ** attempt),
                parse_retry_after(response.headers.get("Retry-After")),
            )
        except requests.RequestException as error:
            if attempt == attempts - 1:
                raise
            response = getattr(error, "response", None)
            status = response.status_code if response is not None else None
            if status is not None and status not in retryable_statuses:
                raise
            retry_after = parse_retry_after(
                response.headers.get("Retry-After") if response is not None else None
            )
            limiter.record_throttle(retry_delay * (2 ** attempt), retry_after)
    return None

def _check_semanticscholar(
    paper: Paper, retries: int, delay: float, rl_delay: float, timeout: int,
    config: dict | None = None,
) -> dict | None:
    if not paper.doi:
        return None
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{paper.doi}?fields=title,authors,year,externalIds"
    api_key = get_semantic_scholar_api_key(config or {})
    headers = {"x-api-key": api_key} if api_key else None
    data = _make_request(url, retries, delay, rl_delay, timeout, headers, config)
    if data:
        return {
            "title": data.get("title"),
            "year": data.get("year"),
            "authors": [a.get("name") for a in data.get("authors", [])],
            "doi": data.get("externalIds", {}).get("DOI"),
        }
    return None

def _check_openalex(
    paper: Paper, retries: int, delay: float, rl_delay: float, timeout: int,
    config: dict | None = None,
) -> dict | None:
    if not paper.doi:
        return None
    url = f"https://api.openalex.org/works/doi:{paper.doi}"
    data = _make_request(url, retries, delay, rl_delay, timeout, config=config)
    if data:
        return {
            "title": data.get("title"),
            "year": data.get("publication_year"),
            "authors": [a.get("author", {}).get("display_name") for a in data.get("authorships", [])],
            "doi": data.get("doi", "").replace("https://doi.org/", "") if data.get("doi") else ""
        }
    return None

def _check_crossref(
    paper: Paper, retries: int, delay: float, rl_delay: float, timeout: int,
    config: dict | None = None,
) -> dict | None:
    if not paper.doi:
        return None
    url = f"https://api.crossref.org/works/{paper.doi}"
    data = _make_request(url, retries, delay, rl_delay, timeout, config=config)
    if data and "message" in data:
        msg = data["message"]
        title = msg.get("title", [""])[0] if msg.get("title") else ""
        year = None
        if "published-print" in msg:
            year = msg["published-print"]["date-parts"][0][0]
        elif "published-online" in msg:
            year = msg["published-online"]["date-parts"][0][0]
        
        authors = []
        for a in msg.get("author", []):
            if "given" in a and "family" in a:
                authors.append(f"{a['given']} {a['family']}")
            elif "family" in a:
                authors.append(a["family"])
                
        return {
            "title": title,
            "year": year,
            "authors": authors,
            "doi": msg.get("DOI")
        }
    return None
