import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner
from openpyxl import load_workbook
from requests import Response
from requests.exceptions import HTTPError

from filtertool.analysis import build_crosstab
from filtertool.classify.classifier import classify_papers
from filtertool.cli import cli
from filtertool.coding import auto_manual_agreement, import_manual_coding
from filtertool.export import export_crosstab_to_excel, export_search_query_reports
from filtertool.filter.rule_filter import apply_rule_filter
from filtertool.models import Paper, PaperStatus
from filtertool.pipeline import Pipeline
from filtertool.prisma import build_prisma_counts, select_search_events
from filtertool.provenance import append_search_log, config_hash
from filtertool.rate_limit import AdaptiveRateLimiter, get_rate_limiter
from filtertool.screening import apply_screening_rows, export_screening_sheet
from filtertool.search.base import BaseSearchAdapter
from filtertool.search.crossref import CrossrefAdapter
from filtertool.search.semantic_scholar import SemanticScholarAdapter
from filtertool.storage import PaperStore
from filtertool.verify import _make_request

CONFIG = {
    "rule_filter": {
        "llm_terms": ["large language model", "LLM", "AI agent"],
        "offensive_terms": ["penetration test", "exploit", "CTF"],
    },
    "taxonomy": {
        "attack_methods": {
            "penetration_testing": {"keywords": ["penetration test", "pentest"]},
            "ctf_solving": {"keywords": ["capture the flag", "ctf"]},
        },
        "attack_stages": {"exploitation": {"keywords": ["exploit"]}},
        "optimization_techniques": {"tool_use": {"keywords": ["tool use", "nmap"]}},
        "optimization_objectives": {"success_rate": {"keywords": ["success rate"]}},
    },
    "analysis": {"sparse_max_k": 1, "min_marginal_total": 1, "min_total_papers": 2},
}


def test_rule_filter_accepts_attack_criterion_without_legacy_exclusion_veto():
    llm_only = Paper(title="Large language model benchmark", status=PaperStatus.DEDUPED.value)
    both_groups = Paper(
        title="LLM agent for autonomous penetration testing",
        status=PaperStatus.DEDUPED.value,
    )
    attack_with_unrelated_exclusion_phrase = Paper(
        title="LLM agent for autonomous penetration testing and protein folding",
        status=PaperStatus.DEDUPED.value,
    )

    apply_rule_filter([llm_only, both_groups, attack_with_unrelated_exclusion_phrase], CONFIG)

    assert llm_only.status == PaperStatus.REVIEW_NEEDED.value
    assert both_groups.status == PaperStatus.RULE_INCLUDED.value
    assert attack_with_unrelated_exclusion_phrase.status == PaperStatus.RULE_INCLUDED.value


def test_rule_filter_accepts_either_attack_or_agent_optimization_branch():
    config = {
        **CONFIG,
        "research_protocol": {
            "minimum_focus_dimensions": 1,
            "focus_dimensions": {
                "token_efficiency": {"keywords": ["token efficiency"]},
                "memory_context_management": {"keywords": ["agent memory"]},
                "hallucination_mitigation": {"keywords": ["hallucination mitigation"]},
            },
        },
    }
    attack_only = Paper(
        title="LLM agent performs automated penetration testing",
        status=PaperStatus.DEDUPED.value,
    )
    optimization_only = Paper(
        title="LLM agent improves token efficiency",
        status=PaperStatus.DEDUPED.value,
    )
    both_criteria = Paper(
        title="LLM agent uses token efficiency in penetration testing",
        status=PaperStatus.DEDUPED.value,
    )
    neither = Paper(
        title="LLM agent benchmark on general language understanding",
        status=PaperStatus.DEDUPED.value,
    )

    apply_rule_filter([attack_only, optimization_only, both_criteria, neither], config)

    assert attack_only.status == PaperStatus.RULE_INCLUDED.value
    assert attack_only.research_focus_hits == []
    assert "A: LLM/agent performs or supports" in attack_only.screening_decisions[-1]["reason"]
    assert optimization_only.status == PaperStatus.RULE_INCLUDED.value
    assert optimization_only.research_focus_hits == ["token_efficiency"]
    assert "B: optimization of an LLM/agent" in optimization_only.screening_decisions[-1]["reason"]
    assert both_criteria.status == PaperStatus.RULE_INCLUDED.value
    assert "A: LLM/agent performs or supports" in both_criteria.screening_decisions[-1]["reason"]
    assert "B: optimization of an LLM/agent" in both_criteria.screening_decisions[-1]["reason"]
    assert neither.status == PaperStatus.REVIEW_NEEDED.value


def test_rule_filter_rescreens_automated_results_but_preserves_human_decisions():
    candidate = Paper(
        title="Large language model for penetration testing",
        status=PaperStatus.SEMANTIC_LOW.value,
    )
    unrelated = Paper(
        title="A study of clinical imaging",
        status=PaperStatus.SEMANTIC_HIGH.value,
    )
    reviewed = Paper(
        title="Human-reviewed paper",
        status=PaperStatus.HUMAN_EXCLUDED.value,
        human_decision="Exclude",
        reason_code="EC2",
    )
    reviewed.add_screening_decision(
        "human_screening:title_abstract", "exclude", "Out of scope", reason_code="EC2"
    )

    apply_rule_filter([candidate, unrelated, reviewed], CONFIG)

    assert candidate.status == PaperStatus.RULE_INCLUDED.value
    assert unrelated.status == PaperStatus.REVIEW_NEEDED.value
    assert reviewed.status == PaperStatus.HUMAN_EXCLUDED.value
    assert reviewed.human_decision == "Exclude"


def test_semantic_filter_rechecks_existing_semantic_statuses(monkeypatch):
    from filtertool.filter import semantic_filter

    class Scores:
        def __init__(self, values):
            self._values = values

        def max(self, dim):
            return self

        @property
        def values(self):
            return self

        def tolist(self):
            return self._values

    class Model:
        def encode(self, texts, convert_to_tensor=True):
            return texts

    class Similarity:
        @staticmethod
        def cos_sim(papers, references):
            return Scores([0.9 for _ in papers])

    paper = Paper(
        title="LLM agent automates penetration testing",
        status=PaperStatus.SEMANTIC_LOW.value,
    )
    paper.keyword_hit_count = 1
    monkeypatch.setattr(semantic_filter, "SentenceTransformer", Model)
    monkeypatch.setattr(semantic_filter, "_get_model", lambda _: Model())
    monkeypatch.setattr(semantic_filter, "util", Similarity)

    semantic_filter.apply_semantic_filter(
        [paper],
        {
            "semantic_filter": {
                "reference_sentences": ["LLM agents for penetration testing"],
                "high_threshold": 0.7,
                "low_threshold": 0.3,
                "semantic_weight": 0.8,
                "keyword_weight": 0.2,
            },
        },
    )

    assert paper.semantic_label == "HIGH"
    assert paper.status == PaperStatus.SEMANTIC_HIGH.value
    assert "Keyword signal: 1.000" in paper.screening_decisions[-1]["reason"]


