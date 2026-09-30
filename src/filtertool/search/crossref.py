from filtertool.models import Paper, Source
from filtertool.normalize import normalize_title, normalize_doi, normalize_author_list
from .base import BaseSearchAdapter, get_contact_email

class CrossrefAdapter(BaseSearchAdapter):
    BASE_URL = "https://api.crossref.org/works"
    
    def search(self, query: str, max_results: int = 100) -> list[Paper]:
        papers = []
        offset = 0
        rows = min(100, max_results)
        
        email = get_contact_email(self.config)
            
        filters = []
        year_from = self.config.get("search", {}).get("year_from")
        year_to = self.config.get("search", {}).get("year_to")
        if year_from:
            filters.append(f"from-pub-date:{year_from}")
        if year_to:
            filters.append(f"until-pub-date:{year_to}")
            
        filter_str = ",".join(filters) if filters else None
        
        while len(papers) < max_results:
            params = {
                "query": query,
                "rows": rows,
                "offset": offset
            }
            if filter_str:
                params["filter"] = filter_str
            if email:
                params["mailto"] = email
                
            data = self._make_request(self.BASE_URL, params=params)
            if not data or "message" not in data or "items" not in data["message"]:
                break
                
            items = data["message"]["items"]
            if not items:
                break
                
            for item in items:
                if len(papers) >= max_results:
                    break
                papers.append(self._parse_paper(item))
                
            offset += rows
            
        return papers
        
    def _parse_paper(self, item: dict) -> Paper:
        title = ""
        if item.get("title"):
            title = item["title"][0]
            
        authors = []
        for a in item.get("author", []):
            given = a.get("given", "")
            family = a.get("family", "")
            if given or family:
                authors.append(f"{given} {family}".strip())
                
        year = None
        pub_date = item.get("published-print") or item.get("published-online")
        if pub_date and pub_date.get("date-parts"):
            year = pub_date["date-parts"][0][0]
            
        doi = item.get("DOI", "")
        abstract = item.get("abstract", "")
        venue = ""
        if item.get("container-title"):
            venue = item["container-title"][0]
            
        p = Paper(
            id=doi,
            title=title,
            title_normalized=normalize_title(title),
            authors=normalize_author_list(authors),
            year=year,
            doi=doi,
            doi_normalized=normalize_doi(doi),
            abstract=abstract,
            url=item.get("URL", ""),
            venue=venue,
            publication_type=item.get("type"),
        )
        p.add_source(Source.CROSSREF, doi)
        return p
        
    def get_paper_by_doi(self, doi: str) -> Paper | None:
        url = f"{self.BASE_URL}/{doi}"
        email = get_contact_email(self.config)
        params = {"mailto": email} if email else {}
            
        try:
            data = self._make_request(url, params=params)
            if data and "message" in data:
                return self._parse_paper(data["message"])
        except Exception:
            return None
        return None
