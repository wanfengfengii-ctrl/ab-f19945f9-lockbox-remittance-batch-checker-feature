"""Acceptance tests for the resumable upload chain (PUT/complete + /verify).

In-process by default; set BASE_URL to run the same cases against a live
server. Live runs share one server-side SQLite file, so every test uses a
fresh random upload id instead of relying on a clean database.
"""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parser import MAX_BYTES
from app.uploads import AppendOutcome, CompleteOutcome, UploadStore
from tests.builders import batch, detail, good_batch, header


@pytest.fixture
def client(tmp_path):
    base_url = os.environ.get("BASE_URL")
    if base_url:
        with httpx.Client(base_url=base_url, timeout=10) as live:
            yield live
        return
    # Isolate every in-process test with its own fresh SQLite file.
    app.state.upload_store = UploadStore(str(tmp_path / "uploads.db"))
    with TestClient(app) as in_process:
        yield in_process


def _uid() -> str:
    return f"t-{uuid.uuid4().hex}"


def put_chunk(client, uid: str, offset: int, body: bytes, length: int | None = None):
    headers = {"Upload-Offset": str(offset)}
    if length is not None:
        headers["Upload-Length"] = str(length)
    return client.put(f"/uploads/{uid}", content=body, headers=headers)


def verify(client, payload: bytes):
    return client.post(
        "/verify", content=payload, headers={"Content-Type": "application/octet-stream"}
    )


# ---------------------------------------------------------------------------
# Happy path: interrupted transfer resumes and verifies like a one-shot POST
# ---------------------------------------------------------------------------


def test_interrupted_resume_succeeds(client):
    payload = good_batch(count=5)
    uid = _uid()
    total = len(payload)
    first, second, third = payload[:40], payload[40:97], payload[97:]

    r = put_chunk(client, uid, 0, first, length=total)
    assert r.status_code == 204
    assert r.headers["Upload-Offset"] == "40"

    # The line drops after chunk 2 is stored but before the response
    # arrives: the client retries chunk 2, then resumes from the offset.
    r = put_chunk(client, uid, 40, second)
    assert r.status_code == 204
    assert r.headers["Upload-Offset"] == "97"
    r = put_chunk(client, uid, 40, second)
    assert r.status_code == 204
    assert r.headers["Upload-Offset"] == "97"

    r = put_chunk(client, uid, 97, third)
    assert r.status_code == 204
    assert r.headers["Upload-Offset"] == str(total)

    done = client.post(f"/uploads/{uid}/complete")
    assert done.status_code == 200
    assert done.json() == verify(client, payload).json()
    assert done.json()["status"] == "ACCEPT"


def test_identical_chunk_retry_is_idempotent(client):
    payload = good_batch(count=3)
    uid = _uid()
    total = len(payload)
    half = total // 2

    assert put_chunk(client, uid, 0, payload[:half], length=total).status_code == 204
    # The same chunk again: no growth, still a success with the same offset.
    for _ in range(2):
        r = put_chunk(client, uid, 0, payload[:half], length=total)
        assert r.status_code == 204
        assert r.headers["Upload-Offset"] == str(half)
    # A retry of an interior slice is equally idempotent.
    r = put_chunk(client, uid, 3, payload[3 : half - 5])
    assert r.status_code == 204
    assert r.headers["Upload-Offset"] == str(half)
    # An empty probe at the tail is a no-op success.
    r = put_chunk(client, uid, half, b"")
    assert r.status_code == 204
    assert r.headers["Upload-Offset"] == str(half)


# ---------------------------------------------------------------------------
# Conflicts: 409 with the current offset, stored bytes untouched
# ---------------------------------------------------------------------------


