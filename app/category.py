"""Normalize user-supplied category URLs to DBpedia local names."""
from urllib.parse import unquote, urlparse

from fastapi import HTTPException

from .config import settings

_RESOURCE_PREFIXES = (
    "https://dbpedia.org/resource/",
    "http://dbpedia.org/resource/",
    "https://dbpedia.org/page/",
    "http://dbpedia.org/page/",
    "https://en.wikipedia.org/wiki/",
    "http://en.wikipedia.org/wiki/",
)


class CategoryUrlError(ValueError):
    """The value is not a DBpedia/Wikipedia category URL or local name."""


def parse_resource_url(value: str) -> str:
    """Strip a DBpedia/Wikipedia URL down to the resource local name."""
    raw = (value or "").strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1].strip()
    if not raw:
        raise CategoryUrlError("Resource URL is required.")

    lowered = raw.lower()
    for prefix in _RESOURCE_PREFIXES:
        if lowered.startswith(prefix):
            raw = unquote(raw[len(prefix) :])
            break
    else:
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            raise CategoryUrlError(
                f"Unsupported resource URL '{value}'. "
                "Use a DBpedia or Wikipedia URL, e.g. "
                "http://dbpedia.org/resource/Category:Food_ingredients."
            )
        raw = unquote(raw)

    raw = raw.strip().lstrip("/")
    if not raw:
        raise CategoryUrlError(f"Resource URL '{value}' is missing the resource name.")
    if raw.lower().startswith("category:"):
        name = raw.split(":", 1)[1].replace(" ", "_").lstrip("_")
        if not name:
            raise CategoryUrlError(f"Category URL '{value}' is missing the category name.")
        return f"Category:{name}"
    return raw.replace(" ", "_")


def parse_category_url(value: str) -> str:
    """
    Accept a DBpedia/Wikipedia category URL or a `Category:...` local name
    and return the graph key, e.g. `Category:Food_ingredients`.
    """
    local_name = parse_resource_url(value)
    if not local_name.startswith("Category:"):
        raise CategoryUrlError(
            f"'{value}' is not a category URL. "
            "Expected a DBpedia or Wikipedia category, e.g. "
            "http://dbpedia.org/resource/Category:Food_ingredients."
        )
    return local_name


def require_resource(value: str) -> str:
    try:
        return parse_resource_url(value)
    except CategoryUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def require_category(value: str) -> str:
    try:
        return parse_category_url(value)
    except CategoryUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def require_max_depth(max_depth: int) -> int:
    cap = settings.max_depth_hard_cap
    if max_depth > cap:
        raise HTTPException(
            status_code=400,
            detail=(
                f"max_depth cannot exceed {cap}; received {max_depth}. "
                f"Choose a value between 0 and {cap}."
            ),
        )
    return max_depth
