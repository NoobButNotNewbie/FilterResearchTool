import time
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import requests
from requests.exceptions import RequestException, Timeout, ConnectionError, HTTPError

logger = logging.getLogger(__name__)

class BaseSearchAdapter:
    def __init__(self, config: dict):
        self.config = config
        
    def search(self, query: str, max_results: int = 100) -> list:
        raise NotImplementedError
        
    def _make_request(self, url, params=None, headers=None) -> dict | str:
        api_config = self.config.get("search", {}).get("api", {})
        retries = api_config.get("retry_attempts", 3)
        retry_delay = api_config.get("retry_delay_seconds", 5)
        rate_limit_delay = api_config.get("rate_limit_delay_seconds", 1)
        timeout = api_config.get("timeout_seconds", 30)
        
        for attempt in range(retries):
            try:
                time.sleep(rate_limit_delay)
                response = requests.get(url, params=params, headers=headers, timeout=timeout)
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                if "json" in content_type or "crossref" in url or "semanticscholar" in url or "openalex" in url:
                    return response.json()
                return response.text
            except (Timeout, ConnectionError, HTTPError) as e:
                logger.warning(f"Request failed (attempt {attempt + 1}/{retries}): {e}")
                if attempt < retries - 1:
                    delay = retry_delay
                    response = getattr(e, "response", None)
                    if response is not None and response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = max(delay * (2 ** attempt), float(retry_after)) if retry_after else delay * (2 ** attempt)
                        except ValueError:
                            try:
                                retry_at = parsedate_to_datetime(retry_after)
                                delay = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                            except (TypeError, ValueError):
                                delay *= 2 ** attempt
                    time.sleep(delay)
                else:
                    logger.error(f"Failed to fetch from {url} after {retries} attempts.")
                    raise


def get_contact_email(config: dict) -> str | None:
    """Read the polite-pool email from the documented API config location."""
    search = config.get("search", {})
    api = search.get("api", {})
    return api.get("contact_email") or config.get("contact_email")

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