def test_conflicting_chunks_rejected_without_touching_data(client):
    payload = good_batch(count=4)
    uid = _uid()
    total = len(payload)
    first = payload[:60]

    r = put_chunk(client, uid, 0, first, length=total)
    assert r.status_code == 204

    # Content conflict: same range, different bytes.
    corrupted = bytes([first[0] ^ 1]) + first[1:]
    r = put_chunk(client, uid, 0, corrupted)
    assert r.status_code == 409
    assert r.headers["Upload-Offset"] == "60"

    # Crossing the tail: starts inside the saved range, ends beyond it.
    r = put_chunk(client, uid, 30, payload[30:90])
    assert r.status_code == 409
    assert r.headers["Upload-Offset"] == "60"

    # Gap: offset beyond the current tail.
    r = put_chunk(client, uid, 61, payload[61:90])
    assert r.status_code == 409
    assert r.headers["Upload-Offset"] == "60"

    # Declared total changed mid-session.
    r = put_chunk(client, uid, 60, payload[60:90], length=total + 1)
    assert r.status_code == 409
    assert r.headers["Upload-Offset"] == "60"

    # Chunk would end beyond the declared total.
    r = put_chunk(client, uid, 60, payload[60:] + b"x")
    assert r.status_code == 409
    assert r.headers["Upload-Offset"] == "60"

    # None of the above touched the stored bytes: the resume completes and
    # the assembled batch verifies exactly like a one-shot upload.
    r = put_chunk(client, uid, 60, payload[60:])
    assert r.status_code == 204
    assert r.headers["Upload-Offset"] == str(total)
    done = client.post(f"/uploads/{uid}/complete")
    assert done.status_code == 200
    assert done.json() == verify(client, payload).json()


def test_first_chunk_and_header_rules(client):
    payload = good_batch(count=1)

    # Missing Upload-Offset.
    r = client.put(
        f"/uploads/{_uid()}",
        content=payload[:10],
        headers={"Upload-Length": str(len(payload))},
    )
    assert r.status_code == 400

    # First chunk without Upload-Length.
    r = put_chunk(client, _uid(), 0, payload[:10])
    assert r.status_code == 400

    # Declared total above 1 MiB.
    r = put_chunk(client, _uid(), 0, payload[:10], length=MAX_BYTES + 1)
    assert r.status_code == 413

    # Non-zero offset for an unknown upload id.
    r = put_chunk(client, _uid(), 5, payload[5:10], length=len(payload))
    assert r.status_code == 404

    # First chunk larger than its declared total: rejected, nothing stored,
    # so a well-formed first chunk afterwards starts the session cleanly.
    uid = _uid()
    r = put_chunk(client, uid, 0, payload[:20], length=10)
    assert r.status_code == 409
    assert r.headers["Upload-Offset"] == "0"
    r = put_chunk(client, uid, 0, payload, length=len(payload))
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# Completion: gating, sealing, frozen response, original verify semantics
# ---------------------------------------------------------------------------


def test_complete_requires_all_bytes(client):
    payload = good_batch(count=2)
    uid = _uid()
    total = len(payload)
    assert put_chunk(client, uid, 0, payload[:30], length=total).status_code == 204

    r = client.post(f"/uploads/{uid}/complete")
    assert r.status_code == 409
    assert r.headers["Upload-Offset"] == "30"

    # Unknown upload id.
    assert client.post(f"/uploads/{_uid()}/complete").status_code == 404


def test_complete_seals_session_and_repeats_same_response(client):
    payload = good_batch(count=3)
    uid = _uid()
    total = len(payload)
    assert put_chunk(client, uid, 0, payload, length=total).status_code == 204

    first = client.post(f"/uploads/{uid}/complete")
    assert first.status_code == 200
    second = client.post(f"/uploads/{uid}/complete")
    assert second.status_code == 200
    assert second.content == first.content

    # The session is sealed: even a byte-identical retry is refused.
    r = put_chunk(client, uid, 0, payload[:10])
    assert r.status_code == 409
    assert r.headers["Upload-Offset"] == str(total)


def test_completed_upload_of_invalid_batch_matches_verify(client):
    # Declared summary disagrees with the details: a legal transport of an
    # illegal batch must keep the exact one-shot REJECT semantics.
    payload = batch([detail(1, "A", 1)], head=header(count=2, total=99))
    uid = _uid()
    cut = 17
    assert put_chunk(client, uid, 0, payload[:cut], length=len(payload)).status_code == 204
    assert put_chunk(client, uid, cut, payload[cut:]).status_code == 204

    done = client.post(f"/uploads/{uid}/complete")
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "REJECT"
    assert body == verify(client, payload).json()
    assert body["differences"] == {"detail_count": -1, "total_amount_cents": -98}


