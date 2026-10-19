from fastapi import HTTPException

from app.category import (
    CategoryUrlError,
    parse_category_url,
    parse_resource_url,
    require_max_depth,
)
from app.config import settings


def test_parse_category_url_from_dbpedia_and_wikipedia():
    expected = "Category:Food_ingredients"
    assert parse_category_url("http://dbpedia.org/resource/Category:Food_ingredients") == expected
    assert parse_category_url("https://en.wikipedia.org/wiki/Category:Food_ingredients") == expected
    assert parse_category_url("Category:Food ingredients") == expected


def test_parse_category_url_rejects_plain_subject():
    try:
        parse_category_url("Food ingredients")
    except CategoryUrlError as exc:
        assert "not a category URL" in str(exc)
    else:
        raise AssertionError("expected CategoryUrlError")


def test_parse_resource_url_keeps_articles():
    assert parse_resource_url("http://dbpedia.org/resource/Chicken") == "Chicken"


def test_require_max_depth_rejects_values_above_cap():
    try:
        require_max_depth(settings.max_depth_hard_cap + 1)
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "max_depth cannot exceed" in str(exc.detail)
        assert str(settings.max_depth_hard_cap) in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_require_max_depth_allows_cap():
    assert require_max_depth(settings.max_depth_hard_cap) == settings.max_depth_hard_cap
