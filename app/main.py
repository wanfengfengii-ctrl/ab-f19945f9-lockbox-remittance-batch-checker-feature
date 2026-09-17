"""FastAPI application: verify lockbox batches, one-shot or resumable.

Two ways in:

* ``POST /verify`` — the original one-shot path: raw bytes in, verdict
  out. Unchanged.
* ``PUT /uploads/{upload_id}`` + ``POST /uploads/{upload_id}/complete`` —
  a resumable session for flaky leased lines: the integrator picks the
  upload_id, appends chunks (Upload-Offset / Upload-Length headers),
  and completes once every declared byte is stored. Completion runs the
  same parser and freezes the same response /verify would have produced.

There is no fixed response anywhere: ACCEPT/REJECT and every number come
from the uploaded payload, whether it arrived in one shot or in chunks.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.parser import MAX_BYTES, Status, parse_verification
from app.schemas import (
    DeclaredOut,
    DetailOut,
    ErrorOut,
    UploadStateOut,
    VerificationResponse,
)
from app.store import CompleteOutcome, LengthRequired, PutConflict, PutOk, UploadStore

#: Client-chosen upload ids are path segments; keep them reasonably sized.
MAX_UPLOAD_ID_LENGTH = 128


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.store = UploadStore(os.environ.get("UPLOADS_DB_PATH", "uploads.db"))
    yield


app = FastAPI(
    title="Lockbox Batch Verification API",
    version="1.1.0",
    description="Verifies fixed-width, separator-free lockbox remittance batches.",
    lifespan=lifespan,
)


class PayloadTooLarge(Exception):
    """Raised when a request body or declared length exceeds MAX_BYTES."""

    def __init__(self, detail: str | None = None) -> None:
        self.detail = (
            detail or f"request body must not exceed {MAX_BYTES} bytes (1 MiB)"
        )


class BadRequest(Exception):
    """Raised when required upload headers are missing or malformed."""

    def __init__(self, detail: str) -> None:
        self.detail = detail


@app.exception_handler(PayloadTooLarge)
async def payload_too_large_handler(_request: Request, exc: PayloadTooLarge) -> JSONResponse:
    # This is an edge rejection (transport-level), not a batch REJECT: the
    # batch was never fully read and cannot be verified.
    return JSONResponse(status_code=413, content={"detail": exc.detail})


@app.exception_handler(BadRequest)
async def bad_request_handler(_request: Request, exc: BadRequest) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": exc.detail})


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _read_body_capped(request: Request) -> bytes:
    # Stream the body with an explicit byte ceiling rather than trusting
    # Content-Length or buffering an unbounded request.body().
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise PayloadTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _build_verification_response(body: bytes) -> VerificationResponse:
    """Turn raw batch bytes into the canonical verification response."""
    result = parse_verification(body)

    if result.status is Status.ACCEPT:
        return VerificationResponse(
            status=result.status.value,
            declared=DeclaredOut(
                batch_number=result.declared.batch_number,
                detail_count=result.declared.detail_count,
                total_amount_cents=result.declared.total_amount_cents,
            ),
            computed_detail_count=result.computed_detail_count,
            computed_total_amount_cents=result.computed_total_amount_cents,
            details=[
                DetailOut(
                    line=detail.line,
                    sequence=detail.sequence,
                    invoice=detail.invoice,
                    amount_cents=detail.amount_cents,
                )
                for detail in result.details
            ],
        )

    # Whole-batch REJECT: details are never emitted. Only a summary
    # mismatch additionally carries declared vs computed figures and the
    # two signed differences; a structural reject carries the error alone.
    response = VerificationResponse(
        status=result.status.value,
        error=ErrorOut(
            line=result.error.line,
            column=result.error.column,
            code=result.error.code.value,
            message=result.error.message,
        ),
    )
    if result.differences is not None:
        response.declared = DeclaredOut(
            batch_number=result.declared.batch_number,
            detail_count=result.declared.detail_count,
            total_amount_cents=result.declared.total_amount_cents,
        )
        response.computed_detail_count = result.computed_detail_count
        response.computed_total_amount_cents = result.computed_total_amount_cents
        response.differences = result.differences
    return response


def _frozen_verification_json(body: bytes) -> str:
    """The exact response body persisted (and replayed) for a completed upload."""
    return _build_verification_response(body).model_dump_json()


@app.post(
    "/verify",
    response_model=VerificationResponse,
    responses={413: {"description": "Payload larger than 1 MiB"}},
    tags=["verification"],
)
async def verify_batch(request: Request) -> VerificationResponse:
    return _build_verification_response(await _read_body_capped(request))


def _parse_upload_offset(request: Request) -> int:
    raw = request.headers.get("Upload-Offset")
    if raw is None or not (raw.isascii() and raw.isdigit()):
        raise BadRequest(
            "Upload-Offset header is required and must be a non-negative integer"
        )
    return int(raw)


def _parse_upload_length(request: Request) -> int | None:
    raw = request.headers.get("Upload-Length")
    if raw is None:
        return None
    if not (raw.isascii() and raw.isdigit()):
        raise BadRequest("Upload-Length must be a non-negative integer")
    total = int(raw)
    if total > MAX_BYTES:
        # The declared total obeys the same 1 MiB edge cap as /verify.
        raise PayloadTooLarge(
            f"Upload-Length must not exceed {MAX_BYTES} bytes (1 MiB)"
        )
    return total


def _conflict_response(upload_id: str, outcome: PutConflict) -> JSONResponse:
    # Every rejection carries the committed offset (also as the
    # Upload-Offset header) so the client can resume deterministically.
    return JSONResponse(
        status_code=409,
        content={
            "detail": outcome.reason,
            "upload_id": upload_id,
            "offset": outcome.offset,
            "upload_length": outcome.total_length,
            "status": outcome.status,
        },
        headers={"Upload-Offset": str(outcome.offset)},
    )


@app.put(
    "/uploads/{upload_id}",
    response_model=UploadStateOut,
    responses={
        400: {"description": "Missing or malformed Upload-Offset/Upload-Length"},
        409: {"description": "Chunk conflicts with the committed session state"},
        413: {"description": "Declared length or chunk larger than 1 MiB"},
    },
    tags=["uploads"],
)
async def put_upload_chunk(
    upload_id: str, request: Request, response: Response
) -> UploadStateOut | JSONResponse:
    if not upload_id or len(upload_id) > MAX_UPLOAD_ID_LENGTH:
        raise BadRequest(
            f"upload_id must be 1-{MAX_UPLOAD_ID_LENGTH} characters"
        )
    offset = _parse_upload_offset(request)
    total = _parse_upload_length(request)
    body = await _read_body_capped(request)

    store: UploadStore = request.app.state.store
    outcome = await run_in_threadpool(store.put_chunk, upload_id, offset, total, body)

    if isinstance(outcome, PutOk):
        response.headers["Upload-Offset"] = str(outcome.offset)
        return UploadStateOut(
            upload_id=upload_id,
            offset=outcome.offset,
            upload_length=outcome.total_length,
            status="receiving",
        )
    if isinstance(outcome, LengthRequired):
        raise BadRequest("the first chunk must declare Upload-Length")
    return _conflict_response(upload_id, outcome)


@app.post(
    "/uploads/{upload_id}/complete",
    responses={
        200: {"model": VerificationResponse},
        404: {"description": "Unknown upload_id"},
        409: {"description": "Fewer bytes received than the declared total"},
    },
    tags=["uploads"],
)
async def complete_upload(upload_id: str, request: Request) -> Response:
    store: UploadStore = request.app.state.store
    outcome: CompleteOutcome = await run_in_threadpool(
        store.complete, upload_id, _frozen_verification_json
    )

    if outcome.kind == "missing":
        return JSONResponse(
            status_code=404, content={"detail": f"unknown upload_id: {upload_id}"}
        )
    if outcome.kind == "incomplete":
        return JSONResponse(
            status_code=409,
            content={
                "detail": "upload is not fully received",
                "upload_id": upload_id,
                "offset": outcome.offset,
                "upload_length": outcome.total_length,
                "status": "receiving",
            },
            headers={"Upload-Offset": str(outcome.offset)},
        )
    # 'completed' and 'already' both return the frozen response bytes, so a
    # repeated complete is byte-identical to the first one.
    return Response(content=outcome.response_json, media_type="application/json")
