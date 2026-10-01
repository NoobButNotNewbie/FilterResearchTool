from filtertool.models import Paper, PaperStatus
from .taxonomy import Taxonomy

def classify_papers(
    papers: list[Paper], config: dict, include_candidates: bool = False
) -> list[Paper]:
    taxonomy = Taxonomy(config)
    valid_statuses = {PaperStatus.HUMAN_INCLUDED, PaperStatus.INCLUDED}
    if include_candidates:
        valid_statuses.update({
            PaperStatus.RULE_INCLUDED,
            PaperStatus.REVIEW_NEEDED,
            PaperStatus.SEMANTIC_HIGH,
            PaperStatus.SEMANTIC_REVIEW,
            PaperStatus.SEMANTIC_LOW,
        })
    
    for paper in papers:
        if paper.status not in valid_statuses:
            continue
            
        text = paper.get_text_for_analysis()
        labels = taxonomy.classify_text(text)
        
        paper.attack_methods_auto = labels["attack_methods"]
        paper.attack_stages_auto = labels["attack_stages"]
        paper.optimization_techniques_auto = labels["optimization_techniques"]
        paper.optimization_objectives_auto = labels.get("optimization_objectives", [])
        
        labels_str = f"Methods: {paper.attack_methods_auto}, Stages: {paper.attack_stages_auto}, Optimizations: {paper.optimization_techniques_auto}, Objectives: {paper.optimization_objectives_auto}"
        paper.add_screening_decision(
            stage="classification",
            decision="suggested",
            reason=f"Assigned labels: {labels_str}"
        )
        
    return papers
