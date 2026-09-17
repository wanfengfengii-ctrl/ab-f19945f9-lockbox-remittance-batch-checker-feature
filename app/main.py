"""FastAPI application: verify lockbox batches, in one shot or resumably.

``POST /verify`` takes application/octet-stream (raw bytes, no form
wrapper), caps the body at 1 MiB, hands the bytes to the pure parser in
:mod:`app.parser`, and serializes whatever the parser concluded. There is
no fixed response anywhere on this path: ACCEPT/REJECT and every number
come from the uploaded payload.

Because dedicated lines drop mid-transfer, batches may also arrive as
resumable chunked uploads: ``PUT /uploads/{upload_id}`` appends one raw
chunk to a session persisted in a local SQLite file, and
``POST /uploads/{upload_id}/complete`` verifies the assembled bytes with
the very same parser and freezes the response. Chunk append/conflict rules
and the commit-order concurrency guarantee live in :mod:`app.uploads`.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from app.parser import MAX_BYTES, Status, parse_verification
from app.schemas import (
    DeclaredOut,
    DetailOut,
    ErrorOut,
    VerificationResponse,
)
from app.uploads import AppendOutcome, CompleteOutcome, UploadStore

app = FastAPI(
    title="Lockbox Batch Verification API",
    version="1.0.0",
    description="Verifies fixed-width, separator-free lockbox remittance batches.",
)

# Resumable-upload sessions live in a local SQLite file; LOCKBOX_UPLOAD_DB
# overrides the path (tests and deployments point it elsewhere).
app.state.upload_store = UploadStore(os.environ.get("LOCKBOX_UPLOAD_DB", "uploads.db"))


class PayloadTooLarge(Exception):
    """Raised when a request body or declared total exceeds MAX_BYTES."""


class InvalidUploadHeader(Exception):
    """Raised when Upload-Offset / Upload-Length are missing or malformed."""


@app.exception_handler(PayloadTooLarge)
async def payload_too_large_handler(_request: Request, exc: PayloadTooLarge) -> JSONResponse:
    # This is an edge rejection (transport-level), not a batch REJECT: the
    # batch was never fully read and cannot be verified.
    return JSONResponse(status_code=413, content={"detail": str(exc)})


@app.exception_handler(InvalidUploadHeader)
async def invalid_upload_header_handler(
    _request: Request, exc: InvalidUploadHeader
) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


async def _read_capped_body(request: Request) -> bytes:
    # Stream the body with an explicit byte ceiling rather than trusting
    # Content-Length or buffering an unbounded request.body().
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise PayloadTooLarge(f"request body must not exceed {MAX_BYTES} bytes (1 MiB)")
        chunks.append(chunk)
    return b"".join(chunks)


def _verification_response(body: bytes) -> VerificationResponse:
    """Serialize the parser's verdict; shared by /verify and upload completion."""
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


def _freeze_verification(body: bytes) -> str:
    """Render the full verification response as storable JSON text."""
    return _verification_response(body).model_dump_json()


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/verify",
    response_model=VerificationResponse,
    responses={413: {"description": "Payload larger than 1 MiB"}},
    tags=["verification"],
)
async def verify_batch(request: Request) -> VerificationResponse:
    body = await _read_capped_body(request)
    return _verification_response(body)


def _parse_upload_offset(request: Request) -> int:
    raw = request.headers.get("Upload-Offset")
    if raw is None:
        raise InvalidUploadHeader("Upload-Offset header is required")
    try:
        value = int(raw)
    except ValueError:
        raise InvalidUploadHeader("Upload-Offset must be a non-negative integer") from None
    if value < 0:
        raise InvalidUploadHeader("Upload-Offset must be a non-negative integer")
    return value


def _parse_upload_length(request: Request) -> int | None:
    # Absent on later chunks (the declared total is reused); required only
    # when a session is created, which the store enforces.
    raw = request.headers.get("Upload-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise InvalidUploadHeader("Upload-Length must be a non-negative integer") from None
    if value < 0:
        raise InvalidUploadHeader("Upload-Length must be a non-negative integer")
    if value > MAX_BYTES:
        raise PayloadTooLarge(
            f"declared Upload-Length must not exceed {MAX_BYTES} bytes (1 MiB)"
        )
    return value


@app.put(
    "/uploads/{upload_id}",
    responses={
        204: {"description": "Chunk accepted (or byte-identical retry)"},
        400: {"description": "Missing/malformed Upload-Offset or Upload-Length"},
        404: {"description": "Unknown upload id for a non-zero offset"},
        409: {"description": "Offset/length/content conflict; current offset returned"},
        413: {"description": "Declared total or body larger than 1 MiB"},
    },
    tags=["uploads"],
)
async def upload_chunk(upload_id: str, request: Request) -> Response:
    offset = _parse_upload_offset(request)
    upload_length = _parse_upload_length(request)
    body = await _read_capped_body(request)

    store: UploadStore = request.app.state.upload_store
    result = await run_in_threadpool(store.append, upload_id, offset, upload_length, body)

    if result.outcome is AppendOutcome.NOT_FOUND:
        return JSONResponse(
            status_code=404,
            content={"detail": "unknown upload id; the first chunk must use Upload-Offset 0"},
        )
    if result.outcome is AppendOutcome.MISSING_LENGTH:
        return JSONResponse(
            status_code=400,
            content={"detail": "the first chunk must declare Upload-Length"},
        )
    if result.outcome is AppendOutcome.CONFLICT:
        # Rejections never mutate the session and always report the tail.
        return JSONResponse(
            status_code=409,
            content={"detail": result.reason},
            headers={"Upload-Offset": str(result.received)},
        )
    return Response(status_code=204, headers={"Upload-Offset": str(result.received)})


@app.post(
    "/uploads/{upload_id}/complete",
    responses={
        200: {"description": "Verification response for the assembled batch"},
        404: {"description": "Unknown upload id"},
        409: {"description": "Not all declared bytes received yet"},
    },
    tags=["uploads"],
)
async def complete_upload(upload_id: str, request: Request) -> Response:
    store: UploadStore = request.app.state.upload_store
    result = await run_in_threadpool(store.complete, upload_id, _freeze_verification)

    if result.outcome is CompleteOutcome.NOT_FOUND:
        return JSONResponse(status_code=404, content={"detail": "unknown upload id"})
    if result.outcome is CompleteOutcome.INCOMPLETE:
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"received {result.received} of {result.upload_length} bytes"
            },
            headers={"Upload-Offset": str(result.received)},
        )
    # Freshly completed or replayed: the frozen response is served as stored,
    # so repeated completions are byte-identical.
    return Response(content=result.response_json, media_type="application/json")
