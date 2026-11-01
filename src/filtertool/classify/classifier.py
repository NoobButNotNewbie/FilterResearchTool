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
    max_topic_categories = config.get("research_protocol", {}).get(
        "max_matched_topic_categories"
    )
    
    for paper in papers:
        if paper.status not in valid_statuses:
            continue
        has_human_screening = any(
            str(decision.get("stage", "")).startswith("human_screening:")
            for decision in paper.screening_decisions
        )
        is_automated_candidate = (
            include_candidates
            and paper.status not in {PaperStatus.HUMAN_INCLUDED, PaperStatus.INCLUDED}
            and not has_human_screening
        )
            
        text = paper.get_text_for_analysis()
        labels = taxonomy.classify_text(text)
        
        paper.attack_methods_auto = labels["attack_methods"]
        paper.attack_stages_auto = labels["attack_stages"]
        paper.optimization_techniques_auto = labels["optimization_techniques"]
        paper.optimization_objectives_auto = labels.get("optimization_objectives", [])

        matched_topic_categories = (
            paper.attack_methods_auto
            + paper.attack_stages_auto
            + [
                label for label in paper.optimization_techniques_auto
                if label != "none_detected"
            ]
            + [
                label for label in paper.optimization_objectives_auto
                if label != "other_improvement"
            ]
        )
        if (
            is_automated_candidate
            and max_topic_categories is not None
            and len(matched_topic_categories) > max_topic_categories
        ):
            paper.status = PaperStatus.REVIEW_NEEDED.value
            paper.add_screening_decision(
                stage="scope_breadth",
                decision="review",
                reason=(
                    f"Matched {len(matched_topic_categories)} distinct taxonomy "
                    f"topics (maximum for automatic candidate: {max_topic_categories}): "
                    f"{', '.join(matched_topic_categories)}"
                ),
            )
        
        labels_str = f"Methods: {paper.attack_methods_auto}, Stages: {paper.attack_stages_auto}, Optimizations: {paper.optimization_techniques_auto}, Objectives: {paper.optimization_objectives_auto}"
        paper.add_screening_decision(
            stage="classification",
            decision="suggested",
            reason=f"Assigned labels: {labels_str}"
        )
        
    return papers
