"""End-to-end tests for the resumable upload sessions.

Covers the acceptance scenarios: interrupted uploads resumed from the
returned offset, byte-identical chunk retries, concurrent writers/completers
decided by commit order without polluting stored content, completion being
refused until every declared byte is in, and completed uploads — legal or
illegal batches — producing exactly the /verify verdict for those bytes.

Runs in-process by default; with BASE_URL set the same cases run as
black-box HTTP checks against a live API (upload ids are random, so the
suite is safe to repeat against a persistent database).
"""

from __future__ import annotations

import threading
import uuid

import pytest

from app.parser import MAX_BYTES
from tests.builders import batch, detail, good_batch, header

_UNSET = object()


def _uid() -> str:
    return uuid.uuid4().hex


def _put(client, uid: str, data: bytes, offset: int, length=_UNSET):
    headers = {
        "Content-Type": "application/octet-stream",
        "Upload-Offset": str(offset),
    }
    if length is not _UNSET:
        headers["Upload-Length"] = str(length)
    return client.put(f"/uploads/{uid}", content=data, headers=headers)


def _complete(client, uid: str):
    return client.post(f"/uploads/{uid}/complete")


def _verify(client, payload: bytes):
    return client.post(
        "/verify",
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    )


def _concurrent(functions):
    """Run callables in barrier-synced threads; return their results in order."""
    barrier = threading.Barrier(len(functions))
    outcomes = [None] * len(functions)

    def run(index, fn):
        barrier.wait(timeout=10)
        outcomes[index] = fn()

    threads = [
        threading.Thread(target=run, args=(index, fn))
        for index, fn in enumerate(functions)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    for thread in threads:
        assert not thread.is_alive(), "a concurrent request never finished"
    return outcomes


# ---------------------------------------------------------------------------
# Happy path: chunked upload, interruption, resume, completion
# ---------------------------------------------------------------------------


def test_interrupted_upload_resumes_from_returned_offset(client):
    payload = good_batch(count=6)
    uid = _uid()
    # The link "drops" between chunks; the client always resumes from the
    # last offset the server confirmed, and only the first chunk declares
    # the total.
    chunks = [payload[:17], payload[17:60], payload[60:61], payload[61:]]
    offset = 0
    for index, chunk in enumerate(chunks):
        length = len(payload) if index == 0 else _UNSET
        response = _put(client, uid, chunk, offset, length)
        assert response.status_code == 200
        assert response.json()["status"] == "receiving"
        assert response.json()["upload_length"] == len(payload)
        offset = int(response.headers["Upload-Offset"])
        assert response.json()["offset"] == offset
    assert offset == len(payload)

    done = _complete(client, uid)
    assert done.status_code == 200
    assert done.json()["status"] == "ACCEPT"
    assert done.json()["computed_detail_count"] == 6
    assert done.json() == _verify(client, payload).json()


def test_identical_chunk_retry_is_idempotent(client):
    payload = good_batch(count=3)
    uid = _uid()
    first, second = payload[:40], payload[40:]

    created = _put(client, uid, first, 0, len(payload))
    assert created.status_code == 200
    assert created.headers["Upload-Offset"] == "40"

    # The client never saw the 200 and retries the same chunk: success,
    # nothing appended, the confirmed offset is reported again.
    replay = _put(client, uid, first, 0, len(payload))
    assert replay.status_code == 200
    assert replay.headers["Upload-Offset"] == "40"

    assert _put(client, uid, second, 40).status_code == 200

    # After a reconnect the client replays both committed chunks; each is
    # fully contained in the stored range and byte-identical.
    assert _put(client, uid, first, 0).status_code == 200
    replay_tail = _put(client, uid, second, 40)
    assert replay_tail.status_code == 200
    assert replay_tail.headers["Upload-Offset"] == str(len(payload))

    done = _complete(client, uid)
    assert done.status_code == 200
    assert done.json() == _verify(client, payload).json()


# ---------------------------------------------------------------------------
# Conflicts: 409 with the committed offset, stored bytes untouched
# ---------------------------------------------------------------------------


def test_conflicting_and_out_of_bounds_chunks_leave_data_untouched(client):
    payload = good_batch(count=2)
    uid = _uid()
    assert _put(client, uid, payload[:30], 0, len(payload)).status_code == 200

    # Same window, different bytes -> content conflict.
    conflict = _put(client, uid, b"X" * 30, 0)
    assert conflict.status_code == 409
    assert conflict.json()["offset"] == 30
    assert conflict.headers["Upload-Offset"] == "30"

    # Starts inside the stored range but crosses the tail.
    crossing = _put(client, uid, payload[20:40], 20)
    assert crossing.status_code == 409
    assert crossing.json()["offset"] == 30

    # Beyond the tail (gap).
    gap = _put(client, uid, payload[31:40], 31)
    assert gap.status_code == 409
    assert gap.json()["offset"] == 30

    # A changed total is refused even at the correct offset.
    changed_total = _put(client, uid, payload[30:], 30, len(payload) + 1)
    assert changed_total.status_code == 409
    assert changed_total.json()["offset"] == 30

    # Appending past the declared total is out of bounds.
    overshoot = _put(client, uid, payload[30:] + b"ZZ", 30, len(payload))
    assert overshoot.status_code == 409
    assert overshoot.json()["offset"] == 30

    # None of the rejected bytes landed: the original chunks still complete
    # into exactly the /verify verdict for the untouched payload.
    resumed = _put(client, uid, payload[30:], 30, len(payload))
    assert resumed.status_code == 200
    assert resumed.headers["Upload-Offset"] == str(len(payload))
    done = _complete(client, uid)
    assert done.status_code == 200
    assert done.json() == _verify(client, payload).json()


def test_first_chunk_exceeding_declared_total_creates_nothing(client):
    payload = good_batch(count=1)
    uid = _uid()
    too_much = _put(client, uid, payload + b"X", 0, len(payload))
    assert too_much.status_code == 409
    assert too_much.json()["offset"] == 0
    assert too_much.headers["Upload-Offset"] == "0"
    # No session was created.
    assert _complete(client, uid).status_code == 404


def test_first_chunk_must_start_at_offset_zero(client):
    response = _put(client, _uid(), b"abc", 5, 3)
    assert response.status_code == 409
    assert response.json()["offset"] == 0
    assert response.headers["Upload-Offset"] == "0"


# ---------------------------------------------------------------------------
# Completion rules
# ---------------------------------------------------------------------------


def test_complete_rejected_until_fully_received(client):
    payload = good_batch(count=2)
    uid = _uid()
    assert _put(client, uid, payload[:10], 0, len(payload)).status_code == 200

    early = _complete(client, uid)
    assert early.status_code == 409
    assert early.json()["offset"] == 10
    assert early.headers["Upload-Offset"] == "10"

    assert _put(client, uid, payload[10:], 10).status_code == 200
    done = _complete(client, uid)
    assert done.status_code == 200
    assert done.json()["status"] == "ACCEPT"


def test_complete_is_atomic_frozen_and_repeatable(client):
    payload = good_batch(count=2)
    uid = _uid()
    assert _put(client, uid, payload, 0, len(payload)).status_code == 200

    first = _complete(client, uid)
    assert first.status_code == 200

    # Writes after completion are refused with the committed offset.
    late = _put(client, uid, payload[:10], 0, len(payload))
    assert late.status_code == 409
    assert late.json()["status"] == "completed"
    assert late.headers["Upload-Offset"] == str(len(payload))

    # Repeating complete returns the very same frozen response.
    second = _complete(client, uid)
    assert second.status_code == 200
    assert second.content == first.content


def test_complete_unknown_upload_is_404(client):
    response = _complete(client, _uid())
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Concurrency: commit order decides, losers get deterministic feedback
# ---------------------------------------------------------------------------


def _twin_batches() -> tuple[bytes, bytes]:
    """Two valid batches of identical length differing only in content."""
    pairs = [("INV1", 1000), ("INV2", 2000)]
    rows = [detail(1, "INV1", 1000), detail(2, "INV2", 2000)]
    payload_a = batch(rows, head=header(batch="00000001", details=pairs))
    payload_b = batch(rows, head=header(batch="00000002", details=pairs))
    assert len(payload_a) == len(payload_b)
    assert payload_a != payload_b
    return payload_a, payload_b


def test_concurrent_first_chunks_single_winner(client):
    payload_a, payload_b = _twin_batches()
    uid = _uid()
    response_a, response_b = _concurrent(
        [
            lambda: _put(client, uid, payload_a, 0, len(payload_a)),
            lambda: _put(client, uid, payload_b, 0, len(payload_b)),
        ]
    )
    assert sorted(r.status_code for r in (response_a, response_b)) == [200, 409]
    winner = payload_a if response_a.status_code == 200 else payload_b
    loser = response_b if response_a.status_code == 200 else response_a
    # The loser learns the committed offset; the stored content is exactly
    # the winner's batch.
    assert loser.json()["offset"] == len(winner)
    assert loser.headers["Upload-Offset"] == str(len(winner))
    done = _complete(client, uid)
    assert done.status_code == 200
    assert done.json() == _verify(client, winner).json()


def test_concurrent_appends_single_winner_content_untouched(client):
    pairs = [("INV7", 777)]
    rows = [detail(1, "INV7", 777)]
    head_a = header(batch="00000011", details=pairs) + b"\n"
    head_b = header(batch="00000022", details=pairs) + b"\n"
    rest = b"".join(rows) + b"\n"
    payload_a, payload_b = head_a + rest, head_b + rest
    assert len(head_a) == len(head_b) == 26

    uid = _uid()
    first = _put(client, uid, b"H", 0, len(payload_a))
    assert first.status_code == 200
    assert first.headers["Upload-Offset"] == "1"

    response_a, response_b = _concurrent(
        [
            lambda: _put(client, uid, head_a[1:], 1),
            lambda: _put(client, uid, head_b[1:], 1),
        ]
    )
    assert sorted(r.status_code for r in (response_a, response_b)) == [200, 409]
    winner_head = head_a if response_a.status_code == 200 else head_b
    loser = response_b if response_a.status_code == 200 else response_a
    assert loser.json()["offset"] == 26

    # The loser resumes from the committed offset; the final content is
    # exactly the winner's bytes plus the shared tail.
    tail = _put(client, uid, rest, 26)
    assert tail.status_code == 200
    assert tail.headers["Upload-Offset"] == str(len(payload_a))
    done = _complete(client, uid)
    assert done.status_code == 200
    assert done.json() == _verify(client, winner_head + rest).json()


def test_concurrent_completes_return_one_frozen_result(client):
    payload = good_batch(count=4)
    uid = _uid()
    assert _put(client, uid, payload, 0, len(payload)).status_code == 200

    results = _concurrent([lambda: _complete(client, uid) for _ in range(4)])
    assert all(r.status_code == 200 for r in results)
    # One transition happened; every caller observes the same frozen body.
    assert len({r.content for r in results}) == 1
    assert results[0].json()["status"] == "ACCEPT"


def test_concurrent_append_and_complete_are_decided_by_commit_order(client):
    payload = good_batch(count=3)
    uid = _uid()
    mid = len(payload) // 2
    assert _put(client, uid, payload[:mid], 0, len(payload)).status_code == 200

    put, complete = _concurrent(
        [
            lambda: _put(client, uid, payload[mid:], mid),
            lambda: _complete(client, uid),
        ]
    )
    # The append is always legal at the committed tail, so it always wins
    # its own race; the complete is decided purely by commit order.
    assert put.status_code == 200
    assert put.headers["Upload-Offset"] == str(len(payload))
    if complete.status_code == 409:
        # Ordered before the final append: refused with the then-current
        # offset, and a retry now completes the full upload.
        assert complete.json()["offset"] == mid
        done = _complete(client, uid)
        assert done.status_code == 200
        assert done.json() == _verify(client, payload).json()
    else:
        # Ordered after it: the full batch was verified immediately.
        assert complete.status_code == 200
        assert complete.json() == _verify(client, payload).json()


# ---------------------------------------------------------------------------
# Header and size validation
# ---------------------------------------------------------------------------


def test_put_requires_upload_offset_header(client):
    response = client.put(
        f"/uploads/{_uid()}",
        content=b"abc",
        headers={"Upload-Length": "3"},
    )
    assert response.status_code == 400


def test_first_chunk_requires_upload_length(client):
    response = _put(client, _uid(), b"abc", 0)
    assert response.status_code == 400


def test_declared_length_is_capped_at_1mib(client):
    response = _put(client, _uid(), b"abc", 0, MAX_BYTES + 1)
    assert response.status_code == 413
    assert "1 MiB" in response.json()["detail"]


def test_chunk_body_is_capped_at_1mib(client):
    response = _put(client, _uid(), b"0" * (MAX_BYTES + 1), 0, MAX_BYTES)
    assert response.status_code == 413
    assert "1 MiB" in response.json()["detail"]


def test_malformed_upload_headers_are_400(client):
    uid = _uid()
    response = client.put(
        f"/uploads/{uid}",
        content=b"abc",
        headers={"Upload-Offset": "soon", "Upload-Length": "3"},
    )
    assert response.status_code == 400
    response = client.put(
        f"/uploads/{uid}",
        content=b"abc",
        headers={"Upload-Offset": "0", "Upload-Length": "many"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Completed uploads keep the original verification semantics
# ---------------------------------------------------------------------------

_PARITY_PAYLOADS = [
    good_batch(count=3),                                            # ACCEPT
    good_batch(count=3, crlf=True),                                 # ACCEPT, CRLF
    good_batch(count=2, final_newline=False),                       # ACCEPT, no trailing LF
    b"X" + b"0" * 24 + b"\n",                                       # structural reject
    batch([detail(1, "A", 1)], head=header(count=2, total=99)),     # summary mismatch
    b"H" + b"00000001" + b"0001" + b"00000000000" + b"\xe9\n",      # non-ascii
    b"",                                                            # empty payload
]


@pytest.mark.parametrize("payload", _PARITY_PAYLOADS)
def test_completed_upload_matches_verify_semantics(client, payload):
    uid = _uid()
    mid = len(payload) // 2
    assert _put(client, uid, payload[:mid], 0, len(payload)).status_code == 200
    assert _put(client, uid, payload[mid:], mid).status_code == 200
    done = _complete(client, uid)
    assert done.status_code == 200
    # The frozen response is field-for-field the /verify verdict, whether
    # the batch is legal or illegal.
    assert done.json() == _verify(client, payload).json()
