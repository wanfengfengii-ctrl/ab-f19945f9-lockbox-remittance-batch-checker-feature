"""Unit tests for the byte-exact parser.

Every batch is assembled byte by byte so that byte positions asserted here
correspond directly to the column numbering in the wire-format contract.
"""

from __future__ import annotations

from app.parser import (
    HEADER_LEN,
    MAX_BYTES,
    ErrorCode,
    Status,
    parse_verification,
)
from tests.builders import batch, detail, good_batch, header


def good_head(count: int = 1, total: int = 1) -> bytes:
    return header(batch="00000001", count=count, total=total)


def one_detail(sequence: int = 1, invoice: bytes = b"A", amount: bytes = b"000000000001") -> bytes:
    return b"D" + f"{sequence:04d}".encode() + invoice.ljust(10, b" ") + amount


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_accept_minimal_one_detail():
    result = parse_verification(good_batch(count=1, final_newline=False))
    assert result.status is Status.ACCEPT
    assert result.declared.batch_number == "00000001"
    assert result.computed_detail_count == 1
    assert result.computed_total_amount_cents == 1000
    (d,) = result.details
    assert (d.sequence, d.invoice, d.amount_cents, d.line) == (1, "INV1", 1000, 2)


def test_accept_lf_crlf_and_no_trailing_newline():
    assert parse_verification(good_batch(crlf=False)).accepted
    assert parse_verification(good_batch(crlf=True)).accepted
    assert parse_verification(good_batch(final_newline=False)).accepted
    assert parse_verification(good_batch(crlf=True, final_newline=False)).accepted


def test_accept_invoice_variants_and_padding():
    invoices = ["A", "AB", "A1", "Z9", "ABCDEFGHIJ"]  # last is exactly 10
    pairs = [(name, idx + 1) for idx, name in enumerate(invoices)]
    rows = [detail(i, name, amount) for i, (name, amount) in enumerate(pairs, start=1)]
    result = parse_verification(batch(rows, head=header(details=pairs)))
    assert result.accepted
    assert [d.invoice for d in result.details] == invoices


def test_accept_zero_detail_zero_total_batch():
    payload = header(batch="00000001", count=0, total=0) + b"\n"
    assert len(payload.rstrip(b"\n")) == 25
    result = parse_verification(payload)
    assert result.accepted
    assert result.details == []
    assert result.computed_detail_count == 0
    assert result.computed_total_amount_cents == 0


def test_reject_never_holds_partial_details():
    result = parse_verification(b"")
    assert result.status is Status.REJECT
    assert result.details == []
    assert result.declared is None
    assert result.computed_total_amount_cents == 0


# ---------------------------------------------------------------------------
# Header violations
# ---------------------------------------------------------------------------

def test_empty_payload():
    result = parse_verification(b"")
    assert result.error.code is ErrorCode.EMPTY_LINE
    assert (result.error.line, result.error.column) == (1, 1)


def test_header_wrong_record_type():
    result = parse_verification(b"X" + b"0" * 24 + b"\n")
    assert result.error.code is ErrorCode.BAD_RECORD_TYPE
    assert (result.error.line, result.error.column) == (1, 1)


def test_header_wrong_length_short_and_long():
    # Pure length defects are located at the first missing column (short)
    # or the first surplus column (long), never blanket-reported at col 1.
    short = b"H" + b"00000001" + b"0000" + b"0" * 11  # 24 bytes
    result = parse_verification(short + b"\n")
    assert result.error.code is ErrorCode.BAD_LENGTH
    assert (result.error.line, result.error.column) == (1, 25)

    long = b"H" + b"0" * 24 + b"X"  # 26 bytes
    result = parse_verification(long + b"\n")
    assert result.error.code is ErrorCode.BAD_LENGTH
    assert (result.error.line, result.error.column) == (1, 26)


