"""
Indexes one export file into Elasticsearch without removing synonyms already stored.

Each document id is the export file plus the entity, so loading another file
appends and loading the same file again replaces only that file's documents.
After the bulk index, the index is refreshed and searched. ready_to_query is
true only when every surface form from the file comes back in that search.
"""
import hashlib
import json
import os
from typing import Any, Dict, List, Tuple

import httpx

from .config import settings
from .export import export_dir
from .graph_store import GraphStore

_REQUIRED = ("entity_url", "entity", "surface_text", "seed_category")
_BULK_CHUNK = 500
_CONNECT_TIMEOUT = 3.0
_TIMEOUT = httpx.Timeout(20.0, connect=_CONNECT_TIMEOUT)
_BULK_TIMEOUT = httpx.Timeout(60.0, connect=_CONNECT_TIMEOUT)

_INDEX_BODY = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "entity_url": {"type": "keyword"},
            "entity": {"type": "keyword"},
            "seed_category": {"type": "keyword"},
            "source_file": {"type": "keyword"},
            "surface_text": {
                "properties": {
                    "text": {"type": "text"},
                    "how_this_record": {"type": "keyword"},
                }
            },
        }
    },
}


class InvalidExportFile(ValueError):
    pass


class ExportFileNotFound(FileNotFoundError):
    pass


