"""In-process SPARQL stand-in driven by tests/test_data fixtures."""
from typing import Any

import httpx


class MockSparqlClient:
    def __init__(self, data: dict[str, Any]):
        self.data = data

    def _structure(self, local_name: str) -> dict[str, Any]:
        return self.data.get("structure", {}).get(local_name, {})

    async def ask_category_exists(self, client: httpx.AsyncClient, local_name: str) -> bool:
        return bool(self.data.get("categories", {}).get(local_name, False))

    async def fetch_subcategories(self, client: httpx.AsyncClient, local_name: str) -> list[str]:
        return list(self._structure(local_name).get("subcategories", []))

    async def fetch_member_articles(self, client: httpx.AsyncClient, local_name: str) -> list[str]:
        return list(self._structure(local_name).get("articles", []))

    async def fetch_labels(self, client: httpx.AsyncClient, local_name: str) -> list[str]:
        return list(self.data.get("labels", {}).get(local_name, []))

    async def fetch_alt_names(self, client: httpx.AsyncClient, local_name: str) -> list[str]:
        return list(self.data.get("alt_names", {}).get(local_name, []))

    async def fetch_redirect_sources(self, client: httpx.AsyncClient, local_name: str) -> list[str]:
        return list(self.data.get("redirects", {}).get(local_name, []))

    async def fetch_disambiguation_sources(self, client: httpx.AsyncClient, local_name: str) -> list[str]:
        return list(self.data.get("disambiguations", {}).get(local_name, []))