def test_header_bad_column_2_wins_over_surplus_byte():
    # Regression: a wrong byte at column 2 and one surplus byte at the end
    # must report bad_batch_number at column 2, not a length error at 1/26.
    head = b"H" + b"x" + b"0" * 7 + b"0001" + b"0" * 12 + b"X"
    assert len(head) == 26
    result = parse_verification(head + b"\n")
    assert result.error.code is ErrorCode.BAD_BATCH_NUMBER
    assert (result.error.line, result.error.column) == (1, 2)


def test_header_bad_column_2_wins_over_truncation():
    # 24-byte header (missing total column 25) but column 2 is illegal:
    # column 2 is earlier than the first missing column.
    head = b"H" + b"x" + b"0" * 7 + b"0001" + b"0" * 11
    assert len(head) == 24
    result = parse_verification(head + b"\n")
    assert result.error.code is ErrorCode.BAD_BATCH_NUMBER
    assert result.error.column == 2


def test_header_bad_count_column_wins_over_surplus():
    head = b"H" + b"00000001" + b"00x0" + b"0" * 12 + b"ZZ"
    result = parse_verification(head + b"\n")
    assert result.error.code is ErrorCode.BAD_DECLARED_COUNT
    assert result.error.column == 12


def test_header_bad_batch_digits_reports_column_9():
    head = b"H" + b"0000000x" + b"0000" + b"0" * 12
    result = parse_verification(head + b"\n")
    assert result.error.code is ErrorCode.BAD_BATCH_NUMBER
    assert result.error.column == 9


def test_header_bad_count_digits_reports_first_bad_column():
    head = b"H" + b"00000001" + b"00x0" + b"0" * 12
    result = parse_verification(head + b"\n")
    assert result.error.code is ErrorCode.BAD_DECLARED_COUNT
    assert result.error.column == 12


def test_header_bad_total_digits_reports_column_25():
    head = b"H" + b"00000001" + b"0000" + b"0" * 11 + b"x"
    result = parse_verification(head + b"\n")
    assert result.error.code is ErrorCode.BAD_DECLARED_TOTAL
    assert result.error.column == 25


# ---------------------------------------------------------------------------
# Detail violations
# ---------------------------------------------------------------------------

def test_detail_wrong_length():
    # Short: first missing column is 27 -> bad_length at column 27.
    payload = good_batch(count=1)
    payload = payload[: len(payload) - 2] + b"\n"  # drop last content byte
    result = parse_verification(payload)
    assert result.error.code is ErrorCode.BAD_LENGTH
    assert (result.error.line, result.error.column) == (2, 27)

    # Long with otherwise-valid columns: surplus byte is column 28.
    long_detail = one_detail() + b"X"  # 28 bytes
    result = parse_verification(good_head() + b"\n" + long_detail + b"\n")
    assert result.error.code is ErrorCode.BAD_LENGTH
    assert (result.error.line, result.error.column) == (2, 28)


def test_detail_bad_sequence_column_wins_over_surplus_byte():
    # 28-byte detail, but sequence column 4 is illegal; that precedes the
    # surplus byte at column 28.
    bad = b"D" + b"00x0" + b"A".ljust(10) + b"000000000001" + b"X"
    assert len(bad) == 28
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.BAD_SEQUENCE
    assert (result.error.line, result.error.column) == (2, 4)


def test_detail_bad_invoice_column_wins_over_truncation():
    # 26-byte detail (missing amount columns 26-27), but the invoice at
    # column 6 is lowercase, which is earlier than the first missing col.
    bad = b"D" + b"0001" + b"abc".ljust(10) + b"00000000000"  # 26 bytes
    assert len(bad) == 26
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.BAD_INVOICE
    assert (result.error.line, result.error.column) == (2, 6)


def test_detail_truncated_inside_amount_reports_first_missing_amount_column():
    # Valid through column 25; amount column 26 is missing.
    bad = b"D" + b"0001" + b"A".ljust(10) + b"0000000000"  # 26 bytes
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.BAD_LENGTH
    assert (result.error.line, result.error.column) == (2, 26)


