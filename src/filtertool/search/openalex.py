from requests.exceptions import RequestException

from filtertool.models import Paper, Source
from filtertool.normalize import normalize_author_list, normalize_doi, normalize_title

from .base import BaseSearchAdapter, get_contact_email


class OpenAlexAdapter(BaseSearchAdapter):
    BASE_URL = "https://api.openalex.org/works"
    
    def search(self, query: str, max_results: int = 100) -> list[Paper]:
        papers = []
        cursor = "*"
        per_page = min(100, max_results)
        
        email = get_contact_email(self.config)
        
        filters = []
        year_from = self.config.get("search", {}).get("year_from")
        year_to = self.config.get("search", {}).get("year_to")
        if year_from:
            filters.append(f"publication_year:>{year_from-1}")
        if year_to:
            filters.append(f"publication_year:<{year_to+1}")
            
        filter_str = ",".join(filters) if filters else None
        
        while len(papers) < max_results:
            params = {
                "search": query,
                "per-page": per_page,
                "cursor": cursor
            }
            if email:
                params["mailto"] = email
            if filter_str:
                params["filter"] = filter_str
                
            data = self._make_request(self.BASE_URL, params=params)
            if not data or "results" not in data or not data["results"]:
                break
                
            for item in data["results"]:
                if len(papers) >= max_results:
                    break
                papers.append(self._parse_paper(item))
                
            cursor = data.get("meta", {}).get("next_cursor")
            if not cursor:
                break
                
        return papers
        
    def _reconstruct_abstract(self, inverted_index: dict) -> str:
        if not inverted_index:
            return ""
        max_idx = 0
        for indices in inverted_index.values():
            max_idx = max(max_idx, max(indices))
            
        words = [""] * (max_idx + 1)
        for word, indices in inverted_index.items():
            for idx in indices:
                words[idx] = word
                
        return " ".join(words).strip()
        
    def _parse_paper(self, item: dict) -> Paper:
        doi = item.get("doi")
        if doi and doi.startswith("https://doi.org/"):
            doi = doi.replace("https://doi.org/", "")
            
        authors = [a.get("author", {}).get("display_name", "") for a in item.get("authorships", [])]
        year = item.get("publication_year")
        abstract = self._reconstruct_abstract(item.get("abstract_inverted_index", {}))
        
        venue = ""
        host_venue = item.get("primary_location", {})
        if host_venue and host_venue.get("source"):
            venue = host_venue["source"].get("display_name", "")
            
        p = Paper(
            id=item.get("id", ""),
            title=item.get("title", ""),
            title_normalized=normalize_title(item.get("title", "")),
            authors=normalize_author_list(authors),
            year=year,
            doi=doi,
            doi_normalized=normalize_doi(doi),
            abstract=abstract,
            url=item.get("id", ""),
            venue=venue,
            publication_type=item.get("type"),
        )
        p.add_source(Source.OPENALEX, item.get("id", ""))
        return p
        
    def get_paper_by_doi(self, doi: str) -> Paper | None:
        url = f"{self.BASE_URL}/https://doi.org/{doi}"
        params = {}
        email = get_contact_email(self.config)
        if email:
            params["mailto"] = email
            
        try:
            data = self._make_request(url, params=params)
            if data and data.get("id"):
                return self._parse_paper(data)
        except (RequestException, ValueError, KeyError, TypeError, IndexError, AttributeError):
            return None
        return None
