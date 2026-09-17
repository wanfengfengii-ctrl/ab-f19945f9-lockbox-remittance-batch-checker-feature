"""Property-style checks: there is no fixed/canned response.

Random well-formed batches must produce totals recomputed independently of
the parser, and a single-byte corruption must never be accepted with a
shifted or stale amount.
"""

from __future__ import annotations

import random

from app.parser import MAX_BYTES, Status, parse_verification
from tests.builders import batch, detail, header

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _make(rng: random.Random):
    count = rng.randint(0, 40)
    # Keep each amount and the overall sum within 12 digits so the header
    # total field stays exactly 12 bytes.
    pairs = []
    for index in range(count):
        invoice = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 10)))
        pairs.append((invoice, rng.randint(1, 10**10 - 1)))
    rows = [detail(i + 1, invoice, amount) for i, (invoice, amount) in enumerate(pairs)]
    crlf = rng.choice((True, False))
    final_newline = rng.choice((True, False))
    return batch(rows, crlf=crlf, final_newline=final_newline,
                 head=header(details=pairs)), pairs


def test_random_valid_batches_match_independent_recomputation():
    rng = random.Random(20260915)
    for _ in range(200):
        payload, pairs = _make(rng)
        assert len(payload) <= MAX_BYTES
        result = parse_verification(payload)
        assert result.status is Status.ACCEPT, result.error
        assert result.computed_detail_count == len(pairs)
        assert result.computed_total_amount_cents == sum(a for _, a in pairs)
        assert [d.invoice for d in result.details] == [inv for inv, _ in pairs]
        assert [d.amount_cents for d in result.details] == [amt for _, amt in pairs]
        assert [d.sequence for d in result.details] == list(range(1, len(pairs) + 1))


def test_single_byte_corruption_never_accepts_with_wrong_money():
    rng = random.Random(4242)
    accepted_after_corruption = 0
    for _ in range(300):
        payload, pairs = _make(rng)
        if not payload:
            continue
        position = rng.randrange(len(payload))
        replacement = rng.choice(b"HDX \r\n!abc0123456789")
        corrupted = payload[:position] + bytes([replacement]) + payload[position + 1:]
        result = parse_verification(corrupted)
        if result.status is Status.ACCEPT:
            # Acceptable only if the mutation landed harmlessly in a way
            # that still satisfies the declared contract; the money must
            # then still equal the independent sum (never a shifted value).
            accepted_after_corruption += 1
            assert result.computed_total_amount_cents == sum(a for _, a in pairs)
            assert result.computed_detail_count == len(pairs)
    # Most random mutations corrupt the fixed-width format.
    assert accepted_after_corruption < 50
