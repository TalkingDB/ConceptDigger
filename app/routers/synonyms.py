"""
GET /api/v1/exports lists export files, newest first.

POST /api/v1/synonyms/load indexes one of those files into Elasticsearch.
The file_name field is an enum in the OpenAPI schema, so Swagger renders it
as a dropdown of the same newest-first list. Loading appends: synonyms already
in the index are kept. The response says whether every synonym from the file
is searchable, or gives the Elasticsearch failure when the cluster cannot be
reached or rejects the load.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..export import list_export_files
from ..synonym_index import (
    ExportFileNotFound,
    InvalidExportFile,
    index_synonyms,
    match_sentence,
    read_export_documents,
    resolve_export_file,
)

router = APIRouter(prefix="/api/v1", tags=["synonyms"])

LOAD_SCHEMA_NAME = "LoadSynonymsRequest"


class LoadSynonymsRequest(BaseModel):
    file_name: str = Field(
        ...,
        description="Export file to load. Swagger lists these newest first.",
    )
    
class MatchSentenceRequest(BaseModel):
    sentence: str = Field(..., description="Sentence to scan for known entity synonyms.")
    fuzzy: bool = Field(
        False,
        description="When true, allow small typos (Elasticsearch AUTO edit distance). Exact phrase match when false.",
    )


class MatchedSurface(BaseModel):
    text: str
    how_this_record: str | None = None
    start: int
    end: int
    matched_text: str | None = None


class MatchedEntity(BaseModel):
    entity: str | None = None
    entity_url: str | None = None
    seed_category: str | None = None
    surface_text: list[MatchedSurface]


class MatchSentenceResult(BaseModel):
    sentence: str
    index: str
    elasticsearch_available: bool
    fuzzy: bool = False
    matches: list[MatchedEntity]
    detail: str | None = None


class LoadSynonymsResult(BaseModel):
    file_name: str
    index: str
    elasticsearch_available: bool
    ready_to_query: bool
    documents_indexed: int
    synonyms_indexed: int
    synonyms_searchable: int
    detail: str


def patch_openapi_file_enum(schema: dict, names: list[str]) -> None:
    """Put the current export names on file_name so Swagger shows a dropdown."""
    try:
        prop = schema["components"]["schemas"][LOAD_SCHEMA_NAME]["properties"]["file_name"]
    except KeyError:
        return
    if not names:
        prop["description"] = (
            "Export file to load. No exports are on disk yet; "
            "run POST /api/v1/export or POST /api/v1/dig first."
        )
        return
    prop["enum"] = names
    prop["description"] = "Export file to load. Listed newest first."


@router.get("/exports")
async def list_exports(request: Request):
    """Export file names, newest first."""
    return {"files": list_export_files(request.app.state.store)}


@router.post(
    "/synonyms/load",
    response_model=LoadSynonymsResult,
    responses={
        502: {"model": LoadSynonymsResult},
        503: {"model": LoadSynonymsResult},
    },
)
async def load_synonyms(body: LoadSynonymsRequest, request: Request):
    """Index one export into Elasticsearch and report whether every synonym is searchable.

    Synonyms already in the index are kept. `ready_to_query` is true only after
    the index refresh and a search that returns every surface form from the file.
    If Elasticsearch cannot be reached, the body has `elasticsearch_available: false`
    and `detail` with the failure.
    """
    store = request.app.state.store
    try:
        path = resolve_export_file(store, body.file_name)
        documents = read_export_documents(path)
    except InvalidExportFile as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ExportFileNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    status_code, payload = await index_synonyms(
        request.app.state.http_client, body.file_name, documents,
    )
    return JSONResponse(status_code=status_code, content=payload)


@router.post(
    "/synonyms/match",
    response_model=MatchSentenceResult,
    responses={
        502: {"model": MatchSentenceResult},
        503: {"model": MatchSentenceResult},
    },
)
async def match_synonyms(body: MatchSentenceRequest, request: Request):
    """Return entities whose indexed synonyms appear in the sentence.

    Leave `fuzzy` false for an exact phrase. Set `fuzzy` true to allow small
    typos such as "ben oill" for "ben oil". A shorter word inside another word
    still does not match. If Elasticsearch cannot be reached, the body has
    `elasticsearch_available: false` and `detail` with the failure.
    """
    sentence = body.sentence.strip()
    if not sentence:
        raise HTTPException(status_code=400, detail="sentence is empty")
    status_code, payload = await match_sentence(
        request.app.state.http_client, sentence, fuzzy=body.fuzzy,
    )
    return JSONResponse(status_code=status_code, content=payload)
