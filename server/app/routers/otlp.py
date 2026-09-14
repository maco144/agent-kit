"""POST /v1/traces — OTLP/HTTP trace ingest for GenAI spans."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_org
from app.database import get_db
from app.models import Organization
from app.otlp.assembler import assemble
from app.otlp.decode import JSON, PROTOBUF, DecodeError, decode_request, encode_response

router = APIRouter(tags=["otlp"])


@router.post("/v1/traces")
async def export_traces(
    request: Request,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Accept an OTLP/HTTP trace export (protobuf or JSON, optionally gzip).

    GenAI spans (OpenTelemetry GenAI semantic conventions or OpenInference) become
    agent-kit runs; other spans are accepted and ignored.
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in (PROTOBUF, JSON):
        return Response(status_code=415)

    try:
        batch = decode_request(
            await request.body(), content_type, request.headers.get("content-encoding", "")
        )
    except DecodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        await assemble(batch.spans, org.id, db)
        await db.commit()
    except IntegrityError:
        # Another request extended the same run concurrently; exporters retry 5xx.
        await db.rollback()
        return Response(status_code=503, headers={"Retry-After": "1"})

    return Response(
        content=encode_response(content_type, batch.rejected, batch.error_message),
        media_type=content_type,
    )
