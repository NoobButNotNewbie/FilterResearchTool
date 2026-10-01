"""Run and search provenance helpers."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SEARCH_LOG_FIELDS = [
    "timestamp", "run_id", "activity", "source", "query", "year_filter", "n_returned", "n_new",
    "cache_used", "protocol_version", "config_hash", "error",
]


def _redact_config(value: Any, key: str = "") -> Any:
    key_lower = key.lower().replace("-", "_")
    sensitive_names = {"api_key", "contact_email", "email", "secret", "token"}
    sensitive_suffixes = ("_api_key", "_secret", "_token", "_email")
    if key_lower in sensitive_names or key_lower.endswith(sensitive_suffixes):
        return "<redacted>" if value else None
    if isinstance(value, dict):
        return {child_key: _redact_config(child, child_key) for child_key, child in value.items()}
    if isinstance(value, list):
        return [_redact_config(child) for child in value]
    return value


def config_hash(config: dict) -> str:
    canonical = json.dumps(_redact_config(config), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def append_search_log(path: str | Path, record: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SEARCH_LOG_FIELDS, extrasaction="ignore")
        if needs_header:
            writer.writeheader()
        writer.writerow(record)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
