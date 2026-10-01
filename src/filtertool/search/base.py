import time
import logging
import os
import requests
from requests.exceptions import RequestException
from filtertool.rate_limit import get_rate_limiter, parse_retry_after

logger = logging.getLogger(__name__)

class BaseSearchAdapter:
    def __init__(self, config: dict):
        self.config = config
        
    def search(self, query: str, max_results: int = 100) -> list:
        raise NotImplementedError
        
    def _make_request(self, url, params=None, headers=None) -> dict | str:
        api_config = self.config.get("search", {}).get("api", {})
        retries = max(1, api_config.get("retry_attempts", 3))
        retry_delay = api_config.get("retry_delay_seconds", 1)
        timeout = api_config.get("timeout_seconds", 30)
        retryable_statuses = {408, 429, 500, 502, 503, 504}
        limiter = get_rate_limiter(url, self.config)

        for attempt in range(retries):
            limiter.wait()
            try:
                response = requests.get(url, params=params, headers=headers, timeout=timeout)
            except RequestException as error:
                if attempt < retries - 1:
                    delay = limiter.record_throttle(retry_delay * (2 ** attempt))
                    logger.warning(
                        "Request failed (attempt %s/%s), backing off %.2fs: %s",
                        attempt + 1, retries, delay, error,
                    )
                    continue
                raise

            if response.status_code in retryable_statuses:
                if attempt == retries - 1:
                    response.raise_for_status()
                delay = limiter.record_throttle(
                    retry_delay * (2 ** attempt),
                    parse_retry_after(response.headers.get("Retry-After")),
                )
                logger.warning(
                    "Request returned HTTP %s (attempt %s/%s), backing off %.2fs",
                    response.status_code, attempt + 1, retries, delay,
                )
                continue

            response.raise_for_status()
            limiter.record_success()
            content_type = response.headers.get("Content-Type", "")
            if "json" in content_type or any(
                source in url for source in ("crossref", "semanticscholar", "openalex")
            ):
                return response.json()
            return response.text


def get_contact_email(config: dict) -> str | None:
    """Read the polite-pool email from the documented API config location."""
    search = config.get("search", {})
    api = search.get("api", {})
    return api.get("contact_email") or config.get("contact_email")


def get_semantic_scholar_api_key(config: dict) -> str | None:
    """Prefer the local environment variable so API keys never need config commits."""
    search = config.get("search", {})
    api = search.get("api", {})
    configured = search.get("api_keys", {}).get("semantic_scholar") or api.get("semantic_scholar_api_key")
    return os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or configured

def get_adapter(source_name: str, config: dict) -> BaseSearchAdapter:
    from .semantic_scholar import SemanticScholarAdapter
    from .openalex import OpenAlexAdapter
    from .crossref import CrossrefAdapter
    from .dblp import DBLPAdapter
    from .arxiv import ArxivAdapter
    
    adapters = {
        "SEMANTIC_SCHOLAR": SemanticScholarAdapter,
        "OPENALEX": OpenAlexAdapter,
        "CROSSREF": CrossrefAdapter,
        "DBLP": DBLPAdapter,
        "ARXIV": ArxivAdapter
    }
    
    adapter_cls = adapters.get(source_name.upper())
    if not adapter_cls:
        raise ValueError(f"Unknown source: {source_name}")
    return adapter_cls(config)
