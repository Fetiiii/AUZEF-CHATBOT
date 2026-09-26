"""Versioned, read-only routes for the Solution Center integration."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from core.deps import get_db

from .auth import verify_integration_api_key
from .schemas import ChangesResponse, MetaResponse, QnAResponse
from .service import InvalidCursor, QnAIntegrationService


logger = logging.getLogger("auzef")
router = APIRouter(
    prefix="/api/integrations/v1",
    tags=["Solution Center Integration"],
    dependencies=[Depends(verify_integration_api_key)],
)


def _service(db: Session = Depends(get_db)) -> QnAIntegrationService:
    return QnAIntegrationService(db)


def _database_unavailable(endpoint: str) -> HTTPException:
    # SQLAlchemy exception strings may contain statements/parameters; log only
    # the safe endpoint/client dimensions, never the exception object.
    logger.error(
        "Integration database error endpoint=%s client=cozum_merkezi", endpoint
    )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Integration data is temporarily unavailable.",
    )


@router.get(
    "/qna",
    response_model=QnAResponse,
    summary="List the active QnA dataset",
    description=(
        "Returns active canonical QnA records from PostgreSQL using cursor "
        "pagination. The until value stays fixed across pages; use it as the "
        "since value for /qna/changes after a successful full sync."
    ),
)
def list_qna(
    limit: int = Query(default=500, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    service: QnAIntegrationService = Depends(_service),
):
    try:
        response = service.list_qna(limit=limit, cursor=cursor)
    except InvalidCursor as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _database_unavailable("qna") from exc
    logger.info(
        "Integration response endpoint=qna client=cozum_merkezi count=%d has_more=%s",
        len(response.items),
        response.pagination.has_more,
    )
    return response


@router.get(
    "/qna/changes",
    response_model=ChangesResponse,
    summary="List incremental QnA changes",
    description=(
        "Returns upserts and deletes at or after since, bounded by a "
        "server-generated until timestamp. Follow all cursor pages with the "
        "same since, then use until as the next since."
    ),
)
def list_qna_changes(
    since: datetime = Query(..., description="Timezone-aware ISO-8601 timestamp."),
    limit: int = Query(default=500, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    service: QnAIntegrationService = Depends(_service),
):
    if since.tzinfo is None or since.utcoffset() is None:
        raise HTTPException(
            status_code=422, detail="since must include a timezone offset."
        )
    try:
        response = service.list_changes(since=since, limit=limit, cursor=cursor)
    except InvalidCursor as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _database_unavailable("qna_changes") from exc
    logger.info(
        "Integration response endpoint=qna_changes client=cozum_merkezi "
        "count=%d has_more=%s",
        len(response.items),
        response.pagination.has_more,
    )
    return response


@router.get(
    "/meta",
    response_model=MetaResponse,
    summary="Inspect QnA dataset metadata",
    description=(
        "Returns the active record count and deterministic latest dataset version."
    ),
)
def get_meta(service: QnAIntegrationService = Depends(_service)):
    try:
        response = service.meta()
    except SQLAlchemyError as exc:
        raise _database_unavailable("meta") from exc
    logger.info(
        "Integration response endpoint=meta client=cozum_merkezi "
        "total_active_records=%d",
        response.total_active_records,
    )
    return response