def test_screening_import_validates_batch_before_mutation():
    paper = Paper(title="Candidate")
    rows = [
        {"id": paper.id, "human_decision": "Exclude", "reason_code": "EC1"},
        {"id": "unknown", "human_decision": "Include", "reason_code": "IC1"},
    ]

    try:
        apply_screening_rows([paper], rows)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown paper id should reject the import")

    assert paper.status == PaperStatus.NEW.value
    assert paper.screening_decisions == []


def test_human_decisions_drive_status_and_preserve_reason_codes():
    paper = Paper(title="Candidate")
    apply_screening_rows(
        [paper],
        [{"id": paper.id, "human_decision": "Include", "reason_code": "IC2"}],
    )
    assert paper.status == PaperStatus.HUMAN_INCLUDED.value
    assert paper.screening_decisions[-1]["reason_code"] == "IC2"

    apply_screening_rows(
        [paper],
        [{
            "id": paper.id,
            "screening_stage": "full_text",
            "human_decision": "Exclude",
            "reason_code": "EC2",
            "fulltext_retrieved": "yes",
        }],
    )
    assert paper.status == PaperStatus.FULLTEXT_EXCLUDED.value
    assert paper.screening_decisions[-1]["reason_code"] == "EC2"


def test_screening_import_skips_rows_without_decisions():
    reviewed = Paper(title="Reviewed candidate")
    pending = Paper(title="Not reviewed yet")

    imported = apply_screening_rows(
        [reviewed, pending],
        [
            {"id": reviewed.id, "human_decision": "Exclude", "reason_code": "EC1"},
            {"id": pending.id, "human_decision": ""},
        ],
    )

    assert imported == 1
    assert reviewed.status == PaperStatus.HUMAN_EXCLUDED.value
    assert pending.status == PaperStatus.NEW.value


def test_title_unsure_reappears_until_resolved(tmp_path):
    paper = Paper(title="Ambiguous LLM candidate", status=PaperStatus.REVIEW_NEEDED.value)
    apply_screening_rows(
        [paper],
        [{"id": paper.id, "screening_stage": "title_abstract", "human_decision": "Unsure"}],
    )
    path = tmp_path / "screening.xlsx"
    export_screening_sheet([paper], path, stage="title_abstract")
    workbook = load_workbook(path, read_only=True, data_only=True)
    rows = list(workbook["Screening"].iter_rows(min_row=2, values_only=True))
    workbook.close()
    assert rows[0][0] == paper.id

    apply_screening_rows(
        [paper],
        [{"id": paper.id, "screening_stage": "title_abstract", "human_decision": "Include", "reason_code": "IC1"}],
    )
    export_screening_sheet([paper], path, stage="title_abstract")
    workbook = load_workbook(path, read_only=True, data_only=True)
    assert workbook["Screening"].max_row == 1
    workbook.close()
    assert paper.status == PaperStatus.HUMAN_INCLUDED.value


def test_screening_sheet_has_fixed_dependent_reason_dropdowns(tmp_path):
    path = tmp_path / "screening.xlsx"
    export_screening_sheet([Paper(title="Candidate")], path)

    workbook = load_workbook(path)
    sheet = workbook["Screening"]
    validations = list(sheet.data_validations.dataValidation)

    assert any(validation.formula1 == '"Include,Exclude,Unsure"' for validation in validations)
    reason_validation = next(
        validation for validation in validations
        if validation.type == "list" and "N2" in str(validation.sqref)
    )
    assert "IncludeReasonCodes" in reason_validation.formula1
    assert "ExcludeReasonCodes" in reason_validation.formula1
    codebook = workbook["Codebook"]
    assert codebook["A2"].value == "IC1"
    assert codebook["A3"].value == "IC2"
    assert codebook["B2"].value == "EC1"
    assert codebook["B4"].value == "EC3"
    workbook.close()


def test_screening_date_updates_on_decision_or_reason_change_only():
    paper = Paper(title="Candidate")
    apply_screening_rows(
        [paper], [{"id": paper.id, "human_decision": "Include", "reason_code": "IC1"}]
    )
    assert paper.review_date == datetime.now().astimezone().date().isoformat()

    paper.review_date = "2000-01-01"
    apply_screening_rows(
        [paper], [{"id": paper.id, "human_decision": "Include", "reason_code": "IC1"}]
    )
    assert paper.review_date == "2000-01-01"

    apply_screening_rows(
        [paper], [{"id": paper.id, "human_decision": "Exclude", "reason_code": "EC1"}]
    )
    assert paper.review_date == datetime.now().astimezone().date().isoformat()


def test_screening_import_rejects_reason_codes_outside_codebook():
    paper = Paper(title="Candidate")
    try:
        apply_screening_rows(
            [paper], [{"id": paper.id, "human_decision": "Exclude", "reason_code": "EC9"}]
        )
    except ValueError as error:
        assert "reason_code" in str(error)
    else:
        raise AssertionError("reason codes outside the fixed codebook should be rejected")

    assert paper.status == PaperStatus.NEW.value


def test_prisma_counts_unresolved_unsure_separately():
    title_unsure = Paper(status=PaperStatus.REVIEW_NEEDED.value)
    title_unsure.add_screening_decision("human_screening:title_abstract", "unsure", "review")
    full_text_unsure = Paper(status=PaperStatus.FULLTEXT_ASSESSED.value)
    full_text_unsure.add_screening_decision("human_screening:title_abstract", "include", "IC1")
    full_text_unsure.add_screening_decision("human_screening:full_text", "unsure", "review")

    counts = build_prisma_counts(
        [title_unsure, full_text_unsure],
        search_events=[{"source": "openalex", "n_returned": 2}],
    )

    assert counts["title_abstract_awaiting_resolution"] == 1
    assert counts["full_text_unsure"] == 1
    assert counts["full_text_awaiting_resolution"] == 1
    assert counts["counts_are_consistent"] is False


def test_classifier_only_suggests_for_human_included_papers():
    automatic_candidate = Paper(
        title="LLM agent uses nmap to exploit systems",
        status=PaperStatus.SEMANTIC_HIGH.value,
    )
    human_candidate = Paper(
        title="LLM agent uses nmap to exploit systems",
        status=PaperStatus.HUMAN_INCLUDED.value,
    )

    classify_papers([automatic_candidate, human_candidate], CONFIG)

    assert automatic_candidate.attack_methods_auto == []
    assert human_candidate.status == PaperStatus.HUMAN_INCLUDED.value
    assert human_candidate.optimization_techniques_auto == ["tool_use"]
    assert human_candidate.optimization_objectives_auto == ["other_improvement"]


