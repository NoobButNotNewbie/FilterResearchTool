"""DOI and title normalization utilities."""

from __future__ import annotations

import re
import unicodedata

from unidecode import unidecode


# ---------------------------------------------------------------------------
# DOI normalization
# ---------------------------------------------------------------------------

_DOI_PREFIXES = [
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
    "DOI:",
]


def normalize_doi(doi: str | None) -> str | None:
    """Normalize a DOI string for dedup matching.
    
    - Strip common URL prefixes
    - Lowercase
    - Strip whitespace
    
    Returns None if input is None/empty.
    """
    if not doi:
        return None
    doi = doi.strip()
    for prefix in _DOI_PREFIXES:
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
            break
    doi = doi.lower().strip()
    if not doi:
        return None
    return doi


# ---------------------------------------------------------------------------
# Title normalization
# ---------------------------------------------------------------------------

_STRIP_PATTERN = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE = re.compile(r"\s+")


def normalize_title(title: str | None) -> str:
    """Normalize title for fuzzy dedup.
    
    - Unicode → ASCII (é → e)
    - Lowercase
    - Remove non-alphanumeric (except spaces)
    - Collapse whitespace
    - Strip
    
    Returns empty string if input is None/empty.
    """
    if not title:
        return ""
    # Unicode to ASCII
    text = unidecode(title)
    # Lowercase
    text = text.lower()
    # Remove special chars
    text = _STRIP_PATTERN.sub(" ", text)
    # Collapse spaces
    text = _MULTI_SPACE.sub(" ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Author normalization
# ---------------------------------------------------------------------------

def normalize_author(name: str) -> str:
    """Normalize author name for comparison.
    
    - Unicode → ASCII
    - Lowercase
    - Remove non-alpha (except spaces)
    - Collapse whitespace
    """
    if not name:
        return ""
    text = unidecode(name).lower()
    text = re.sub(r"[^a-z\s]", " ", text)
    text = _MULTI_SPACE.sub(" ", text).strip()
    return text


def normalize_author_list(authors: list[str]) -> list[str]:
    """Normalize a list of author names."""
    return [normalize_author(a) for a in authors if a]
