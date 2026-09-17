"""FastAPI application: accept one raw lockbox batch and verify it.

The endpoint takes application/octet-stream (raw bytes, no form wrapper),
caps the body at 1 MiB, hands the bytes to the pure parser in
:mod:`app.parser`, and serializes whatever the parser concluded. There is
no fixed response anywhere on this path: ACCEPT/REJECT and every number
come from the uploaded payload.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.parser import MAX_BYTES, Status, parse_verification
from app.schemas import (
    DeclaredOut,
    DetailOut,
    ErrorOut,
    VerificationResponse,
)

app = FastAPI(
    title="Lockbox Batch Verification API",
    version="1.0.0",
    description="Verifies fixed-width, separator-free lockbox remittance batches.",
)


class PayloadTooLarge(Exception):
    """Raised when the request body exceeds MAX_BYTES."""


@app.exception_handler(PayloadTooLarge)
async def payload_too_large_handler(_request: Request, _exc: PayloadTooLarge) -> JSONResponse:
    # This is an edge rejection (transport-level), not a batch REJECT: the
    # batch was never fully read and cannot be verified.
    return JSONResponse(
        status_code=413,
        content={
            "detail": f"request body must not exceed {MAX_BYTES} bytes (1 MiB)"
        },
    )


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
    # Stream the body with an explicit byte ceiling rather than trusting
    # Content-Length or buffering an unbounded request.body().
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise PayloadTooLarge
        chunks.append(chunk)
    body = b"".join(chunks)

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
