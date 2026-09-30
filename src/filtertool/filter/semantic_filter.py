from typing import List
from ..models import Paper, PaperStatus

try:
    from sentence_transformers import SentenceTransformer, util
except ImportError:
    SentenceTransformer = None
    util = None

# Cache the model in a module-level variable
_model = None

def _get_model(model_name: str):
    global _model
    if _model is None and SentenceTransformer is not None:
        _model = SentenceTransformer(model_name)
    return _model

def apply_semantic_filter(papers: List[Paper], config: dict) -> List[Paper]:
    """
    Applies semantic filtering using Sentence Transformers.
    Updates paper status, semantic score, and semantic label.
    
    Args:
        papers: List of Paper objects.
        config: Configuration dictionary.
        
    Returns:
        List of Paper objects with updated status.
    """
    semantic_config = config.get("semantic_filter", {})
    model_name = semantic_config.get("model_name", "all-MiniLM-L6-v2")
    reference_sentences = semantic_config.get("reference_sentences", [])
    high_threshold = semantic_config.get("high_threshold", 0.7)
    low_threshold = semantic_config.get("low_threshold", 0.4)
    keyword_weight = semantic_config.get("keyword_weight", 0.3)
    semantic_weight = semantic_config.get("semantic_weight", 0.7)
    
    rule_config = config.get("rule_filter", {})
    keyword_target = max(1, rule_config.get("min_keyword_hits", 1))
        
    # Semantic scores prioritize human review; they do not determine inclusion.
    eligible_statuses = {PaperStatus.RULE_INCLUDED, PaperStatus.REVIEW_NEEDED}
    papers_to_process = [p for p in papers if p.status in eligible_statuses]
    
    if not papers_to_process:
        return papers
        
    if not reference_sentences or SentenceTransformer is None:
        # Preserve papers and make the unavailable stage visible in the audit.
        reason = "Semantic screening unavailable: reference sentences missing" if not reference_sentences else "Semantic screening unavailable: sentence-transformers is not installed"
        for paper in papers_to_process:
            paper.semantic_label = "REVIEW"
            paper.status = PaperStatus.SEMANTIC_REVIEW
            paper.add_screening_decision("semantic_filter", "REVIEW", reason)
        return papers
        
    model = _get_model(model_name)
    if model is None:
        for paper in papers_to_process:
            paper.semantic_label = "REVIEW"
            paper.status = PaperStatus.SEMANTIC_REVIEW
            paper.add_screening_decision("semantic_filter", "REVIEW", "Semantic model could not be loaded")
        return papers
        
    # Encode reference sentences
    ref_embeddings = model.encode(reference_sentences, convert_to_tensor=True)
    
    # Get texts and batch encode
    texts = [p.get_text_for_analysis() for p in papers_to_process]
    
    valid_indices = []
    valid_texts = []
    for i, text in enumerate(texts):
        if text.strip():
            valid_indices.append(i)
            valid_texts.append(text)
            
    if valid_texts:
        paper_embeddings = model.encode(valid_texts, convert_to_tensor=True)
        cosine_scores = util.cos_sim(paper_embeddings, ref_embeddings)
        max_scores = cosine_scores.max(dim=1).values.tolist()
    else:
        max_scores = []
        
    score_idx = 0
    for i, paper in enumerate(papers_to_process):
        text = texts[i]
        
        # Edge case: empty text -> score 0
        if not text.strip():
            semantic_sim = 0.0
        else:
            semantic_sim = max_scores[score_idx]
            score_idx += 1
            
        keyword_ratio = min(1.0, paper.keyword_hit_count / keyword_target)
        final_score = (semantic_weight * semantic_sim) + (keyword_weight * keyword_ratio)
        
        paper.semantic_score = final_score
        
        if final_score >= high_threshold:
            label = "HIGH"
            new_status = PaperStatus.SEMANTIC_HIGH
        elif final_score >= low_threshold:
            label = "REVIEW"
            new_status = PaperStatus.SEMANTIC_REVIEW
        else:
            label = "LOW"
            new_status = PaperStatus.SEMANTIC_LOW
            
        paper.semantic_label = label
        paper.status = new_status
        
        paper.add_screening_decision(
            stage="semantic_filter",
            decision=label,
            reason=f"Semantic similarity: {semantic_sim:.3f}, Keyword ratio: {keyword_ratio:.3f}",
            score=final_score
        )
        
    return papers
