from typing import List, Dict
from rapidfuzz import fuzz
from .models import Paper, PaperStatus

def deduplicate(papers: List[Paper], config: dict) -> List[Paper]:
    """
    Deduplicates papers using exact DOI match and fuzzy title match.
    Updates paper status, sources, and adds screening decisions.
    
    Args:
        papers: List of Paper objects.
        config: Configuration dictionary.
        
    Returns:
        List of Paper objects with updated status and screening decisions.
    """
    dedup_config = config.get("dedup", {})
    fuzzy_config = dedup_config.get("title_fuzzy", {})
    fuzzy_enabled = fuzzy_config.get("enabled", True)
    fuzzy_threshold = fuzzy_config.get("threshold", 90)

    # Phase 1: DOI exact match
    doi_map: Dict[str, Paper] = {}
    active_papers: List[Paper] = []
    
    for paper in papers:
        if paper.status == PaperStatus.DUPLICATE:
            continue
            
        if paper.doi_normalized:
            if paper.doi_normalized in doi_map:
                canonical = doi_map[paper.doi_normalized]
                paper.status = PaperStatus.DUPLICATE
                paper.duplicate_of = canonical.id
                
                # Merge sources
                for source in paper.sources:
                    # Use string name or enum value as fallback if dictionary key is a string
                    source_key = source.name if hasattr(source, "name") else str(source)
                    ext_id = paper.source_ids.get(source, paper.source_ids.get(source_key, ""))
                    canonical.add_source(source, ext_id)
                canonical.citation_discoveries.extend(paper.citation_discoveries)
                    
                paper.add_screening_decision(
                    stage="dedup",
                    decision="DUPLICATE",
                    reason=f"Exact DOI match with {canonical.id}"
                )
            else:
                doi_map[paper.doi_normalized] = paper
                active_papers.append(paper)
        else:
            active_papers.append(paper)
            
    # Phase 2: Fuzzy title match
    if fuzzy_enabled:
        n = len(active_papers)
        is_duplicate = [False] * n
        
        for i in range(n):
            if is_duplicate[i]:
                continue
            canonical = active_papers[i]
            if not canonical.title_normalized or len(canonical.title_normalized) < 10:
                continue
                
            for j in range(i + 1, n):
                if is_duplicate[j]:
                    continue
                candidate = active_papers[j]
                if not candidate.title_normalized or len(candidate.title_normalized) < 10:
                    continue
                
                score = fuzz.ratio(canonical.title_normalized, candidate.title_normalized)
                
                is_match = False
                if score >= fuzzy_threshold:
                    is_match = True
                elif fuzzy_threshold - 5 <= score < fuzzy_threshold:
                    # Cross-validate: check year and first author match as tiebreaker
                    year_match = (canonical.year is not None and 
                                  candidate.year is not None and 
                                  canonical.year == candidate.year)
                    
                    canonical_first_author = canonical.authors[0] if canonical.authors else None
                    candidate_first_author = candidate.authors[0] if candidate.authors else None
                    author_match = (canonical_first_author is not None and 
                                    candidate_first_author is not None and 
                                    canonical_first_author == candidate_first_author)
                    
                    if year_match and author_match:
                        is_match = True
                        
                if is_match:
                    is_duplicate[j] = True
                    candidate.status = PaperStatus.DUPLICATE
                    candidate.duplicate_of = canonical.id
                    
                    # Merge sources
                    for source in candidate.sources:
                        source_key = source.name if hasattr(source, "name") else str(source)
                        ext_id = candidate.source_ids.get(source, candidate.source_ids.get(source_key, ""))
                        canonical.add_source(source, ext_id)
                    canonical.citation_discoveries.extend(candidate.citation_discoveries)
                        
                    candidate.add_screening_decision(
                        stage="dedup",
                        decision="DUPLICATE",
                        reason=f"Fuzzy title match with {canonical.id} (score: {score:.1f})"
                    )
                    
    # Mark non-duplicates as DEDUPED if they were NEW
    for paper in papers:
        if paper.status == PaperStatus.NEW:
            paper.status = PaperStatus.DEDUPED
            paper.add_screening_decision(
                stage="dedup",
                decision="DEDUPED",
                reason="Passed deduplication"
            )
            
    return papers
