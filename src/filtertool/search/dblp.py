from filtertool.models import Paper, Source
from filtertool.normalize import normalize_author_list, normalize_doi, normalize_title

from .base import BaseSearchAdapter


class DBLPAdapter(BaseSearchAdapter):
    BASE_URL = "https://dblp.org/search/publ/api"

    def search(self, query: str, max_results: int = 100) -> list[Paper]:
        papers = []
        offset = 0
        limit = min(100, max_results)

        while len(papers) < max_results:
            data = self._make_request(
                self.BASE_URL,
                params={"q": query, "format": "json", "h": limit, "f": offset},
            )
            hits = data.get("result", {}).get("hits", {}) if data else {}
            items = hits.get("hit", [])
            if isinstance(items, dict):
                items = [items]
            if not items:
                break

            for item in items:
                if len(papers) >= max_results:
                    break
                papers.append(self._parse_paper(item))

            offset += limit
            if offset >= int(hits.get("@total", 0)):
                break

        return papers

    def _parse_paper(self, item: dict) -> Paper:
        info = item.get("info", {})
        title = info.get("title", "")
        authors_data = info.get("authors", {}).get("author", [])
        if isinstance(authors_data, (str, dict)):
            authors_data = [authors_data]
        authors = [
            author.get("text", "") if isinstance(author, dict) else author
            for author in authors_data
        ]
        doi = info.get("doi") or None
        source_id = info.get("key") or item.get("@id", "")
        year = info.get("year")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None

        paper = Paper(
            id=doi or source_id,
            title=title,
            title_normalized=normalize_title(title),
            authors=normalize_author_list(authors),
            year=year,
            doi=doi,
            doi_normalized=normalize_doi(doi),
            abstract=info.get("abstract") or "",
            url=info.get("url") or item.get("@id", ""),
            venue=info.get("venue", ""),
            publication_type=info.get("type"),
        )
        paper.add_source(Source.DBLP, source_id)
        return paper