def test_zero_length_upload_completes_as_empty_reject(client):
    uid = _uid()
    r = put_chunk(client, uid, 0, b"", length=0)
    assert r.status_code == 204
    assert r.headers["Upload-Offset"] == "0"
    done = client.post(f"/uploads/{uid}/complete")
    assert done.status_code == 200
    assert done.json()["status"] == "REJECT"
    assert done.json()["error"]["code"] == "empty_line"


# ---------------------------------------------------------------------------
# Concurrency: commit order decides, losers get deterministic feedback
# ---------------------------------------------------------------------------


def test_concurrent_writers_single_winner_no_pollution(client):
    uid = _uid()
    payloads = [good_batch(count=i + 1) for i in range(5)]

    def upload(payload: bytes):
        return put_chunk(client, uid, 0, payload, length=len(payload))

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        responses = list(pool.map(upload, payloads))

    wins = [i for i, r in enumerate(responses) if r.status_code == 204]
    assert len(wins) == 1
    winner = wins[0]
    for i, r in enumerate(responses):
        if i != winner:
            assert r.status_code == 409
            assert r.headers["Upload-Offset"] == str(len(payloads[winner]))

    # The stored bytes are exactly the winner's payload — never a blend.
    done = client.post(f"/uploads/{uid}/complete")
    assert done.status_code == 200
    assert done.json() == verify(client, payloads[winner]).json()


def test_concurrent_completes_return_identical_frozen_response(client):
    payload = good_batch(count=3)
    uid = _uid()
    assert put_chunk(client, uid, 0, payload, length=len(payload)).status_code == 204

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post(f"/uploads/{uid}/complete"), range(4)))

    assert all(r.status_code == 200 for r in responses)
    assert len({r.content for r in responses}) == 1
    assert responses[0].json() == verify(client, payload).json()


def test_store_serializes_racing_appends_by_commit_order(tmp_path):
    store = UploadStore(str(tmp_path / "uploads.db"))
    uid = "race"
    total = 600
    payloads = [bytes([65 + i]) * total for i in range(6)]
    first_chunks = [p[:100] for p in payloads]

    barrier = threading.Barrier(len(payloads))
    lock = threading.Lock()
    results: list[tuple[int, object]] = []

    def worker(index: int):
        barrier.wait()
        result = store.append(uid, 0, total, first_chunks[index])
        with lock:
            results.append((index, result))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(payloads))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    appended = [i for i, r in results if r.outcome is AppendOutcome.APPENDED]
    assert len(appended) == 1
    assert all(r.received == 100 for _i, r in results)

    # Finish the winner's upload: the assembled bytes are purely its own.
    winner = appended[0]
    rest = store.append(uid, 100, None, payloads[winner][100:])
    assert rest.outcome is AppendOutcome.APPENDED
    assert rest.received == total

    captured = {}

    def finalize(data: bytes) -> str:
        captured["data"] = data
        return "{}"

    completed = store.complete(uid, finalize)
    assert completed.outcome is CompleteOutcome.COMPLETED
    assert captured["data"] == payloads[winner]


def test_store_racing_final_append_and_complete(tmp_path):
    store = UploadStore(str(tmp_path / "uploads.db"))
    uid = "finish"
    payload = good_batch(count=2)
    total = len(payload)
    assert store.append(uid, 0, total, payload[: total - 5]).outcome is AppendOutcome.APPENDED

    barrier = threading.Barrier(2)
    outcomes = {}

    def appender():
        barrier.wait()
        outcomes["append"] = store.append(uid, total - 5, None, payload[total - 5 :])

    def completer():
        barrier.wait()
        outcomes["complete"] = store.complete(uid, lambda data: "{}")

    threads = [threading.Thread(target=appender), threading.Thread(target=completer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes["append"].outcome is AppendOutcome.APPENDED
    # The complete either committed after the append (and sealed) or before
    # it (and reported the shortfall) — never a torn state.
    if outcomes["complete"].outcome is CompleteOutcome.COMPLETED:
        assert outcomes["complete"].received == total
    else:
        assert outcomes["complete"].outcome is CompleteOutcome.INCOMPLETE
        assert outcomes["complete"].received == total - 5
    # Either way the session ends sealed: a later complete is either the
    # one that flips it or a replay of the already-frozen response.
    sealed = store.complete(uid, lambda data: "{}")
    assert sealed.outcome in (CompleteOutcome.COMPLETED, CompleteOutcome.ALREADY)
    assert sealed.received == total