def test_candidate_suggestions_do_not_change_review_status():
    candidate = Paper(
        title="LLM agent for penetration testing",
        status=PaperStatus.SEMANTIC_HIGH.value,
    )

    classify_papers([candidate], CONFIG, include_candidates=True)

    assert candidate.attack_methods_auto == ["penetration_testing"]
    assert candidate.status == PaperStatus.SEMANTIC_HIGH.value


def test_broad_candidate_is_sent_to_human_review():
    candidate = Paper(
        title=(
            "LLM agent penetration test enumeration exploit tool use "
            "token usage success rate"
        ),
        status=PaperStatus.SEMANTIC_HIGH.value,
    )

    classify_papers(
        [candidate],
        {**CONFIG, "research_protocol": {"max_matched_topic_categories": 3}},
        include_candidates=True,
    )
    human_included = Paper(
        title=candidate.title,
        status=PaperStatus.HUMAN_INCLUDED.value,
    )
    classify_papers(
        [human_included],
        {**CONFIG, "research_protocol": {"max_matched_topic_categories": 3}},
        include_candidates=True,
    )

    assert candidate.status == PaperStatus.REVIEW_NEEDED.value
    breadth_decision = next(
        decision for decision in candidate.screening_decisions
        if decision["stage"] == "scope_breadth"
    )
    assert "distinct taxonomy topics" in breadth_decision["reason"]
    assert human_included.status == PaperStatus.HUMAN_INCLUDED.value


def test_prisma_uses_search_events_and_reason_codes():
    excluded = Paper(status=PaperStatus.HUMAN_EXCLUDED.value, sources=["openalex"])
    excluded.add_screening_decision(
        "human_screening:title_abstract", "exclude", "Out of scope", reason_code="EC2"
    )
    included = Paper(status=PaperStatus.INCLUDED.value, sources=["citation"])
    included.add_screening_decision(
        "human_screening:title_abstract", "include", "In scope", reason_code="IC1"
    )
    included.add_screening_decision(
        "human_screening:full_text", "include", "Full text included", reason_code="IC1"
    )
    counts = build_prisma_counts(
        [excluded, included],
        snowballing=[{"found": 3}],
        search_events=[{"source": "openalex", "n_returned": "10", "n_new": "2"}],
    )

    assert counts["identified_records"] == 13
    assert counts["identified_by_source"] == {"openalex": 10}
    assert counts["title_abstract_exclusions_by_reason"] == {"EC2": 1}
    assert counts["studies_included"] == 1
    assert counts["counts_are_consistent"]


