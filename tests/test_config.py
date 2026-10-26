"""Empty env vars must fall back to documented defaults instead of crashing."""
from app.config import Settings, _env_float, _env_int, _env_str

EMPTY_ENV = [
    "SPARQL_ENDPOINT",
    "MAX_DEPTH_HARD_CAP",
    "SPARQL_TIMEOUT_SECONDS",
    "SPARQL_MIN_INTERVAL_SECONDS",
    "SPARQL_MAX_RETRIES",
    "CACHE_FILE_PATH",
    "EXPORT_MAX_FILES",
    "ELASTICSEARCH_URL",
    "ELASTICSEARCH_INDEX",
    "PORT",
]


def test_empty_env_values_use_defaults(monkeypatch):
    for name in EMPTY_ENV:
        monkeypatch.setenv(name, "")
    cfg = Settings()
    assert cfg.sparql_endpoint == "https://dbpedia.org/sparql"
    assert cfg.max_depth_hard_cap == 6
    assert cfg.sparql_timeout_seconds == 20.0
    assert cfg.sparql_min_interval_seconds == 0.5
    assert cfg.sparql_max_retries == 3
    assert cfg.cache_file_path == "data/graph_cache.json"
    assert cfg.export_max_files == 50
    assert cfg.elasticsearch_url == "http://localhost:9200"
    assert cfg.elasticsearch_index == "train-synonyms"
    assert cfg.port == 5007


def test_whitespace_env_values_use_defaults(monkeypatch):
    monkeypatch.setenv("EXPORT_MAX_FILES", "  ")
    monkeypatch.setenv("MAX_DEPTH_HARD_CAP", "\t")
    assert _env_int("EXPORT_MAX_FILES", 50) == 50
    assert _env_int("MAX_DEPTH_HARD_CAP", 6) == 6
    assert _env_str("EXPORT_MAX_FILES", "50") == "50"
    assert _env_float("SPARQL_MIN_INTERVAL_SECONDS", 0.5) == 0.5


def test_zero_export_max_files_is_kept(monkeypatch):
    monkeypatch.setenv("EXPORT_MAX_FILES", "0")
    assert Settings().export_max_files == 0
