"""CLI entry point for FilterTool.

Usage:
    filtertool run --config config.yaml           # Run full pipeline
    filtertool run --config config.yaml --stage search   # Run single stage
    filtertool status --config config.yaml        # Show database status
    filtertool export --config config.yaml        # Export only
    filtertool reset --config config.yaml         # Reset all statuses to NEW
"""

from __future__ import annotations

import click
from pathlib import Path


@click.group()
@click.version_option(version="0.1.0", prog_name="FilterTool")
def cli():
    """FilterTool — Local literature screening & research mapping pipeline."""
    pass


@cli.command()
@click.option("--config", "-c", required=True, type=click.Path(exists=True),
              help="Path to YAML config file")
@click.option("--stage", "-s", default=None,
              help="Run a single stage: search, normalize, dedup, rule_filter, semantic_filter, "
                "screening_export, verification, citation_expansion, classification, analysis, export")
@click.option("--skip", multiple=True,
              help="Skip stage(s). Can be repeated: --skip search --skip verification")
@click.option("--no-cache", is_flag=True,
              help="Bypass cached search results for this run and record cache_used=false")
def run(config: str, stage: str | None, skip: tuple[str, ...], no_cache: bool):
    """Run the literature screening pipeline."""
    from filtertool.pipeline import Pipeline

    pipeline = Pipeline(config_path=config, no_cache=no_cache)

    if stage:
        click.echo(f"Running single stage: {stage}")
        pipeline.run_stage(stage)
    else:
        pipeline.run_all(skip_stages=list(skip) if skip else None)


@cli.command("collect")
@click.option("--config", "-c", required=True, type=click.Path(exists=True))
@click.option("--no-cache", is_flag=True, help="Fetch from sources instead of using saved search responses")
def collect(config: str, no_cache: bool):
    """Collect source records and search provenance without filtering them."""
    from filtertool.pipeline import Pipeline

    Pipeline(config_path=config, no_cache=no_cache).run_stage("collect")


@cli.command("prepare-review")
@click.option("--config", "-c", required=True, type=click.Path(exists=True))
def prepare_review(config: str):
    """Process local collected records and export the human screening workbook."""
    from filtertool.pipeline import Pipeline

    Pipeline(config_path=config).run_stage("prepare_review")


@cli.command("report")
@click.option("--config", "-c", required=True, type=click.Path(exists=True))
def report(config: str):
    """Rebuild reports from saved human decisions and manual codes."""
    from filtertool.pipeline import Pipeline

    Pipeline(config_path=config).run_stage("report")


@cli.command("screening-sheet")
@click.option("--config", "-c", required=True, type=click.Path(exists=True))
@click.option("--stage", type=click.Choice(["title_abstract", "full_text"]), default="title_abstract")
def screening_sheet(config: str, stage: str):
    """Export a human screening workbook for a screening stage."""
    from filtertool.pipeline import Pipeline

    pipeline = Pipeline(config_path=config)
    pipeline.run_screening_export(stage)
    pipeline.write_run_manifest()


@cli.command("import-screening")
@click.option("--config", "-c", required=True, type=click.Path(exists=True))
@click.argument("screening_file", type=click.Path(exists=True))
def import_screening(config: str, screening_file: str):
    """Import human screening decisions from XLSX or CSV."""
    from filtertool.pipeline import Pipeline
    from filtertool.screening import apply_screening_rows, read_screening_rows

    pipeline = Pipeline(config_path=config)
    rows = read_screening_rows(screening_file)
    count = apply_screening_rows(pipeline.store.get_all(), rows)
    pipeline.store.save()
    click.echo(f"Imported {count} human screening decisions")
    stages = {str(row.get("screening_stage") or "title_abstract").strip().lower() for row in rows}
    if "title_abstract" in stages:
        pipeline.run_screening_export("full_text")
    pipeline.run_classification()
    pipeline.run_analysis()
    pipeline.run_prisma()
    pipeline.run_export()
    pipeline.write_run_manifest()


