from filtertool.models import Paper, PaperStatus


def _status_value(status: str | PaperStatus) -> str:
    return status.value if isinstance(status, PaperStatus) else str(status)


def _labels(paper: Paper, dimension: str, source: str) -> set[str]:
    field_name = f"{dimension}_{source}"
    values = getattr(paper, field_name, None)
    if values is None and source == "manual":
        legacy_field = {
            "attack_methods": "attack_methods",
            "attack_stages": "attack_stages",
            "optimization_techniques": "optimization_techniques",
        }.get(dimension)
        values = getattr(paper, legacy_field, []) if legacy_field else []
    return {value for value in (values or []) if value}


def _build_pair_matrix(papers, row_key, column_key, row_categories, column_categories, total):
    matrix = {
        row: {
            column: {"k": 0, "row_n": 0, "column_n": 0, "total_n": total, "papers": []}
            for column in column_categories
        }
        for row in row_categories
    }
    row_members = {row: set() for row in row_categories}
    column_members = {column: set() for column in column_categories}

    for paper in papers:
        rows = _labels(paper, row_key, "manual")
        columns = _labels(paper, column_key, "manual")
        for row in rows:
            if row in row_members:
                row_members[row].add(paper.id)
        for column in columns:
            if column in column_members:
                column_members[column].add(paper.id)
        for row in rows:
            for column in columns:
                if row in matrix and column in matrix[row]:
                    cell = matrix[row][column]
                    cell["k"] += 1
                    cell["papers"].append(paper.id)

    for row, columns in matrix.items():
        for column, cell in columns.items():
            cell["row_n"] = len(row_members[row])
            cell["column_n"] = len(column_members[column])
            cell["row_ratio"] = cell["k"] / cell["row_n"] if cell["row_n"] else 0.0
            cell["column_ratio"] = cell["k"] / cell["column_n"] if cell["column_n"] else 0.0
            cell["total_ratio"] = cell["k"] / total if total else 0.0
    return matrix


def build_crosstab(papers: list[Paper], config: dict) -> dict:
    analysis_config = config.get("analysis", {})
    taxonomy = config.get("taxonomy", {})
    included = [p for p in papers if _status_value(p.status) == PaperStatus.INCLUDED.value]
    total = len(included)
    methods = sorted(set(taxonomy.get("attack_methods", {})) | {
        label for paper in included for label in _labels(paper, "attack_methods", "manual")
    })
    techniques = sorted(set(taxonomy.get("optimization_techniques", {})) | {"none_detected"} | {
        label for paper in included for label in _labels(paper, "optimization_techniques", "manual")
    })
    objectives = sorted(set(taxonomy.get("optimization_objectives", {})) | {"other_improvement"} | {
        label for paper in included for label in _labels(paper, "optimization_objectives", "manual")
    })

    technique_method = _build_pair_matrix(
        included, "optimization_techniques", "attack_methods", techniques, methods, total
    )
    technique_objective = _build_pair_matrix(
        included, "optimization_techniques", "optimization_objectives", techniques, objectives, total
    )

    sparse_max_k = analysis_config.get("sparse_max_k", 2)
    min_marginal_total = analysis_config.get("min_marginal_total", 10)
    min_total_papers = analysis_config.get("min_total_papers", 30)
    sparse_cells = []
    for dimension, matrix in (
        ("optimization_technique_x_attack_method", technique_method),
        ("optimization_technique_x_objective", technique_objective),
    ):
        for row, columns in matrix.items():
            for column, cell in columns.items():
                if (
                    total >= min_total_papers
                    and cell["row_n"] >= min_marginal_total
                    and cell["column_n"] >= min_marginal_total
                    and cell["k"] <= sparse_max_k
                ):
                    sparse_cells.append({
                        "dimension": dimension,
                        "optimization_technique": row,
                        "category": column,
                        **cell,
                    })

    metrics = []
    for dimension, matrix in (
        ("optimization_technique_x_attack_method", technique_method),
        ("optimization_technique_x_objective", technique_objective),
    ):
        for row, columns in matrix.items():
            for column, cell in columns.items():
                metrics.append({
                    "dimension": dimension,
                    "optimization_technique": row,
                    "category": column,
                    **cell,
                })

    return {
        "matrix": technique_method,
        "objective_matrix": technique_objective,
        "metrics": metrics,
        "sparse_cells": sparse_cells,
        "total_included": total,
        "total_classified": total,
        "sparse_max_k": sparse_max_k,
        "min_marginal_total": min_marginal_total,
    }


def identify_gaps(crosstab: dict) -> list[dict]:
    """Return candidate sparse cells; these are not validated research gaps."""
    return sorted(
        crosstab.get("sparse_cells", []),
        key=lambda cell: (cell.get("k", 0), cell.get("dimension", ""), cell.get("category", "")),
    )
