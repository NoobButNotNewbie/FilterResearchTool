
from ..models import Paper, PaperStatus


def apply_rule_filter(papers: list[Paper], config: dict) -> list[Paper]:
    """Flag papers matching either research-protocol criterion as candidates."""
    rule_config = config.get("rule_filter", {})
    llm_terms = [k.lower() for k in rule_config.get("llm_terms", [])]
    offensive_terms = [k.lower() for k in rule_config.get("offensive_terms", [])]
    research_protocol = config.get("research_protocol", {})
    focus_dimensions = research_protocol.get("focus_dimensions", {})
    minimum_focus_dimensions = research_protocol.get("minimum_focus_dimensions", 1)
    for paper in papers:
        if any(
            decision.get("stage") == "human_screening:title_abstract"
            for decision in paper.screening_decisions
        ):
            continue
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
        if paper.status not in eligible_statuses:
            continue

        text_to_analyze = paper.get_text_for_analysis().lower()
        matched_llm = [k for k in llm_terms if k in text_to_analyze]
        matched_offensive = [k for k in offensive_terms if k in text_to_analyze]
        matched_focus_keywords = [
            str(keyword).lower()
            for dimension in focus_dimensions.values()
            for keyword in dimension.get("keywords", [])
            if str(keyword).lower() in text_to_analyze
        ]
        matched_focus_dimensions = [
            dimension
            for dimension, data in focus_dimensions.items()
            if any(
                keyword.lower() in text_to_analyze
                for keyword in data.get("keywords", [])
            )
        ]
        paper.keyword_hits = list(
            dict.fromkeys(matched_llm + matched_offensive + matched_focus_keywords)
        )
        paper.keyword_hit_count = len(paper.keyword_hits)
        paper.research_focus_hits = matched_focus_dimensions

        attack_match = bool(matched_llm and matched_offensive)
        optimization_match = bool(matched_llm) and (
            len(matched_focus_dimensions) >= minimum_focus_dimensions
        )
        matched_criteria = []
        if attack_match:
            matched_criteria.append("A: LLM/agent performs or supports offensive security")
        if optimization_match:
            matched_criteria.append("B: optimization of an LLM/agent")
        at_least_one_criterion = attack_match or optimization_match
        if at_least_one_criterion:
            paper.status = PaperStatus.RULE_INCLUDED
            paper.add_screening_decision(
                stage="rule_filter",
                decision="candidate",
                reason=(
                    f"Matched {', '.join(matched_criteria)}; "
                    f"Matched keywords: {', '.join(paper.keyword_hits)}; research dimensions: "
                    f"{', '.join(matched_focus_dimensions) or 'none'}"
                ),
            )
        else:
            paper.status = PaperStatus.REVIEW_NEEDED
            reasons = []
            if not matched_llm:
                reasons.append("no LLM/agent term matched")
            if not matched_offensive and not matched_focus_dimensions:
                reasons.append(
                    "neither an offensive-security term nor an optimization dimension matched"
                )
            elif matched_offensive and not matched_focus_dimensions:
                reasons.append(
                    "offensive-security terms matched without an LLM/agent term"
                )
            if matched_focus_dimensions and not matched_llm:
                reasons.append(
                    "optimization dimension matched without an LLM/agent term"
                )
            paper.add_screening_decision(
                stage="rule_filter",
                decision="review",
                reason=(
                    "; ".join(reasons)
                    or "Does not satisfy candidate rule; human review required"
                ),
            )

    return papers