@cli.command("import-coding")
@click.option("--config", "-c", required=True, type=click.Path(exists=True))
@click.argument("coding_file", type=click.Path(exists=True))
def import_coding(config: str, coding_file: str):
    """Import manual taxonomy codes from an edited coding workbook."""
    from filtertool.coding import import_manual_coding
    from filtertool.pipeline import Pipeline
    from filtertool.screening import read_screening_rows

    pipeline = Pipeline(config_path=config)
    count = import_manual_coding(
        pipeline.store.get_all(), read_screening_rows(coding_file), pipeline.config
    )
    pipeline.store.save()
    click.echo(f"Imported manual codes for {count} papers")
    pipeline.run_analysis()
    pipeline.run_prisma()
    pipeline.run_export()
    pipeline.write_run_manifest()


@cli.command("prisma")
@click.option("--config", "-c", required=True, type=click.Path(exists=True))
def prisma(config: str):
    """Export PRISMA-style flow counts from the local database."""
    from filtertool.pipeline import Pipeline

    pipeline = Pipeline(config_path=config)
    pipeline.run_prisma()
    pipeline.write_run_manifest()


@cli.command("agreement")
@click.option("--config", "-c", required=True, type=click.Path(exists=True))
def agreement(config: str):
    """Report auto/manual exact agreement and Cohen's kappa."""
    import json
    from filtertool.coding import classification_agreement
    from filtertool.pipeline import Pipeline

    pipeline = Pipeline(config_path=config)
    results = classification_agreement(pipeline.store.get_all())
    path = pipeline.output_dir / "classification_agreement.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    click.echo(f"Agreement report: {path}")
    for dimension, metrics in results.items():
        click.echo(f"{dimension}: n={metrics['n']}, agreement={metrics['exact_agreement']}, kappa={metrics['cohen_kappa']}")


@cli.command("pilot")
@click.option("--config", "-c", required=True, type=click.Path(exists=True))
@click.argument("title_file", type=click.Path(exists=True))
def pilot(config: str, title_file: str):
    """Check expected paper titles against each configured search source."""
    from filtertool.pipeline import Pipeline

    pipeline = Pipeline(config_path=config, no_cache=True)
    pipeline.run_pilot(title_file)


@cli.command()
@click.option("--config", "-c", required=True, type=click.Path(exists=True),
              help="Path to YAML config file")
def status(config: str):
    """Show current database status."""
    from filtertool.pipeline import Pipeline

    pipeline = Pipeline(config_path=config)
    click.echo(pipeline.store.summary())


@cli.command()
@click.option("--config", "-c", required=True, type=click.Path(exists=True),
              help="Path to YAML config file")
def export(config: str):
    """Export papers to Excel and JSON."""
    from filtertool.pipeline import Pipeline

    pipeline = Pipeline(config_path=config)
    pipeline.run_export()


@cli.command()
@click.option("--config", "-c", required=True, type=click.Path(exists=True),
              help="Path to YAML config file")
@click.option("--stage", "-s", default=None,
              help="Reset only papers at a specific stage")