def test_prisma_selects_one_manifest_run_and_excludes_pilot(tmp_path):
    config = {
        "protocol_version": "test-v1",
        "search": {"sources": ["semantic_scholar"], "api": {}},
        "storage": {
            "database_file": str(tmp_path / "papers.sqlite"),
            "cache_dir": str(tmp_path / "cache"),
        },
        "export": {"output_dir": str(tmp_path / "output")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    digest = config_hash(config)
    manifest = {
        "status": "complete",
        "config_hash": digest,
        "stages_completed": ["collect"],
        "last_successful_search_run_id": "official",
    }
    (output / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    common = {
        "timestamp": "t", "activity": "search", "source": "semantic_scholar",
        "query": "q", "year_filter": "2022-2026", "n_new": 0,
        "cache_used": "false", "protocol_version": "test-v1", "config_hash": digest,
    }
    log = output / "search_log.csv"
    append_search_log(log, dict(common, run_id="trial", n_returned=40))
    append_search_log(log, dict(common, run_id="official", n_returned=100))
    append_search_log(log, dict(common, run_id="pilot", activity="pilot", n_returned=20))

    pipeline = Pipeline(config_path)
    pipeline.run_prisma()
    default_counts = json.loads((output / "prisma_counts.json").read_text(encoding="utf-8"))
    assert default_counts["identified_records"] == 100

    pipeline.run_prisma(run_id="trial")
    explicit_counts = json.loads((output / "prisma_counts.json").read_text(encoding="utf-8"))
    assert explicit_counts["identified_records"] == 40


def test_prisma_selection_rejects_missing_or_invalid_run():
    events = [
        {"run_id": "bad", "activity": "search", "source": "openalex", "n_returned": "5", "error": "HTTP 429"}
    ]
    for run_id in (None, "missing", "bad"):
        try:
            select_search_events(events, run_id)
        except ValueError:
            pass
        else:
            raise AssertionError(f"run id {run_id!r} should be rejected")


def test_prepare_review_skips_prisma_without_successful_search_run(tmp_path, capsys):
    config = {
        "protocol_version": "test-v1",
        "search": {"sources": ["openalex"], "api": {}},
        "storage": {
            "database_file": str(tmp_path / "papers.sqlite"),
            "cache_dir": str(tmp_path / "cache"),
        },
        "export": {"output_dir": str(tmp_path / "output")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    pipeline = Pipeline(config_path)
    completed = []
    pipeline.run_normalize = lambda: completed.append("normalize")
    pipeline.run_dedup = lambda: completed.append("dedup")
    pipeline.run_rule_filter = lambda: completed.append("rule_filter")
    pipeline.run_semantic_filter = lambda: completed.append("semantic_filter")
    pipeline.run_classification = lambda include_candidates=False: completed.append("classification")
    pipeline.run_screening_export = lambda stage: completed.append("screening_export")
    pipeline.run_export = lambda: completed.append("export")

    def unexpected_prisma():
        raise AssertionError("PRISMA must not run without a successful search")

    pipeline.run_prisma = unexpected_prisma
    pipeline.run_stage("prepare_review")

    assert completed == [
        "normalize", "dedup", "rule_filter", "semantic_filter",
        "classification", "screening_export", "export",
    ]
    assert "PRISMA SKIPPED" in capsys.readouterr().out
    manifest = json.loads((tmp_path / "output" / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert not (tmp_path / "output" / "prisma_counts.json").exists()


def test_pipeline_rewrites_output_guide_for_each_run(tmp_path):
    config = {
        "storage": {
            "database_file": str(tmp_path / "papers.sqlite"),
            "cache_dir": str(tmp_path / "cache"),
        },
        "export": {"output_dir": str(tmp_path / "output")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    first_pipeline = Pipeline(config_path)
    guide_path = tmp_path / "output" / "OUTPUT_GUIDE.txt"
    first_run_id = first_pipeline.run_id
    first_guide = guide_path.read_text(encoding="utf-8")
    assert first_run_id in first_guide
    assert "screening_sheet.xlsx" in first_guide
    assert "crosstab.xlsx" in first_guide
    assert "Có thể nhập từng phần" in first_guide
    assert "import-screening --config config.yaml" in first_guide
    assert "import-coding --config config.yaml" in first_guide

    assert "không tải hoặc đọc bài" in first_guide

    guide_path.unlink()
    guide_path.parent.rmdir()
    second_pipeline = Pipeline(config_path)
    second_guide = guide_path.read_text(encoding="utf-8")

    assert second_pipeline.run_id != first_run_id
    assert second_pipeline.run_id in second_guide
    assert guide_path.exists()


def test_config_queries_have_auditable_keyword_groups():
    config = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    queries = config["search"]["queries"]
    query_keyword_groups = config["research_protocol"]["query_keyword_groups"]

    assert len(queries) == 5
    assert set(queries) == set(query_keyword_groups)
    assert all(query_keyword_groups[query]["llm_agent"] for query in queries)
    assert all(query_keyword_groups[query]["question_component"] for query in queries)
    assert {
        query_keyword_groups[query]["criterion"] for query in queries
    } == {"attack_execution_support", "agent_optimization"}
    assert sum(
        query_keyword_groups[query]["criterion"] == "attack_execution_support"
        for query in queries
    ) == 2
    assert sum(
        query_keyword_groups[query]["criterion"] == "agent_optimization"
        for query in queries
    ) == 3
    assert all(
        query_keyword_groups[query]["offensive_security"]
        for query in queries
        if query_keyword_groups[query]["criterion"] == "attack_execution_support"
    )
    assert all(
        query_keyword_groups[query]["focus_terms"]
        for query in queries
        if query_keyword_groups[query]["criterion"] == "agent_optimization"
    )
    assert {
        query_keyword_groups[query]["focus_dimension"]
        for query in queries
        if query_keyword_groups[query]["criterion"] == "agent_optimization"
    } == {
        "token_efficiency",
        "memory_context_management",
        "hallucination_mitigation",
    }
    assert sum(
        query_keyword_groups[query]["focus_dimension"] == "token_efficiency"
        for query in queries
    ) == 1
    assert sum(
        query_keyword_groups[query]["focus_dimension"] == "memory_context_management"
        for query in queries
    ) == 1
    assert sum(
        query_keyword_groups[query]["focus_dimension"] == "hallucination_mitigation"
        for query in queries
    ) == 1
    assert config["research_protocol"]["max_research_per_query"] == 20


def test_query_research_report_limits_unique_papers_and_splits_focus_sheets(tmp_path):
    query = "LLM agent token efficiency"
    config = {
        "search": {"sources": ["openalex", "crossref"], "queries": [query]},
        "research_protocol": {
            "max_research_per_query": 20,
            "query_keyword_groups": {
                query: {
                    "criterion": "agent_optimization",
                    "question_component": "Optimize LLM agents for token efficiency.",
                    "llm_agent": ["LLM agent"],
                    "offensive_security": ["penetration testing"],
                    "focus_dimension": "token_efficiency",
                    "focus_terms": ["token efficiency"],
                }
            },
            "focus_dimensions": {
                "token_efficiency": {
                    "keywords": ["token efficiency", "token usage"]
                },
                "memory_context_management": {"keywords": ["agent memory"]},
                "hallucination_mitigation": {"keywords": ["hallucination"]},
            },
        },
        "rule_filter": {
            "llm_terms": ["LLM"],
            "offensive_terms": ["penetration testing"],
        },
    }
    results = []
    for paper_number in range(25):
        record = {
            "id": f"paper-{paper_number}",
            "title": f"LLM agent penetration testing token efficiency paper {paper_number}",
            "title_normalized": f"paper {paper_number}",
            "authors": ["Author"],
            "year": 2025,
            "doi": f"10.1000/{paper_number}",
            "doi_normalized": f"10.1000/{paper_number}",
            "abstract": "Improves token efficiency for LLM agent penetration testing.",
            "url": "",
        }
        for source in ("openalex", "crossref"):
            results.append({
                "run_id": "test-run",
                "query": query,
                "source": source,
                "cache_used": False,
                "record": record,
            })

    summary_path, detail_path = export_search_query_reports(
        config,
        "test-run",
        [
            {"source": source, "query": query, "result_count": 25}
            for source in ("openalex", "crossref")
        ],
        results,
        tmp_path,
    )

    summary_book = load_workbook(summary_path, read_only=True)
    summary_sheet = summary_book["Query summary"]
    headers = [cell.value for cell in summary_sheet[3]]
    summary = dict(zip(headers, next(summary_sheet.iter_rows(
        min_row=4, max_row=4, values_only=True
    ))))
    assert summary["Unique research matching A or B"] == 25
    assert summary["Matching both A and B by keywords"] == 25
    assert summary["Research selected for detail report"] == 20
    summary_book.close()

    detail_book = load_workbook(detail_path, read_only=True)
    assert detail_book["Research by query"].max_row == 21
    token_sheet = detail_book["token_efficiency"]
    assert token_sheet.max_row == 21
    headers = [cell.value for cell in token_sheet[1]]
    first_record = dict(zip(headers, next(token_sheet.iter_rows(
        min_row=2, max_row=2, values_only=True
    ))))
    assert first_record["Sources"] == "crossref; openalex"
    assert first_record["Matched A/B keyword evidence (report-only)"] == (
        "Both A and B (keyword evidence)"
    )
    detail_book.close()


def test_query_report_prioritizes_both_criteria_then_balances_single_criterion(
    tmp_path,
):
    query = "LLM agent token efficiency"
    config = {
        "search": {"sources": ["openalex"], "queries": [query]},
        "research_protocol": {
            "max_research_per_query": 20,
            "minimum_focus_dimensions": 1,
            "query_keyword_groups": {
                query: {
                    "criterion": "agent_optimization",
                    "question_component": "Optimize LLM agents for token efficiency.",
                    "llm_agent": ["LLM agent"],
                    "offensive_security": [],
                    "focus_dimension": "token_efficiency",
                    "focus_terms": ["token efficiency"],
                }
            },
            "focus_dimensions": {
                "token_efficiency": {"keywords": ["token efficiency"]}
            },
        },
        "rule_filter": {
            "llm_terms": ["LLM"],
            "offensive_terms": ["penetration testing"],
        },
    }
    results = []
    papers = [
        (f"both-{number}", "LLM agent penetration testing token efficiency")
        for number in range(2)
    ]
    papers.extend(
        (f"attack-{number}", "LLM agent penetration testing")
        for number in range(12)
    )
    papers.extend(
        (f"optimization-{number}", "LLM agent token efficiency")
        for number in range(12)
    )
    for paper_id, title in papers:
        results.append({
            "run_id": "balanced-run",
            "query": query,
            "source": "openalex",
            "cache_used": False,
            "record": {
                "id": paper_id,
                "title": title,
                "title_normalized": title.casefold(),
                "authors": [],
                "year": 2025,
                "doi": f"10.1000/{paper_id}",
                "doi_normalized": f"10.1000/{paper_id}",
                "abstract": title,
                "url": "",
            },
        })

    summary_path, detail_path = export_search_query_reports(
        config,
        "balanced-run",
        [{"source": "openalex", "query": query, "result_count": len(results)}],
        results,
        tmp_path,
    )

    summary_book = load_workbook(summary_path, read_only=True)
    summary_sheet = summary_book["Query summary"]
    headers = [cell.value for cell in summary_sheet[3]]
    summary = dict(zip(headers, next(summary_sheet.iter_rows(
        min_row=4, max_row=4, values_only=True
    ))))
    assert summary["Matching both A and B by keywords"] == 2
    assert summary["Matching A only by keywords"] == 12
    assert summary["Matching B only by keywords"] == 12
    assert summary["Research selected for detail report"] == 20
    summary_book.close()

    detail_book = load_workbook(detail_path, read_only=True)
    sheet = detail_book["Research by query"]
    headers = [cell.value for cell in sheet[1]]
    records = [
        dict(zip(headers, row))
        for row in sheet.iter_rows(min_row=2, values_only=True)
    ]
    categories = [
        record["Matched A/B keyword evidence (report-only)"] for record in records
    ]
    assert categories[:2] == [
        "Both A and B (keyword evidence)",
        "Both A and B (keyword evidence)",
    ]
    assert categories.count("A only (keyword evidence)") == 9
    assert categories.count("B only (keyword evidence)") == 9
    assert categories[2:6] == [
        "A only (keyword evidence)",
        "B only (keyword evidence)",
        "A only (keyword evidence)",
        "B only (keyword evidence)",
    ]
    detail_book.close()


def test_query_research_report_accepts_attack_branch_without_optimization_term(tmp_path):
    query = "LLM agents perform automated penetration testing"
    config = {
        "search": {"sources": ["openalex"], "queries": [query]},
        "research_protocol": {
            "max_research_per_query": 20,
            "query_keyword_groups": {
                query: {
                    "criterion": "attack_execution_support",
                    "question_component": "Do LLM agents perform offensive tasks?",
                    "llm_agent": ["LLM agents"],
                    "offensive_security": ["penetration testing"],
                    "focus_dimension": None,
                    "focus_terms": [],
                }
            },
            "focus_dimensions": {
                "token_efficiency": {"keywords": ["token efficiency"]}
            },
        },
        "rule_filter": {
            "llm_terms": ["LLM"],
            "offensive_terms": ["penetration test"],
        },
    }
    result = {
        "run_id": "attack-run",
        "query": query,
        "source": "openalex",
        "cache_used": False,
        "record": {
            "id": "attack-paper",
            "title": "LLM agent for automated penetration testing",
            "title_normalized": "llm agent for automated penetration testing",
            "authors": [],
            "year": 2025,
            "doi": "10.1000/attack",
            "doi_normalized": "10.1000/attack",
            "abstract": "An LLM agent autonomously performs penetration testing.",
            "url": "https://example.org/attack",
        },
    }
    outside_keyword_evidence = {
        **result,
        "record": {
            **result["record"],
            "id": "query-returned-paper",
            "title": "Research result returned for this query",
            "title_normalized": "research result returned for this query",
            "doi": "10.1000/query-returned",
            "doi_normalized": "10.1000/query-returned",
            "abstract": "Relevant search result; no configured A/B phrase in metadata.",
        },
    }

    _, detail_path = export_search_query_reports(
        config,
        "attack-run",
        [{"source": "openalex", "query": query, "result_count": 2}],
        [result, outside_keyword_evidence],
        tmp_path,
    )

    workbook = load_workbook(detail_path, read_only=True)
    sheet = workbook["Research by query"]
    assert sheet.max_row == 3
    headers = [cell.value for cell in sheet[1]]
    rows = [
        dict(zip(headers, values))
        for values in sheet.iter_rows(
        min_row=2, max_row=2, values_only=True
        )
    ]
    record = rows[0]
    assert record["Natural-language criterion"] == "attack_execution_support"
    assert record["Title"] == "LLM agent for automated penetration testing"
    query_returned_record = dict(zip(headers, next(sheet.iter_rows(
        min_row=3, max_row=3, values_only=True
    ))))
    assert (
        query_returned_record["Matched A/B keyword evidence (report-only)"]
        == "No A/B keyword evidence"
    )
    url_column = headers.index("URL") + 1
    assert sheet.cell(row=2, column=url_column).value == "https://example.org/attack"
    workbook.close()


def test_config_hash_tracks_token_usage_but_redacts_credentials():
    base = {
        "taxonomy": {"optimization_objectives": {"token_usage": {"keywords": ["token"]}}},
        "search": {"api": {"semantic_scholar_api_key": "key-a", "contact_email": "a@example.org"}},
    }
    changed_taxonomy = {
        "taxonomy": {"optimization_objectives": {"token_usage": {"keywords": ["tokens consumed"]}}},
        "search": {"api": {"semantic_scholar_api_key": "key-a", "contact_email": "a@example.org"}},
    }
    changed_credentials = {
        "taxonomy": {"optimization_objectives": {"token_usage": {"keywords": ["token"]}}},
        "search": {"api": {"semantic_scholar_api_key": "key-b", "contact_email": "b@example.org"}},
    }

    assert config_hash(base) != config_hash(changed_taxonomy)
    assert config_hash(base) == config_hash(changed_credentials)


def test_analysis_uses_manual_codes_only_for_finally_included():
    included = Paper(
        status=PaperStatus.INCLUDED.value,
        attack_methods_manual=["penetration_testing"],
        optimization_techniques_manual=["tool_use"],
        optimization_objectives_manual=["success_rate"],
    )
    semantic_only = Paper(
        status=PaperStatus.SEMANTIC_HIGH.value,
        attack_methods_manual=["ctf_solving"],
        optimization_techniques_manual=["tool_use"],
    )
    report = build_crosstab([included, semantic_only], CONFIG)

    assert report["total_included"] == 1
    assert report["matrix"]["tool_use"]["penetration_testing"]["k"] == 1
    assert report["objective_matrix"]["tool_use"]["success_rate"]["k"] == 1
    assert all(cell["category"] != "ctf_solving" for cell in report["sparse_cells"])


def test_incomplete_coding_uses_coded_n_and_suppresses_sparse_cells(tmp_path):
    coded = [
        Paper(
            status=PaperStatus.INCLUDED.value,
            attack_methods_manual=["penetration_testing"],
            optimization_techniques_manual=["tool_use"],
            optimization_objectives_manual=["success_rate"],
        )
        for _ in range(3)
    ]
    uncoded = [Paper(status=PaperStatus.INCLUDED.value) for _ in range(2)]
    report = build_crosstab(coded + uncoded, CONFIG)

    method_coverage = report["matrix_coverage"]["optimization_technique_x_attack_method"]
    cell = report["matrix"]["tool_use"]["penetration_testing"]
    assert report["total_included"] == 5
    assert method_coverage["coded_n"] == 3 and method_coverage["complete"] is False
    assert cell["total_n"] == 3
    assert report["sparse_cells"] == []

    workbook_path = tmp_path / "crosstab.xlsx"
    export_crosstab_to_excel(report, workbook_path, gaps=report["sparse_cells"])
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    method_sheet = workbook["Technique x Method"]
    assert "3/5 coded" in method_sheet["A1"].value
    assert "CODING INCOMPLETE" in method_sheet["A2"].value
    assert method_sheet["F4"].value == 3
    assert "Coding incomplete" in workbook["Candidate Sparse Cells"]["A2"].value
    workbook.close()


def test_manual_code_import_and_exact_agreement():
    paper = Paper(
        status=PaperStatus.INCLUDED.value,
        attack_methods_auto=["penetration_testing"],
        optimization_techniques_auto=["tool_use"],
        optimization_objectives_auto=["success_rate"],
    )
    count = import_manual_coding(
        [paper],
        [{
            "id": paper.id,
            "attack_methods_manual": "penetration_testing",
            "optimization_techniques_manual": "tool_use",
            "optimization_objectives_manual": "success_rate",
            "baseline_compared": "manual baseline",
            "measured": "yes",
        }],
        CONFIG,
    )
    agreement = auto_manual_agreement([paper])

    assert count == 1
    assert paper.measured == "yes"
    assert agreement["attack_methods"]["exact_agreement_auto_manual"] == 1.0
    assert agreement["attack_methods"]["cohen_kappa_auto_manual"] == 1.0


def test_full_run_stops_after_search_errors_and_marks_manifest_invalid(tmp_path, monkeypatch):
    config = {
        "protocol_version": "test-v1",
        "search": {"sources": [], "queries": [], "api": {}},
        "storage": {
            "database_file": str(tmp_path / "papers.sqlite"),
            "cache_dir": str(tmp_path / "cache"),
        },
        "export": {"output_dir": str(tmp_path / "output")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    pipeline = Pipeline(config_path)
    normalized = []

    def failed_search():
        pipeline.run_errors.append({"stage": "search", "error": "fixture failure"})

    monkeypatch.setattr(pipeline, "run_search", failed_search)
    monkeypatch.setattr(pipeline, "run_normalize", lambda: normalized.append(True))

    try:
        pipeline.run_all()
    except RuntimeError:
        pass
    else:
        raise AssertionError("a search error must invalidate the full run")

    manifest = yaml.safe_load((tmp_path / "output" / "run_manifest.json").read_text())
    assert manifest["valid"] is False
    assert not normalized


def test_validation_sheet_lists_sparse_cells_for_human_search(tmp_path):
    crosstab = {
        "matrix": {},
        "objective_matrix": {},
        "sparse_cells": [{
            "dimension": "optimization_technique_x_attack_method",
            "optimization_technique": "tool_use",
            "category": "penetration_testing",
            "k": 1,
            "row_n": 20,
            "column_n": 10,
            "total_n": 25,
            "papers": ["paper-1"],
        }],
    }
    path = tmp_path / "crosstab.xlsx"
    export_crosstab_to_excel(crosstab, path, gaps=crosstab["sparse_cells"])
    workbook = load_workbook(path, data_only=True)

    sheet = workbook["Validation Search"]
    assert sheet["A2"].value == "optimization_technique_x_attack_method"
    assert sheet["B2"].value == "tool_use x penetration_testing"
    assert sheet["C2"].value is None


def test_crossref_contact_email_uses_mailto_query_parameter():
    adapter = CrossrefAdapter({"search": {"api": {"contact_email": "review@example.org"}}})
    with patch.object(adapter, "_make_request", return_value={"message": {"items": []}}) as request:
        adapter.search("LLM penetration testing", max_results=1)

    assert request.call_args.kwargs["params"]["mailto"] == "review@example.org"


def test_semantic_scholar_search_requests_only_parsed_fields():
    adapter = SemanticScholarAdapter({"search": {"api": {}}})
    with patch.object(adapter, "_make_request", return_value={"data": [], "total": 0}) as request:
        adapter.search("LLM penetration testing", max_results=100)

    fields = request.call_args.kwargs["params"]["fields"].split(",")
    assert fields == [
        "title", "authors", "year", "abstract", "externalIds", "url",
        "venue", "publicationTypes",
    ]


def test_search_discards_records_outside_year_range_and_missing_year(tmp_path):
    config = {
        "search": {
            "sources": ["fixture"],
            "queries": ["LLM security"],
            "max_results_per_query": 10,
            "year_from": 2025,
            "year_to": 2026,
        },
        "storage": {
            "database_file": str(tmp_path / "papers.sqlite"),
            "cache_dir": str(tmp_path / "cache"),
        },
        "export": {"output_dir": str(tmp_path / "output")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    class FixtureAdapter:
        def search(self, query, max_results):
            return [
                Paper(title="Old paper", year=2024),
                Paper(title="Current paper", year=2025),
                Paper(title="Unknown-year paper"),
                Paper(title="Future paper", year=2027),
            ]

    pipeline = Pipeline(config_path)
    with patch("filtertool.search.get_adapter", return_value=FixtureAdapter()):
        pipeline.run_search()

    papers = pipeline.store.get_all()
    assert [paper.title for paper in papers] == ["Current paper"]
    raw_records = [
        json.loads(line)
        for line in (tmp_path / "output" / "raw_search_results.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [record["record"]["title"] for record in raw_records] == ["Current paper"]
    summary_book = load_workbook(
        tmp_path / "output" / "search_query_summary.xlsx", read_only=True
    )
    summary_sheet = summary_book["Query summary"]
    summary_headers = [cell.value for cell in summary_sheet[3]]
    summary = dict(zip(summary_headers, next(summary_sheet.iter_rows(
        min_row=4, max_row=4, values_only=True
    ))))
    assert summary["Query"] == "LLM security"
    assert summary["Returned by: fixture"] == 1
    assert summary["Total source records (may contain cross-source duplicates)"] == 1
    assert summary["Unique research in query (DOI/title dedup across sources)"] == 1
    summary_book.close()

    research_book = load_workbook(
        tmp_path / "output" / "search_query_research.xlsx", read_only=True
    )
    research_sheet = research_book["Research by query"]
    research_headers = [cell.value for cell in research_sheet[1]]
    research = dict(zip(research_headers, next(research_sheet.iter_rows(
        min_row=2, max_row=2, values_only=True
    ))))
    assert research["Query"] == "LLM security"
    assert research["Title"] == "Current paper"
    assert research["Year"] == 2025
    assert research["Sources"] == "fixture"
    research_book.close()
    pipeline.store._conn.close()


def test_search_stops_requesting_provider_after_http_429(tmp_path):
    query_names = ["query one", "query two", "query three"]
    config = {
        "search": {
            "sources": ["fixture"],
            "queries": query_names,
            "api": {},
        },
        "storage": {
            "database_file": str(tmp_path / "papers.sqlite"),
            "cache_dir": str(tmp_path / "cache"),
        },
        "export": {"output_dir": str(tmp_path / "output")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    response = Response()
    response.status_code = 429
    failure = HTTPError("rate limited", response=response)

    class RateLimitedAdapter:
        def __init__(self):
            self.calls = []

        def search(self, query, max_results):
            self.calls.append(query)
            raise failure

    adapter = RateLimitedAdapter()
    pipeline = Pipeline(config_path)
    with patch("filtertool.search.get_adapter", return_value=adapter):
        pipeline.run_search()

    assert adapter.calls == ["query one"]
    assert len(pipeline.search_snapshots) == 3
    assert "HTTPError" in pipeline.search_snapshots[0]["error"]
    assert all("Skipped after this source returned HTTP 429" in snapshot["error"]
               for snapshot in pipeline.search_snapshots[1:])
    assert len(pipeline.run_warnings) == 1
    assert pipeline.run_warnings[0]["rate_limited"] is True
    pipeline.store._conn.close()


def test_search_cache_keeps_result_set_stable_when_only_screening_rules_change(tmp_path):
    config = {
        "search": {
            "sources": ["fixture"],
            "queries": ["LLM agent token efficiency"],
            "max_results_per_query": 10,
            "year_from": 2025,
            "year_to": 2026,
            "api": {},
        },
        "rule_filter": {"llm_terms": ["LLM"]},
        "storage": {
            "database_file": str(tmp_path / "papers.sqlite"),
            "cache_dir": str(tmp_path / "cache"),
        },
        "export": {"output_dir": str(tmp_path / "output")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    class FixtureAdapter:
        calls = 0

        def search(self, query, max_results):
            self.calls += 1
            return [
                Paper(title="Zeta paper", year=2025, abstract="token efficiency"),
                Paper(title="Alpha paper", year=2026, abstract="agent memory"),
            ]

    adapter = FixtureAdapter()
    with patch("filtertool.search.get_adapter", return_value=adapter):
        first = Pipeline(config_path)
        first.run_search()
        first_snapshot = first.search_snapshots[0]
        first.store._conn.close()

        config["rule_filter"]["llm_terms"] = ["language model"]
        config["search"]["queries"].append("LLM agent token efficiency new query")
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        second = Pipeline(config_path)
        second.run_search()
        second_snapshot = second.search_snapshots[0]

    assert adapter.calls == 2
    assert first_snapshot["result_set_sha256"] == second_snapshot["result_set_sha256"]
    assert second_snapshot["cache_used"] is True
    manifest_path = second.write_run_manifest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["research_protocol"]["search"]["queries"] == [
        "LLM agent token efficiency",
        "LLM agent token efficiency new query",
    ]
    assert manifest["search_snapshots"][0]["result_set_sha256"] == second_snapshot[
        "result_set_sha256"
    ]
    second.store._conn.close()


def test_dedup_summary_counts_existing_canonical_records(tmp_path, capsys):
    config = {
        "dedup": {"title_fuzzy": {"enabled": False}},
        "storage": {
            "database_file": str(tmp_path / "papers.sqlite"),
            "cache_dir": str(tmp_path / "cache"),
        },
        "export": {"output_dir": str(tmp_path / "output")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    pipeline = Pipeline(config_path)
    canonical = Paper(title="Canonical paper", status=PaperStatus.SEMANTIC_HIGH.value)
    duplicate = Paper(
        title="Duplicate paper",
        status=PaperStatus.DUPLICATE.value,
        duplicate_of=canonical.id,
    )
    pipeline.store.add_many([canonical, duplicate])
    pipeline.store.save()

    pipeline.run_dedup()

    assert "[DEDUP DONE] 1 unique, 1 duplicates" in capsys.readouterr().out
    pipeline.store._conn.close()


def test_full_reset_preserves_human_screening_audit(tmp_path):
    config = {
        "storage": {
            "database_file": str(tmp_path / "papers.sqlite"),
            "cache_dir": str(tmp_path / "cache"),
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    store = PaperStore(config["storage"]["database_file"], config["storage"]["cache_dir"])
    reviewed = Paper(
        title="Reviewed", status=PaperStatus.HUMAN_INCLUDED.value,
        human_decision="Include", reason_code="IC1",
    )
    reviewed.add_screening_decision(
        "human_screening:title_abstract", "include", "In scope", reason_code="IC1"
    )
    unreviewed = Paper(title="Unreviewed", status=PaperStatus.SEMANTIC_HIGH.value)
    unreviewed.add_screening_decision("semantic_filter", "HIGH", "score")
    store.add_many([reviewed, unreviewed])
    store.save()

    result = CliRunner().invoke(
        cli, ["reset", "--config", str(config_path)], input="y\n"
    )
    assert result.exit_code == 0, result.output
    store = PaperStore(config["storage"]["database_file"], config["storage"]["cache_dir"])

    assert store.get(reviewed.id).status == PaperStatus.HUMAN_INCLUDED.value
    assert store.get(reviewed.id).screening_decisions[-1]["reason_code"] == "IC1"
    assert store.get(unreviewed.id).status == PaperStatus.NEW.value
    assert store.get(unreviewed.id).screening_decisions


def test_verification_retries_respect_retry_after_and_spacing():
    throttled = type(
        "Response",
        (),
        {"status_code": 429, "headers": {"Retry-After": "12"}},
    )()
    success = type(
        "Response",
        (),
        {"status_code": 200, "headers": {}, "json": lambda self: {"ok": True}},
    )()

    with (
        patch("filtertool.verify.requests.get", side_effect=[throttled, success]) as request,
        patch("filtertool.rate_limit.time.sleep") as sleep,
    ):
        result = _make_request("https://example.test", 2, 5, 6, 10)

    assert result == {"ok": True}
    assert request.call_count == 2
    assert [call.args[0] for call in sleep.call_args_list] == [6, 12.0]


def test_citation_expansion_errors_are_recorded_once():
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.run_errors = []
    failure = {
        "seed_id": "seed-1",
        "direction": "references",
        "error": "HTTP 429",
    }

    pipeline._record_citation_errors([failure, failure])

    assert len(pipeline.run_errors) == 1
    assert pipeline.run_errors[0]["error"] == "HTTP 429"


def test_semantic_scholar_key_comes_from_environment():
    adapter = SemanticScholarAdapter({"search": {"api": {"semantic_scholar_api_key": None}}})
    with (
        patch.dict("os.environ", {"SEMANTIC_SCHOLAR_API_KEY": "dummy-test-key"}),
        patch.object(adapter, "_make_request", return_value={"data": [], "total": 0}) as request,
    ):
        adapter.search("LLM penetration testing", max_results=100)

    assert request.call_args.kwargs["headers"]["x-api-key"] == "dummy-test-key"


def test_semantic_scholar_endpoints_share_provider_floor():
    config = {
        "search": {
            "api": {
                "rate_limit_delay_seconds": 0,
                "minimum_interval_by_host_seconds": {"api.semanticscholar.org": 1.0},
            }
        }
    }
    search_limiter = get_rate_limiter(
        "https://api.semanticscholar.org/graph/v1/paper/search", config
    )
    citation_limiter = get_rate_limiter(
        "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1/x/references", config
    )

    assert search_limiter is citation_limiter
    assert search_limiter.minimum_interval == 1.0


def test_adaptive_limiter_starts_fast_and_recovers_gradually():
    limiter = AdaptiveRateLimiter(minimum_interval=0, successes_to_recover=2)
    with patch("filtertool.rate_limit.time.sleep") as sleep:
        limiter.wait()
        assert not sleep.called
        limiter.record_throttle(1)
        limiter.wait()
        assert sleep.call_args.args == (1,)
        limiter.record_success()
        limiter.record_success()

    assert limiter.current_interval == 0.8


def test_search_adapter_adapts_to_retry_after():
    adapter = BaseSearchAdapter({
        "search": {"api": {"retry_attempts": 2, "retry_delay_seconds": 1}}
    })
    throttled = type(
        "Response",
        (),
        {"status_code": 429, "headers": {"Retry-After": "2"}, "raise_for_status": lambda self: None},
    )()
    success = type(
        "Response",
        (),
        {
            "status_code": 200,
            "headers": {"Content-Type": "application/json"},
            "json": lambda self: {"ok": True},
            "raise_for_status": lambda self: None,
        },
    )()

    with (
        patch("filtertool.search.base.requests.get", side_effect=[throttled, success]),
        patch("filtertool.rate_limit.time.sleep") as sleep,
    ):
        result = adapter._make_request("https://adaptive-test.example/api")

    assert result == {"ok": True}
    assert [call.args[0] for call in sleep.call_args_list] == [2.0]


def test_search_adapter_retries_429_beyond_transient_retry_limit():
    adapter = BaseSearchAdapter({
        "search": {"api": {
            "retry_attempts": 2,
            "rate_limit_retry_attempts": 4,
            "retry_delay_seconds": 1,
        }}
    })
    throttled = type(
        "Response",
        (),
        {
            "status_code": 429,
            "headers": {},
            "raise_for_status": lambda self: None,
        },
    )()
    success = type(
        "Response",
        (),
        {
            "status_code": 200,
            "headers": {"Content-Type": "application/json"},
            "json": lambda self: {"ok": True},
            "raise_for_status": lambda self: None,
        },
    )()
    with (
        patch(
            "filtertool.search.base.requests.get",
            side_effect=[throttled, throttled, throttled, success],
        ) as request,
        patch("filtertool.rate_limit.time.sleep"),
    ):
        result = adapter._make_request("https://retry-429-test.example/api")

    assert result == {"ok": True}
    assert request.call_count == 4


def test_run_continues_and_marks_manifest_partial_for_search_warnings(tmp_path, monkeypatch):
    config = {
        "protocol_version": "test-v1",
        "search": {"sources": [], "queries": [], "api": {}},
        "storage": {
            "database_file": str(tmp_path / "papers.sqlite"),
            "cache_dir": str(tmp_path / "cache"),
        },
        "export": {"output_dir": str(tmp_path / "output")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    pipeline = Pipeline(config_path)
    completed = []

    def partial_search():
        pipeline.run_warnings.append({
            "stage": "search",
            "source": "semantic_scholar",
            "query": "fixture",
            "error": "HTTPError: 429",
            "run_id": pipeline.run_id,
        })
        pipeline.last_successful_search_run_id = pipeline.run_id

    monkeypatch.setattr(pipeline, "run_search", partial_search)
    monkeypatch.setattr(pipeline, "run_normalize", lambda: completed.append("normalize"))
    monkeypatch.setattr(pipeline, "run_dedup", lambda: completed.append("dedup"))
    monkeypatch.setattr(pipeline, "run_rule_filter", lambda: completed.append("rule_filter"))
    monkeypatch.setattr(pipeline, "run_semantic_filter", lambda: completed.append("semantic_filter"))
    monkeypatch.setattr(
        pipeline, "run_classification",
        lambda include_candidates=False: completed.append(
            f"classification:{include_candidates}"
        ),
    )
    monkeypatch.setattr(
        pipeline, "run_screening_export",
        lambda stage="title_abstract": completed.append("screening_export"),
    )
    monkeypatch.setattr(pipeline, "run_prisma", lambda run_id=None: completed.append("prisma"))
    monkeypatch.setattr(pipeline, "run_export", lambda: completed.append("export"))

    pipeline.run_all()

    manifest = json.loads(
        (tmp_path / "output" / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert completed == [
        "normalize", "dedup", "rule_filter", "semantic_filter", "classification:True",
        "screening_export", "prisma", "export",
    ]
    assert manifest["status"] == "partial"
    assert manifest["valid"] is True
    assert manifest["errors"] == []
    assert len(manifest["warnings"]) == 1
    pipeline.store._conn.close()


def test_prisma_can_use_successful_events_from_partial_search_run():
    events = [
        {"run_id": "partial", "activity": "search", "n_returned": 0, "error": "HTTP 429"},
        {"run_id": "partial", "activity": "search", "n_returned": 12, "error": ""},
        {"run_id": "other", "activity": "search", "n_returned": 20, "error": ""},
    ]

    selected = select_search_events(events, "partial", allow_partial=True)

    assert selected == [events[1]]
    try:
        select_search_events(events, "partial")
    except ValueError:
        pass
    else:
        raise AssertionError("partial search events must require explicit opt-in")
