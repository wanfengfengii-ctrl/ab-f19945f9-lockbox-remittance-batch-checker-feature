"""End-to-end HTTP tests for /verify.

In-process by default; set BASE_URL to run the same cases against a live
server (this is what the one-shot compose `verify` service does).
"""

from __future__ import annotations

from app.parser import MAX_BYTES
from tests.builders import detail, good_batch, header, batch


def POST(client, payload: bytes):
    return client.post(
        "/verify",
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    )


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_accept_full_response_shape(client):
    response = POST(client, good_batch(count=2))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ACCEPT"
    assert body["declared"] == {
        "batch_number": "00000001",
        "detail_count": 2,
        "total_amount_cents": 3000,
    }
    assert body["computed_detail_count"] == 2
    assert body["computed_total_amount_cents"] == 3000
    assert body["differences"] is None
    assert body["error"] is None
    assert body["details"] == [
        {"line": 2, "sequence": 1, "invoice": "INV1", "amount_cents": 1000},
        {"line": 3, "sequence": 2, "invoice": "INV2", "amount_cents": 2000},
    ]


def test_structural_reject_is_whole_batch(client):
    # First line has a bad record type; no declared/computed figures or
    # details may leak out on a structural reject.
    response = POST(client, b"X" + b"0" * 24 + b"\n")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "REJECT"
    assert body["error"] == {
        "line": 1,
        "column": 1,
        "code": "bad_record_type",
        "message": "line 1 must be an H header record",
    }
    assert body["declared"] is None
    assert body["computed_detail_count"] is None
    assert body["computed_total_amount_cents"] is None
    assert body["details"] == []
    assert body["differences"] is None


def test_summary_mismatch_carries_differences(client):
    payload = batch([detail(1, "A", 1)], head=header(count=2, total=99))
    body = POST(client, payload).json()
    assert body["status"] == "REJECT"
    assert body["error"]["code"] == "summary_mismatch"
    assert body["error"]["line"] == 1
    assert body["error"]["column"] == 10
    assert body["differences"] == {"detail_count": -1, "total_amount_cents": -98}
    assert body["declared"]["detail_count"] == 2
    assert body["computed_detail_count"] == 1
    assert body["computed_total_amount_cents"] == 1
    # A summary reject still exposes no detail rows.
    assert body["details"] == []


def test_non_ascii_rejected(client):
    bad = b"H" + b"00000001" + b"0001" + b"00000000000" + b"\xe9\n"
    body = POST(client, bad).json()
    assert body["status"] == "REJECT"
    assert body["error"]["code"] == "non_ascii"
    assert (body["error"]["line"], body["error"]["column"]) == (1, 25)


def test_empty_body_rejected_not_500(client):
    body = POST(client, b"").json()
    assert body["status"] == "REJECT"
    assert body["error"]["code"] == "empty_line"


def test_largest_valid_batch_accepted_and_any_giant_body_413(client):
    # The 4-digit declared count caps a well-formed batch at 9999 details
    # (~280 KiB with LF), so a valid batch can never approach 1 MiB; the
    # byte cap is a pure transport guard.
    n = 9999
    rows = [detail(i, f"IN{i:07d}", 1) for i in range(1, n + 1)]
    payload = batch(rows, head=header(count=n, total=n))
    assert len(payload) == 26 + 28 * n < MAX_BYTES
    assert POST(client, payload).json()["status"] == "ACCEPT"

    oversized = b"0" * (MAX_BYTES + 1)
    assert len(oversized) == MAX_BYTES + 1
    response = POST(client, oversized)
    assert response.status_code == 413
    assert "1 MiB" in response.json()["detail"]


def test_payload_one_byte_over_limit_returns_413(client):
    response = POST(client, b"0" * (MAX_BYTES + 1))
    assert response.status_code == 413
    assert "1 MiB" in response.json()["detail"]


def test_crlf_batch_accepted(client):
    assert POST(client, good_batch(crlf=True)).json()["status"] == "ACCEPT"


def test_no_trailing_newline_accepted(client):
    assert POST(client, good_batch(final_newline=False)).json()["status"] == "ACCEPT"


def test_duplicate_header_rejected(client):
    payload = good_batch(count=1) + b"H" + b"0" * 24 + b"\n"
    body = POST(client, payload).json()
    assert body["status"] == "REJECT"
    assert body["error"]["code"] == "duplicate_header"
    assert body["error"]["line"] == 3


def test_extra_space_misalignment_can_never_accept_wrong_amount(client):
    # The classic lockbox failure: one extra space in the invoice pushes
    # the amount one byte to the right, making the record 28 bytes. With
    # column-ordered validation the shifted space is hit at amount column
    # 16 (before the surplus byte at column 28), so the batch is rejected
    # and the amount can never be parsed from shifted columns.
    field = b"INV1 " + b" " * 6             # 11 bytes: invoice + one stray space
    bad = b"D" + b"0001" + field + b"000000001000"  # 28 bytes total
    assert len(bad) == 28
    body = POST(client, header(count=1, total=1000) + b"\n" + bad + b"\n").json()
    assert body["status"] == "REJECT"
    assert (body["error"]["line"], body["error"]["column"]) == (2, 16)
    assert body["error"]["code"] == "bad_amount"
    assert body["details"] == []
