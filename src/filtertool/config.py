"""Configuration loader for FilterTool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path) -> dict[str, Any]:
    """Load YAML config and return as dict.
    
    Raises FileNotFoundError if config doesn't exist.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if cfg is None:
        cfg = {}
    return cfg


def get_nested(cfg: dict, *keys: str, default: Any = None) -> Any:
    """Safely get nested config value.
    
    Example: get_nested(cfg, "search", "api", "timeout_seconds", default=30)
    """
    current = cfg
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key, default)
        else:
            return default
    return current