def test_detail_bad_sequence_digits():
    bad = one_detail(amount=b"000000000001")
    bad = b"D" + b"00x0" + bad[5:]
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.BAD_SEQUENCE
    assert result.error.column == 4


def test_detail_sequence_gap_and_wrong_start():
    head = header(count=2, total=2)
    rows = [detail(1, "A", 1), detail(3, "B", 1)]
    result = parse_verification(batch(rows, head=head))
    assert result.error.code is ErrorCode.BAD_SEQUENCE
    assert (result.error.line, result.error.column) == (3, 2)

    result = parse_verification(good_head() + b"\n" + one_detail(2) + b"\n")
    assert result.error.code is ErrorCode.BAD_SEQUENCE
    assert (result.error.line, result.error.column) == (2, 2)


def test_detail_lowercase_invoice_rejected_at_column_6():
    bad = one_detail(invoice=b"abc")
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.BAD_INVOICE
    assert result.error.column == 6


def test_detail_invoice_internal_space_reports_space_column():
    field = b"AB CD     "  # first space at byte 8 (column 8)
    bad = b"D" + b"0001" + field + b"000000000001"
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.BAD_INVOICE
    assert result.error.column == 8


def test_detail_invoice_char_after_padding():
    field = b"AB       X"  # padding starts at column 8, X at column 15
    bad = b"D" + b"0001" + field + b"000000000001"
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.BAD_INVOICE
    # A legal invoice char follows a space, so the first space at column 8
    # is the earliest byte that cannot be right-side padding.
    assert result.error.column == 8


def test_detail_invoice_illegal_byte_after_padding_reports_padding_start():
    # Content after a space means that space started too early; whatever
    # follows (here '!' at column 15), the earliest offending byte is the
    # first padding space at column 8.
    field = b"AB       !"  # padding begins at column 8, '!' at column 15
    bad = b"D" + b"0001" + field + b"000000000001"
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.BAD_INVOICE
    assert result.error.column == 8


def test_detail_space_at_column_6_then_lowercase_reports_column_6():
    # Regression: column 6 is a space yet a lowercase letter follows; the
    # space is the first byte that cannot be right-side padding, so the
    # error must point at column 6, not at column 7.
    field = b" abc      "  # space at column 6, lowercase 'a' at column 7
    bad = b"D" + b"0001" + field + b"000000000001"
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.BAD_INVOICE
    assert (result.error.line, result.error.column) == (2, 6)


def test_detail_invoice_all_spaces():
    bad = b"D" + b"0001" + b" " * 10 + b"000000000001"
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.BAD_INVOICE
    assert result.error.column == 6


def test_detail_zero_and_negative_amount():
    zero = b"D" + b"0001" + b"A".ljust(10) + b"0" * 12
    result = parse_verification(good_head() + b"\n" + zero + b"\n")
    assert result.error.code is ErrorCode.BAD_AMOUNT
    assert result.error.column == 16

    minus = b"D" + b"0001" + b"A".ljust(10) + b"-" + b"0" * 11
    result = parse_verification(good_head() + b"\n" + minus + b"\n")
    assert result.error.code is ErrorCode.BAD_AMOUNT
    assert result.error.column == 16


def test_detail_wrong_record_type():
    bad = b"X" + b"0001" + b"A".ljust(10) + b"000000000001"
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.BAD_RECORD_TYPE
    assert result.error.column == 1


def test_second_header_rejected():
    payload = good_batch(count=1) + b"H" + b"0" * 24 + b"\n"
    result = parse_verification(payload)
    assert result.error.code is ErrorCode.DUPLICATE_HEADER
    assert result.error.line == 3


def test_empty_line_between_records_and_blank_tail():
    rows = good_batch(count=2)
    payload = rows[: HEADER_LEN + 1] + b"\n" + rows[HEADER_LEN + 1:]
    result = parse_verification(payload)
    assert result.error.code is ErrorCode.EMPTY_LINE
    assert result.error.line == 2

    result = parse_verification(good_head() + b"\n\n")
    assert result.error.code is ErrorCode.EMPTY_LINE
    assert result.error.line == 2


