
from ..models import Paper, PaperStatus


def apply_rule_filter(papers: list[Paper], config: dict) -> list[Paper]:
    """
    Applies keyword-based inclusion and exclusion rules to papers.
    Updates paper status and records screening decisions.
    
    Args:
        papers: List of Paper objects.
        config: Configuration dictionary.
        
    Returns:
        List of Paper objects with updated status.
    """
    rule_config = config.get("rule_filter", {})
    min_keyword_hits_base = rule_config.get("min_keyword_hits", 1)
    llm_terms = [k.lower() for k in rule_config.get("llm_terms", [])]
    offensive_terms = [k.lower() for k in rule_config.get("offensive_terms", [])]
    inclusion_keywords = [k.lower() for k in rule_config.get("inclusion_keywords", [])]
    if llm_terms and offensive_terms:
        inclusion_keywords = list(dict.fromkeys(llm_terms + offensive_terms))
    exclusion_keywords = [k.lower() for k in rule_config.get("exclusion_keywords", [])]
    for paper in papers:
        if any(
            decision.get("stage") == "human_screening:title_abstract"
            for decision in paper.screening_decisions
        ):
            continue
        was_keyword_excluded = any(
            decision.get("stage") == "rule_filter"
            and decision.get("decision") == "EXCLUDED"
            and decision.get("reason", "").startswith("Not enough inclusion keywords")
            for decision in paper.screening_decisions
        )
        eligible_statuses = {
            PaperStatus.DEDUPED,
            PaperStatus.REVIEW_NEEDED,
            PaperStatus.RULE_INCLUDED,
            PaperStatus.RULE_EXCLUDED,
            PaperStatus.SEMANTIC_HIGH,
            PaperStatus.SEMANTIC_REVIEW,
            PaperStatus.SEMANTIC_LOW,
            PaperStatus.VERIFIED,
            PaperStatus.UNVERIFIED,
            PaperStatus.VERIFY_ERROR,
        }
        if paper.status not in eligible_statuses and not was_keyword_excluded:
            continue
            
        text_to_analyze = paper.get_text_for_analysis().lower()
        has_abstract = bool(paper.abstract and paper.abstract.strip())
        
        # Edge case: paper with no abstract — lower the min threshold by 1 (but min 1)
        min_keyword_hits = max(1, min_keyword_hits_base - 1) if not has_abstract else min_keyword_hits_base
        
        matched_inclusion = []
        matched_exclusion = []
        
        # Pass 1: Check inclusion keywords
        for keyword in inclusion_keywords:
            if keyword in text_to_analyze:
                matched_inclusion.append(keyword)

        matched_llm = [k for k in llm_terms if k in text_to_analyze]
        matched_offensive = [k for k in offensive_terms if k in text_to_analyze]
                
        paper.keyword_hits = matched_inclusion
        paper.keyword_hit_count = len(matched_inclusion)
        
        # Pass 2: Check exclusion keywords
        for keyword in exclusion_keywords:
            if keyword in text_to_analyze:
                matched_exclusion.append(keyword)
                
        # Handle decision
        grouped_terms_match = bool(matched_llm and matched_offensive)
        keyword_threshold_met = paper.keyword_hit_count >= min_keyword_hits
        if grouped_terms_match and keyword_threshold_met and not matched_exclusion:
            paper.status = PaperStatus.RULE_INCLUDED
            paper.add_screening_decision(
                stage="rule_filter",
                decision="candidate",
                reason=f"Matched {paper.keyword_hit_count} inclusion keywords: {', '.join(matched_inclusion)}"
            )
        else:
            paper.status = PaperStatus.REVIEW_NEEDED
            reasons = []
            if llm_terms and not matched_llm:
                reasons.append("no LLM/agent term matched")
            if offensive_terms and not matched_offensive:
                reasons.append("no offensive-security term matched")
            if not keyword_threshold_met:
                reasons.append(f"keyword count {paper.keyword_hit_count} below {min_keyword_hits}")
            if matched_exclusion:
                reasons.append(f"possible exclusion terms: {', '.join(matched_exclusion)}")
            paper.add_screening_decision(
                stage="rule_filter",
                decision="review",
                reason="; ".join(reasons) or "Does not satisfy candidate rule; human review required"
            )
            
    return papers
