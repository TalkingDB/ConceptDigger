"""
Async client for the English Wikipedia lead text ("abstract") of graph pages.

Public DBpedia no longer serves rdfs:comment / dbo:abstract, so abstracts come
from the source DBpedia took them from: the plain-text intro of the Wikipedia
article, via the MediaWiki action API (TextExtracts). One request covers up to
20 titles (the API's limit for intro extracts).

The API rewrites the titles we send: "Maida_(flour)" is normalized to
"Maida (flour)", and redirects are followed. Both rewrites are mapped back so
every result is keyed by the graph local name we asked for.

Pages with no abstract map to None: missing pages, empty extracts and
disambiguation pages ("X may refer to: ...", which describe no one entity).
"""
import asyncio
import time
from typing import Dict, Iterable, List, Optional
from urllib.parse import unquote

import httpx

from .config import settings

# The API caps intro extracts at 20 pages per request.
MAX_TITLES_PER_REQUEST = 20

_RETRYABLE_STATUSES = {429, 502, 503, 504}
_RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)
# Characters that are never valid in a Wikipedia title. "|" also separates
# titles in the request, so one bad name would corrupt the whole batch.
_INVALID_TITLE_CHARS = set("#<>[]{}|")


class WikipediaTransientError(Exception):
    """Wikipedia was slow or unavailable after retries. Safe to retry later."""


def to_title(local_name: str) -> str:
    """Wikipedia title for a graph local name. DBpedia percent-encodes some
    characters in its URIs ('Got_Rice%3F' is the article 'Got Rice?'); the API
    needs them decoded, or it looks for a page literally called 'Got Rice%3F'."""
    return unquote(local_name)


def is_valid_title(local_name: str) -> bool:
    title = to_title(local_name)
    return bool(title) and not any(c in _INVALID_TITLE_CHARS for c in title)


def _clean(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = " ".join(text.split())
    return text or None


class WikipediaClient:
    def __init__(
        self,
        api_url: str,
        user_agent: str,
        timeout: float,
        min_interval: float = 0.0,
        max_retries: int = 0,
    ):
        self.api_url = api_url
        self.user_agent = user_agent
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._throttle_lock = asyncio.Lock()
        self._last_call_at = 0.0

    async def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._throttle_lock:
            wait = self.min_interval - (time.monotonic() - self._last_call_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_at = time.monotonic()

    async def _backoff(self, attempt: int, retry_after: Optional[str] = None) -> None:
        try:
            delay = float(retry_after) if retry_after else 2.0 * attempt
        except ValueError:
            delay = 2.0 * attempt
        await asyncio.sleep(delay)

    async def _get(self, client: httpx.AsyncClient, params: dict) -> dict:
        attempt = 0
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        while True:
            await self._throttle()
            try:
                resp = await client.get(
                    self.api_url,
                    params=params,
                    headers=headers,
                    timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout), pool=10.0),
                )
            except _RETRYABLE_EXCEPTIONS as exc:
                if attempt >= self.max_retries:
                    raise WikipediaTransientError(
                        f"Wikipedia lookup failed after {attempt + 1} attempt(s): {exc}"
                    ) from exc
                attempt += 1
                await self._backoff(attempt)
                continue

            if resp.status_code in _RETRYABLE_STATUSES:
                if attempt >= self.max_retries:
                    raise WikipediaTransientError(f"Wikipedia returned HTTP {resp.status_code}")
                attempt += 1
                await self._backoff(attempt, resp.headers.get("Retry-After"))
                continue

            resp.raise_for_status()
            payload = resp.json()
            error = payload.get("error")
            if error:
                # maxlag / ratelimited are the API's way of saying "slow down".
                if error.get("code") in {"maxlag", "ratelimited"} and attempt < self.max_retries:
                    attempt += 1
                    await self._backoff(attempt, resp.headers.get("Retry-After"))
                    continue
                raise WikipediaTransientError(f"Wikipedia API error: {error.get('code')}: {error.get('info')}")
            return payload

    async def fetch_abstracts(
        self, client: httpx.AsyncClient, local_names: Iterable[str]
    ) -> Dict[str, Optional[str]]:
        """Return {local_name: abstract or None} for up to 20 graph local names.

        Every requested name is in the result. Raises WikipediaTransientError
        if Wikipedia stays unavailable, so the caller can leave these nodes
        un-hydrated and retry them later.
        """
        names: List[str] = list(dict.fromkeys(local_names))
        if len(names) > MAX_TITLES_PER_REQUEST:
            raise ValueError(f"at most {MAX_TITLES_PER_REQUEST} titles per request, got {len(names)}")
        result: Dict[str, Optional[str]] = {name: None for name in names}
        if not names:
            return result

        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "extracts|pageprops",
            "exintro": "1",
            "explaintext": "1",
            "exlimit": "max",
            "ppprop": "disambiguation",
            "redirects": "1",
            "titles": "|".join(dict.fromkeys(to_title(n) for n in names)),
        }
        pages: Dict[str, dict] = {}
        renamed: Dict[str, str] = {}
        cont: dict = {}
        # The API may split one answer over several responses ("continue").
        while True:
            payload = await self._get(client, {**params, **cont})
            query = payload.get("query", {})
            for step in query.get("normalized", []) + query.get("redirects", []):
                renamed[step["from"]] = step["to"]
            for page in query.get("pages", []):
                merged = pages.setdefault(page["title"], {})
                merged.update({k: v for k, v in page.items() if v not in (None, "")})
            if "continue" not in payload:
                break
            cont = payload["continue"]

        for name in names:
            title = to_title(name)
            seen = set()
            while title in renamed and title not in seen:  # normalized -> redirect -> ...
                seen.add(title)
                title = renamed[title]
            page = pages.get(title)
            if not page or page.get("missing") or page.get("invalid"):
                continue
            if "disambiguation" in (page.get("pageprops") or {}):
                continue
            result[name] = _clean(page.get("extract"))
        return result


wikipedia_client = WikipediaClient(
    settings.wikipedia_api_url,
    settings.wikipedia_user_agent,
    settings.wikipedia_timeout_seconds,
    min_interval=settings.wikipedia_min_interval_seconds,
    max_retries=settings.wikipedia_max_retries,
)