class ElasticsearchUnavailable(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class ElasticsearchFailure(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def resolve_export_file(store: GraphStore, file_name: str) -> str:
    """Return the path of one export file. Rejects paths outside the export folder."""
    if (
        not file_name
        or file_name != os.path.basename(file_name)
        or not file_name.endswith(".json")
    ):
        raise InvalidExportFile("file_name must be an export file name, not a path")
    directory = os.path.realpath(export_dir(store))
    path = os.path.realpath(os.path.join(directory, file_name))
    try:
        inside = os.path.commonpath([directory, path]) == directory
    except ValueError:
        inside = False
    if not inside:
        raise InvalidExportFile("file_name must be an export file name, not a path")
    if not os.path.isfile(path):
        raise ExportFileNotFound(f"Export file not found: {file_name}")
    return path


def read_export_documents(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidExportFile(f"Export file is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise InvalidExportFile("Export file must be a JSON array of synonym documents")
    documents = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict) or any(key not in item for key in _REQUIRED):
            raise InvalidExportFile(
                f"Export document {index} is missing entity_url, entity, surface_text, or seed_category"
            )
        forms = item["surface_text"]
        if not isinstance(forms, list) or any(not _valid_form(form) for form in forms):
            raise InvalidExportFile(f"Export document {index} has an invalid surface_text list")
        documents.append({key: item[key] for key in _REQUIRED})
    return documents


def _valid_form(form: Any) -> bool:
    return (
        isinstance(form, dict)
        and isinstance(form.get("text"), str)
        and form["text"].strip() != ""
        and isinstance(form.get("how_this_record"), str)
    )


def document_id(file_name: str, document: Dict[str, Any]) -> str:
    raw = f"{file_name}\n{document['entity_url']}\n{document['seed_category']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _synonym_texts(document: Dict[str, Any]) -> List[str]:
    return [form["text"] for form in document.get("surface_text") or []]


def _base_url() -> str:
    return settings.elasticsearch_url.rstrip("/")


def _index_url() -> str:
    return f"{_base_url()}/{settings.elasticsearch_index}"


async def index_synonyms(
    client: httpx.AsyncClient,
    file_name: str,
    documents: List[Dict[str, Any]],
) -> Tuple[int, Dict[str, Any]]:
    """Append documents, refresh, and search them back. Returns (status, body)."""
    index = settings.elasticsearch_index
    to_index = [doc for doc in documents if _synonym_texts(doc)]
    synonyms_indexed = sum(len(_synonym_texts(doc)) for doc in to_index)
    body = {
        "file_name": file_name,
        "index": index,
        "elasticsearch_available": True,
        "ready_to_query": False,
        "documents_indexed": 0,
        "synonyms_indexed": synonyms_indexed,
        "synonyms_searchable": 0,
        "detail": "",
    }
    try:
        if not to_index:
            await _request(client, "GET", f"{_base_url()}/")
            body["ready_to_query"] = True
            body["detail"] = (
                "This export has no synonyms to index. "
                "Synonyms already in Elasticsearch were left in place."
            )
            return 200, body

        await _ensure_index(client)
        indexed_ids = await _bulk_index(client, file_name, to_index)
        body["documents_indexed"] = len(indexed_ids)
        await _request(client, "POST", f"{_index_url()}/_refresh")
        found = await _search_ids(client, indexed_ids)
    except ElasticsearchUnavailable as exc:
        body["elasticsearch_available"] = False
        body["ready_to_query"] = False
        body["detail"] = exc.detail
        return 503, body
    except ElasticsearchFailure as exc:
        body["ready_to_query"] = False
        body["detail"] = exc.detail
        return 502, body

    searchable, missing = _count_searchable(to_index, indexed_ids, found)
    body["synonyms_searchable"] = searchable
    if missing == 0 and len(found) == len(indexed_ids):
        body["ready_to_query"] = True
        body["detail"] = "All synonyms are indexed and ready to query."
        return 200, body

    body["ready_to_query"] = False
    pending = "1 is not ready to query." if missing == 1 else f"{missing} are not ready to query."
    body["detail"] = f"{searchable} of {synonyms_indexed} synonyms are searchable. {pending}"
    return 502, body


def _count_searchable(
    documents: List[Dict[str, Any]],
    ids: List[str],
    found: Dict[str, Dict[str, Any]],
) -> Tuple[int, int]:
    searchable = 0
    missing = 0
    for document, doc_id in zip(documents, ids):
        stored = set(_synonym_texts(found.get(doc_id) or {}))
        for text in _synonym_texts(document):
            if text in stored:
                searchable += 1
            else:
                missing += 1
    return searchable, missing


async def _ensure_index(client: httpx.AsyncClient) -> None:
    exists = await _request(client, "HEAD", _index_url())
    if exists.status_code == 200:
        return
    if exists.status_code != 404:
        _raise_for_status(exists, "checking the synonym index")
    created = await _request(client, "PUT", _index_url(), json=_INDEX_BODY)
    if created.status_code in (200, 201):
        return
    if created.status_code == 400 and "resource_already_exists" in created.text:
        return
    _raise_for_status(created, "creating the synonym index")


async def _bulk_index(
    client: httpx.AsyncClient,
    file_name: str,
    documents: List[Dict[str, Any]],
) -> List[str]:
    ids = [document_id(file_name, document) for document in documents]
    for start in range(0, len(documents), _BULK_CHUNK):
        chunk_docs = documents[start:start + _BULK_CHUNK]
        chunk_ids = ids[start:start + _BULK_CHUNK]
        lines = []
        for doc_id, document in zip(chunk_ids, chunk_docs):
            lines.append(json.dumps({"index": {"_index": settings.elasticsearch_index, "_id": doc_id}}))
            stored = dict(document)
            stored["source_file"] = file_name
            lines.append(json.dumps(stored, ensure_ascii=False))
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        response = await _request(
            client,
            "POST",
            f"{_base_url()}/_bulk",
            content=payload,
            headers={"Content-Type": "application/x-ndjson"},
            timeout=_BULK_TIMEOUT,
        )
        _raise_for_status(response, "indexing synonyms")
        report = response.json()
        if report.get("errors"):
            reason = _first_bulk_error(report)
            raise ElasticsearchFailure(f"Elasticsearch rejected one or more synonyms: {reason}")
    return ids


async def _search_ids(client: httpx.AsyncClient, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(ids), _BULK_CHUNK):
        chunk = ids[start:start + _BULK_CHUNK]
        response = await _request(
            client,
            "POST",
            f"{_index_url()}/_search",
            json={
                "size": len(chunk),
                "query": {"ids": {"values": chunk}},
                "_source": ["surface_text"],
            },
        )
        _raise_for_status(response, "searching indexed synonyms")
        for hit in response.json().get("hits", {}).get("hits", []):
            found[hit["_id"]] = hit.get("_source") or {}
    return found


async def _request(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    kwargs.setdefault("timeout", _TIMEOUT)
    try:
        return await client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise ElasticsearchUnavailable(
            f"Elasticsearch is not available at {settings.elasticsearch_url}: timed out ({exc})"
        ) from exc
    except httpx.TransportError as exc:
        raise ElasticsearchUnavailable(
            f"Elasticsearch is not available at {settings.elasticsearch_url}: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc


def _raise_for_status(response: httpx.Response, action: str) -> None:
    if response.status_code < 400:
        return
    snippet = response.text.strip().replace("\n", " ")
    if len(snippet) > 500:
        snippet = snippet[:500] + "..."
    message = (
        f"Elasticsearch failed while {action} "
        f"({response.status_code} from {settings.elasticsearch_url}): {snippet}"
    )
    if response.status_code in (502, 503, 504):
        raise ElasticsearchUnavailable(message)
    raise ElasticsearchFailure(message)


def _first_bulk_error(report: Dict[str, Any]) -> str:
    for item in report.get("items") or []:
        details = next(iter(item.values()), {})
        error = details.get("error")
        if not error:
            continue
        if isinstance(error, dict):
            return error.get("reason") or error.get("type") or str(error)
        return str(error)
    return "bulk response reported errors"
