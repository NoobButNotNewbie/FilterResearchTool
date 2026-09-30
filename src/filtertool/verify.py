import time
import logging
from typing import Dict, Any, List
import requests
from rapidfuzz import fuzz

from filtertool.models import Paper, PaperStatus, Source
from filtertool.normalize import normalize_doi, normalize_title, normalize_author

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
                    data = _check_semanticscholar(paper, retry_attempts, retry_delay_seconds, rate_limit_delay_seconds, timeout_seconds)
                elif source == "openalex":
                    data = _check_openalex(paper, retry_attempts, retry_delay_seconds, rate_limit_delay_seconds, timeout_seconds)
                elif source == "crossref":
                    data = _check_crossref(paper, retry_attempts, retry_delay_seconds, rate_limit_delay_seconds, timeout_seconds)
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
                    
            except Exception as e:
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

def _make_request(url: str, retry_attempts: int, retry_delay: float, rate_limit_delay: float, timeout: int) -> dict | None:
    for attempt in range(retry_attempts):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 404:
                return None
            elif resp.status_code == 429:
                time.sleep(rate_limit_delay)
                continue
            else:
                resp.raise_for_status()
        except requests.RequestException as e:
            if attempt == retry_attempts - 1:
                raise e
            time.sleep(retry_delay)
    return None

def _check_semanticscholar(paper: Paper, retries: int, delay: float, rl_delay: float, timeout: int) -> dict | None:
    if not paper.doi:
        return None
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{paper.doi}?fields=title,authors,year,externalIds"
    data = _make_request(url, retries, delay, rl_delay, timeout)
    if data:
        return {
            "title": data.get("title"),
            "year": data.get("year"),
            "authors": [a.get("name") for a in data.get("authors", [])],
            "doi": data.get("externalIds", {}).get("DOI")
        }
    return None

def _check_openalex(paper: Paper, retries: int, delay: float, rl_delay: float, timeout: int) -> dict | None:
    if not paper.doi:
        return None
    url = f"https://api.openalex.org/works/doi:{paper.doi}"
    data = _make_request(url, retries, delay, rl_delay, timeout)
    if data:
        return {
            "title": data.get("title"),
            "year": data.get("publication_year"),
            "authors": [a.get("author", {}).get("display_name") for a in data.get("authorships", [])],
            "doi": data.get("doi", "").replace("https://doi.org/", "") if data.get("doi") else ""
        }
    return None

def _check_crossref(paper: Paper, retries: int, delay: float, rl_delay: float, timeout: int) -> dict | None:
    if not paper.doi:
        return None
    url = f"https://api.crossref.org/works/{paper.doi}"
    data = _make_request(url, retries, delay, rl_delay, timeout)
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
