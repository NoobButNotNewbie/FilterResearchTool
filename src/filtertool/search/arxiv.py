import xml.etree.ElementTree as ET
from filtertool.models import Paper, Source
from filtertool.normalize import normalize_title, normalize_doi, normalize_author_list
from .base import BaseSearchAdapter

class ArxivAdapter(BaseSearchAdapter):
    BASE_URL = "http://export.arxiv.org/api/query"
    NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    
    def search(self, query: str, max_results: int = 100) -> list[Paper]:
        papers = []
        start = 0
        chunk_size = min(100, max_results)
        
        year_from = self.config.get("search", {}).get("year_from")
        year_to = self.config.get("search", {}).get("year_to")
        
        while len(papers) < max_results:
            params = {
                "search_query": f"all:{query}",
                "start": start,
                "max_results": chunk_size
            }
            
            xml_data = self._make_request(self.BASE_URL, params=params)
            if not xml_data:
                break
                
            try:
                root = ET.fromstring(xml_data)
            except ET.ParseError:
                break
                
            entries = root.findall("atom:entry", self.NS)
            if not entries:
                break
                
            for entry in entries:
                if len(papers) >= max_results:
                    break
                    
                paper = self._parse_entry(entry)
                
                if year_from and paper.year and paper.year < year_from:
                    continue
                if year_to and paper.year and paper.year > year_to:
                    continue
                    
                papers.append(paper)
                
            start += chunk_size
            
        return papers
        
    def _parse_entry(self, entry: ET.Element) -> Paper:
        title_el = entry.find("atom:title", self.NS)
        title = title_el.text.strip().replace("\n", " ") if title_el is not None else ""
        
        authors = []
        for author_el in entry.findall("atom:author", self.NS):
            name_el = author_el.find("atom:name", self.NS)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())
                
        year = None
        published_el = entry.find("atom:published", self.NS)
        if published_el is not None and published_el.text:
            try:
                year = int(published_el.text[:4])
            except ValueError:
                pass
                
        abstract_el = entry.find("atom:summary", self.NS)
        abstract = abstract_el.text.strip().replace("\n", " ") if abstract_el is not None else ""
        
        id_el = entry.find("atom:id", self.NS)
        arxiv_url = id_el.text.strip() if id_el is not None else ""
        arxiv_id = arxiv_url.split("/")[-1] if arxiv_url else ""
        
        doi = ""
        doi_el = entry.find("arxiv:doi", self.NS)
        if doi_el is not None and doi_el.text:
            doi = doi_el.text.strip()
            
        p = Paper(
            id=arxiv_id,
            title=title,
            title_normalized=normalize_title(title),
            authors=normalize_author_list(authors),
            year=year,
            doi=doi,
            doi_normalized=normalize_doi(doi),
            abstract=abstract,
            url=arxiv_url,
            venue="arXiv",
            publication_type="preprint",
        )
        p.add_source(Source.ARXIV, arxiv_id)
        return p
