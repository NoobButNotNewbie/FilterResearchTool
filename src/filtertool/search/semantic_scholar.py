import logging

from requests.exceptions import RequestException

from filtertool.models import Paper, Source
from filtertool.normalize import normalize_author_list, normalize_doi, normalize_title

from .base import BaseSearchAdapter, get_semantic_scholar_api_key

logger = logging.getLogger(__name__)


class SemanticScholarAdapter(BaseSearchAdapter):
    BASE_URL = "https://api.semanticscholar.org/graph/v1"
    
    def search(self, query: str, max_results: int = 100) -> list[Paper]:
        url = f"{self.BASE_URL}/paper/search"
        fields = "title,authors,year,abstract,externalIds,url,venue,publicationTypes"
        papers = []
        offset = 0
        limit = min(100, max_results)
        
        headers = {}
        api_key = get_semantic_scholar_api_key(self.config)
        if api_key:
            headers["x-api-key"] = api_key
            
        year_from = self.config.get("search", {}).get("year_from")
        year_to = self.config.get("search", {}).get("year_to")
        year_param = ""
        if year_from and year_to:
            year_param = f"{year_from}-{year_to}"
        elif year_from:
            year_param = f"{year_from}-"
        elif year_to:
            year_param = f"-{year_to}"
            
        while len(papers) < max_results:
            params = {
                "query": query,
                "fields": fields,
                "offset": offset,
                "limit": limit
            }
            if year_param:
                params["year"] = year_param
                
            data = self._make_request(url, params=params, headers=headers)
            if not data or "data" not in data:
                break
                
            for item in data["data"]:
                if len(papers) >= max_results:
                    break
                papers.append(self._parse_paper(item))
                
            offset += limit
            if offset >= data.get("total", 0):
                break
                
        return papers
        
    def _parse_paper(self, item: dict) -> Paper:
        doi = item.get("externalIds", {}).get("DOI")
        authors = [a.get("name", "") for a in item.get("authors", [])]
        
        p = Paper(
            id=item.get("paperId", ""),
            title=item.get("title", ""),
            title_normalized=normalize_title(item.get("title", "")),
            authors=normalize_author_list(authors),
            year=item.get("year"),
            doi=doi,
            doi_normalized=normalize_doi(doi),
            abstract=item.get("abstract") or "",
            url=item.get("url") or "",
            venue=item.get("venue") or "",
            publication_type="; ".join(item.get("publicationTypes") or []) or None,
        )
        p.add_source(Source.SEMANTIC_SCHOLAR, item.get("paperId", ""))
        return p
        
    def get_paper_by_doi(self, doi: str) -> Paper | None:
        url = f"{self.BASE_URL}/paper/DOI:{doi}"
        fields = "title,authors,year,abstract,externalIds,url,venue,publicationTypes"
        headers = {}
        api_key = get_semantic_scholar_api_key(self.config)
        if api_key:
            headers["x-api-key"] = api_key
            
        try:
            data = self._make_request(url, params={"fields": fields}, headers=headers)
            if data:
                return self._parse_paper(data)
        except (RequestException, ValueError, KeyError, TypeError, IndexError, AttributeError):
            return None
        return None

    def get_references(self, paper_id: str) -> list[Paper]:
        url = f"{self.BASE_URL}/paper/{paper_id}/references"
        fields = "title,authors,year,abstract,externalIds,url,venue"
        headers = {}
        api_key = get_semantic_scholar_api_key(self.config)
        if api_key:
            headers["x-api-key"] = api_key
            
        papers = []
        try:
            data = self._make_request(url, params={"fields": fields, "limit": 100}, headers=headers)
            for item in data.get("data", []):
                cited = item.get("citedPaper", {})
                if cited:
                    papers.append(self._parse_paper(cited))
        except (RequestException, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
            logger.warning("Failed to fetch references for %s: %s", paper_id, error)
        return papers
        
    def get_citations(self, paper_id: str) -> list[Paper]:
        url = f"{self.BASE_URL}/paper/{paper_id}/citations"
        fields = "title,authors,year,abstract,externalIds,url,venue"
        headers = {}
        api_key = get_semantic_scholar_api_key(self.config)
        if api_key:
            headers["x-api-key"] = api_key
            
        papers = []
        try:
            data = self._make_request(url, params={"fields": fields, "limit": 100}, headers=headers)
            for item in data.get("data", []):
                citing = item.get("citingPaper", {})
                if citing:
                    papers.append(self._parse_paper(citing))
        except (RequestException, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
            logger.warning("Failed to fetch citations for %s: %s", paper_id, error)
        return papers
