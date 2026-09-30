"""SQLite-backed local paper store and persistent API response cache."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from filtertool.models import Paper


class PaperStore:
    """Persistent local store. Paper JSON payloads preserve the extensible model."""

    def __init__(self, db_path: str | Path, cache_dir: str | Path | None = None):
        self.db_path = Path(db_path)
        self.cache_dir = Path(cache_dir) if cache_dir else self.db_path.parent / "cache"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, doi TEXT, title TEXT, status TEXT NOT NULL, payload TEXT NOT NULL)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS papers_doi_idx ON papers(doi)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS api_cache (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self._conn.commit()
        self._papers: dict[str, Paper] = {}
        self._doi_index: dict[str, str] = {}
        self._title_index: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        for payload, in self._conn.execute("SELECT payload FROM papers"):
            try:
                paper = Paper.from_dict(json.loads(payload))
            except (ValueError, TypeError):
                continue
            self._index(paper)

    def _index(self, paper: Paper) -> None:
        self._papers[paper.id] = paper
        if paper.doi_normalized:
            self._doi_index[paper.doi_normalized] = paper.id
        if paper.title_normalized:
            self._title_index[paper.title_normalized] = paper.id

    def save(self) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT INTO papers(id,doi,title,status,payload) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET doi=excluded.doi,title=excluded.title,status=excluded.status,payload=excluded.payload",
                [(p.id, p.doi_normalized, p.title_normalized, str(p.status), json.dumps(p.to_dict(), ensure_ascii=False)) for p in self._papers.values()],
            )

    def add(self, paper: Paper) -> Paper:
        self._index(paper)
        return paper

    def add_many(self, papers: list[Paper]) -> int:
        for paper in papers:
            self.add(paper)
        return len(papers)

    def get(self, paper_id: str) -> Paper | None:
        return self._papers.get(paper_id)

    def get_by_doi(self, doi_normalized: str) -> Paper | None:
        pid = self._doi_index.get(doi_normalized)
        return self._papers.get(pid) if pid else None

    def get_by_title(self, title_normalized: str) -> Paper | None:
        pid = self._title_index.get(title_normalized)
        return self._papers.get(pid) if pid else None

    def get_all(self) -> list[Paper]:
        return list(self._papers.values())

    def get_by_status(self, *statuses: str) -> list[Paper]:
        return [p for p in self._papers.values() if p.status in set(statuses)]

    def count(self) -> int:
        return len(self._papers)

    def count_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for paper in self._papers.values():
            counts[paper.status] = counts.get(paper.status, 0) + 1
        return counts

    def update(self, paper: Paper) -> None:
        self._index(paper)

    def cache_key(self, prefix: str, identifier: str) -> str:
        return f"{prefix}_{hashlib.md5(f'{prefix}:{identifier}'.encode()).hexdigest()}"

    def get_cache(self, key: str) -> Any | None:
        row = self._conn.execute("SELECT payload FROM api_cache WHERE cache_key=?", (key,)).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except (ValueError, TypeError):
                return None
        # Read old file cache for backwards compatibility.
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return None
        return None

    def set_cache(self, key: str, data: Any) -> None:
        with self._conn:
            self._conn.execute("INSERT INTO api_cache(cache_key,payload) VALUES(?,?) ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload", (key, json.dumps(data, ensure_ascii=False)))

    def has_cache(self, key: str) -> bool:
        if self._conn.execute("SELECT 1 FROM api_cache WHERE cache_key=?", (key,)).fetchone():
            return True
        return (self.cache_dir / f"{key}.json").exists()

    def summary(self) -> str:
        counts = self.count_by_status()
        return "\n".join([f"Total papers: {self.count()}", *(f"  {status}: {count}" for status, count in sorted(counts.items()))])