@click.confirmation_option(prompt="This will reset paper statuses. Continue?")
def reset(config: str, stage: str | None):
    """Reset paper statuses to allow re-running stages."""
    from filtertool.config import load_config, get_nested
    from filtertool.storage import PaperStore
    from filtertool.models import PaperStatus

    cfg = load_config(config)
    store = PaperStore(
        db_path=get_nested(cfg, "storage", "database_file", default="./data/papers.json"),
        cache_dir=get_nested(cfg, "storage", "cache_dir", default="./data/cache"),
    )

    papers = store.get_all()
    reset_count = 0

    # Map stage names to the status they produce
    stage_statuses = {
        "dedup": [PaperStatus.DEDUPED.value, PaperStatus.DUPLICATE.value],
        "rule_filter": [PaperStatus.DEDUPED.value, PaperStatus.RULE_INCLUDED.value, PaperStatus.RULE_EXCLUDED.value, PaperStatus.REVIEW_NEEDED.value],
        "semantic_filter": [PaperStatus.SEMANTIC_HIGH.value, PaperStatus.SEMANTIC_REVIEW.value, PaperStatus.SEMANTIC_LOW.value],
        "verification": [PaperStatus.VERIFIED.value, PaperStatus.UNVERIFIED.value, PaperStatus.VERIFY_ERROR.value],
        "classification": [PaperStatus.CLASSIFIED.value],
    }

    for paper in papers:
        if stage:
            target_statuses = stage_statuses.get(stage, [])
            if paper.status in target_statuses:
                # Reset to the previous stage's output status
                if stage == "dedup":
                    paper.status = PaperStatus.NEW.value
                elif stage == "rule_filter":
                    paper.status = PaperStatus.DEDUPED.value
                elif stage == "semantic_filter":
                    paper.status = PaperStatus.RULE_INCLUDED.value
                elif stage == "verification":
                    if paper.semantic_label:
                        paper.status = f"semantic_{paper.semantic_label.lower()}"
                    else:
                        paper.status = PaperStatus.RULE_INCLUDED.value
                elif stage == "classification":
                    paper.status = PaperStatus.VERIFIED.value
                reset_count += 1
                store.update(paper)
        else:
            has_human_audit = any(
                str(decision.get("stage", "")).startswith("human_screening:")
                for decision in paper.screening_decisions
            )
            if paper.human_decision or has_human_audit:
                continue
            paper.status = PaperStatus.NEW.value
            paper.duplicate_of = None
            paper.semantic_score = None
            paper.semantic_label = None
            paper.keyword_hits = []
            paper.keyword_hit_count = 0
            paper.verification_results = {}
            paper.attack_methods = []
            paper.attack_stages = []
            paper.optimization_techniques = []
            paper.attack_methods_auto = []
            paper.attack_stages_auto = []
            paper.optimization_techniques_auto = []
            paper.optimization_objectives_auto = []
            reset_count += 1
            store.update(paper)

    store.save()
    click.echo(f"Reset {reset_count} papers" + (f" at stage '{stage}'" if stage else " (full reset)"))


@cli.command()
@click.option("--config", "-c", required=True, type=click.Path(exists=True),
              help="Path to YAML config file")
@click.argument("csv_path", type=click.Path(exists=True))
def import_csv(config: str, csv_path: str):
    """Import papers from a CSV file (must have 'title' column, optional: doi, authors, year, abstract, url)."""
    import csv
    from filtertool.config import load_config, get_nested
    from filtertool.storage import PaperStore
    from filtertool.models import Paper, Source
    from filtertool.normalize import normalize_doi, normalize_title

    cfg = load_config(config)
    store = PaperStore(
        db_path=get_nested(cfg, "storage", "database_file", default="./data/papers.json"),
        cache_dir=get_nested(cfg, "storage", "cache_dir", default="./data/cache"),
    )

    added = 0
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            paper = Paper(
                title=row.get("title", "").strip(),
                doi=row.get("doi", "").strip() or None,
                abstract=row.get("abstract", "").strip() or None,
                year=int(row["year"]) if row.get("year", "").strip().isdigit() else None,
                url=row.get("url", "").strip() or None,
                venue=row.get("venue", "").strip() or None,
            )
            # Authors
            authors_str = row.get("authors", "")
            if authors_str:
                paper.authors = [a.strip() for a in authors_str.split(";") if a.strip()]

            paper.add_source(Source.MANUAL.value)
            paper.doi_normalized = normalize_doi(paper.doi)
            paper.title_normalized = normalize_title(paper.title)

            # Skip if DOI already exists
            if paper.doi_normalized and store.get_by_doi(paper.doi_normalized):
                continue

            store.add(paper)
            added += 1

    store.save()
    click.echo(f"Imported {added} papers from {csv_path}")


if __name__ == "__main__":
    cli()