def test_stray_cr_not_part_of_crlf_fails():
    # CR embedded in the amount field (not immediately before LF) is a
    # stray byte, not a CRLF ending: record stays 27 bytes, amount fails.
    bad = b"D" + b"0001" + b"A".ljust(10) + b"0000000000\r1"
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.BAD_AMOUNT
    assert result.error.column == 26


# ---------------------------------------------------------------------------
# ASCII gating and earliest-error precedence
# ---------------------------------------------------------------------------

def test_non_ascii_in_header_total():
    bad = b"H" + b"00000001" + b"0001" + b"00000000000" + b"\xe9"
    result = parse_verification(bad + b"\n")
    assert result.error.code is ErrorCode.NON_ASCII
    assert result.error.column == 25


def test_non_ascii_in_invoice():
    bad = b"D" + b"0001" + b"\xe9" + b" " * 9 + b"000000000001"
    result = parse_verification(good_head() + b"\n" + bad + b"\n")
    assert result.error.code is ErrorCode.NON_ASCII
    assert (result.error.line, result.error.column) == (2, 6)


def test_structural_error_earlier_than_non_ascii_wins():
    bad = b"X" + b"00000001" + b"0001" + b"00000000000" + b"\xe9"
    result = parse_verification(bad + b"\n")
    assert result.error.code is ErrorCode.BAD_RECORD_TYPE
    assert (result.error.line, result.error.column) == (1, 1)


def test_first_bad_line_wins_only_one_error():
    bad1 = b"D" + b"00x0" + b"A".ljust(10) + b"000000000001"
    bad2 = b"D" + b"0002" + b" " * 10 + b"000000000001"
    payload = good_head(count=2, total=2) + b"\n" + bad1 + b"\n" + bad2 + b"\n"
    result = parse_verification(payload)
    assert result.error.code is ErrorCode.BAD_SEQUENCE
    assert (result.error.line, result.error.column) == (2, 4)


# ---------------------------------------------------------------------------
# Summary mismatches and differences
# ---------------------------------------------------------------------------

def test_missing_final_detail_count_mismatch():
    payload = batch([detail(1, "A", 1)], head=header(count=2, total=1))
    result = parse_verification(payload)
    assert result.error.code is ErrorCode.SUMMARY_MISMATCH
    assert result.error.column == 10
    assert result.differences == {"detail_count": -1, "total_amount_cents": 0}
    assert result.computed_detail_count == 1
    assert result.declared.detail_count == 2
    assert result.details == []


def test_extra_detail_count_mismatch():
    rows = [detail(1, "A", 1), detail(2, "B", 2)]
    result = parse_verification(batch(rows, head=header(count=1, total=3)))
    assert result.error.code is ErrorCode.SUMMARY_MISMATCH
    assert result.differences == {"detail_count": 1, "total_amount_cents": 0}


def test_amount_sum_mismatch_points_at_total_field():
    rows = [detail(1, "A", 1), detail(2, "B", 2)]
    result = parse_verification(batch(rows, head=header(count=2, total=4)))
    assert result.error.code is ErrorCode.SUMMARY_MISMATCH
    assert result.error.column == 14
    assert result.differences == {"detail_count": 0, "total_amount_cents": -1}
    assert result.computed_total_amount_cents == 3


def test_both_mismatches_report_count_field_and_both_diffs():
    payload = batch([detail(1, "A", 1)], head=header(count=2, total=99))
    result = parse_verification(payload)
    assert result.error.column == 10
    assert result.differences["detail_count"] == -1
    assert result.differences["total_amount_cents"] == -98


def test_twelve_digit_amounts_sum_exactly():
    big = 10**12 - 1  # maximum 12-digit positive amount
    result = parse_verification(batch([detail(1, "INV", big)],
                                      head=header(count=1, total=big)))
    assert result.accepted
    assert result.computed_total_amount_cents == big
    assert result.details[0].amount_cents == big


def test_max_size_is_one_mib_constant():
    assert MAX_BYTES == 1024 * 1024
