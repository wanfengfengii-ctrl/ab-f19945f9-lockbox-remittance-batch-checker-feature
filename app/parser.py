"""Byte-exact parser and verifier for lockbox remittance batches.

Wire format (all offsets below are 1-based byte positions)::

    line 1 (header, exactly 25 bytes plus line ending):
        1      'H'
        2-9    batch number, exactly 8 decimal digits
        10-13  declared detail count, exactly 4 decimal digits
        14-25  declared total amount in cents, exactly 12 decimal digits

    lines 2..n (details, exactly 27 bytes plus line ending):
        1      'D'
        2-5    sequence number, 0001 increasing consecutively by 1
        6-15   invoice number: 1-10 uppercase [A-Z0-9], right-padded
               with spaces and with spaces nowhere else
        16-27  amount in cents, exactly 12 decimal digits, must be positive

Only ASCII is permitted, line endings are LF or CRLF, and the final line
ending is optional. A second 'H' record and empty lines are forbidden.

The parser is intentionally pure: it takes raw bytes, returns a result
object, never raises on malformed input and never returns canned values.
Every figure in an accepted result is computed from the payload itself.
Malformed batches return the single earliest error ordered by
``(line, column)``; parsing is a strict left-to-right scan, so the first
error encountered is by definition the earliest one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

#: Hard upper bound on an inbound batch (1 MiB). Enforced at the edge.
MAX_BYTES = 1 << 20

HEADER_LEN = 25
DETAIL_LEN = 27

_BATCH_SLICE = slice(1, 9)      # columns 2-9
_COUNT_SLICE = slice(9, 13)    # columns 10-13
_TOTAL_SLICE = slice(13, 25)   # columns 14-25
_SEQ_SLICE = slice(1, 5)       # columns 2-5
_INVOICE_SLICE = slice(5, 15)  # columns 6-15
_AMOUNT_SLICE = slice(15, 27)  # columns 16-27

_DIGIT_LO, _DIGIT_HI = ord("0"), ord("9")
_SPACE = ord(" ")
_H = ord("H")
_D = ord("D")


class Status(str, Enum):
    """Batch verdict. These two literals exist only in this module."""

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


class ErrorCode(str, Enum):
    EMPTY_LINE = "empty_line"
    NON_ASCII = "non_ascii"
    BAD_RECORD_TYPE = "bad_record_type"
    DUPLICATE_HEADER = "duplicate_header"
    BAD_LENGTH = "bad_length"
    BAD_BATCH_NUMBER = "bad_batch_number"
    BAD_DECLARED_COUNT = "bad_declared_count"
    BAD_DECLARED_TOTAL = "bad_declared_total"
    BAD_SEQUENCE = "bad_sequence"
    BAD_INVOICE = "bad_invoice"
    BAD_AMOUNT = "bad_amount"
    SUMMARY_MISMATCH = "summary_mismatch"


@dataclass(frozen=True)
class ParseError:
    line: int
    column: int
    code: ErrorCode
    message: str


@dataclass(frozen=True)
class Detail:
    line: int
    sequence: int
    invoice: str
    amount_cents: int


@dataclass(frozen=True)
class Declared:
    batch_number: str
    detail_count: int
    total_amount_cents: int


@dataclass
class VerificationResult:
    status: Status
    error: ParseError | None = None
    declared: Declared | None = None
    computed_detail_count: int = 0
    computed_total_amount_cents: int = 0
    #: Present only for ACCEPT. A REJECT never leaks partial details.
    details: list[Detail] = field(default_factory=list)
    #: Present only for REJECT caused by a summary mismatch.
    differences: dict[str, int] | None = None

    @property
    def accepted(self) -> bool:
        return self.status is Status.ACCEPT


def _reject(line: int, column: int, code: ErrorCode, message: str) -> VerificationResult:
    return VerificationResult(
        status=Status.REJECT,
        error=ParseError(line=line, column=column, code=code, message=message),
    )


def _is_digit(value: int) -> bool:
    return _DIGIT_LO <= value <= _DIGIT_HI


def _is_invoice_char(value: int) -> bool:
    # Uppercase A-Z or 0-9 only; lowercase letters and everything else
    # (including other ASCII controls) fall through.
    return (
        _DIGIT_LO <= value <= _DIGIT_HI
        or ord("A") <= value <= ord("Z")
    )


def _length_error(line: int, column: int, expected: int, actual: int, label: str) -> VerificationResult:
    return _reject(
        line,
        column,
        ErrorCode.BAD_LENGTH,
        f"{label} must be exactly {expected} bytes, got {actual}",
    )


def _scan_digit_field(
    raw: bytes,
    line: int,
    start: int,
    end: int,
    expected_width: int,
    code: ErrorCode,
    message: str,
    label: str,
) -> VerificationResult | None:
    """Scan inclusive 1-based columns start..end for decimal digits.

    Columns are visited in order, so the first offending column always
    wins: a missing byte in a truncated record is reported as a length
    error at that missing column rather than at column 1.
    """
    for col in range(start, end + 1):
        if col > len(raw):
            return _length_error(line, col, expected_width, len(raw), label)
        if not _is_digit(raw[col - 1]):
            return _reject(line, col, code, message)
    return None


def _split_records(blob: bytes) -> list[bytes]:
    """Split on LF, allowing CRLF and one optional trailing newline.

    A CR is stripped only when it terminates a record that actually ended
    in LF, so a lone trailing CR or a stray CR survives and fails
    downstream validation instead of being silently accepted.
    """
    pieces = blob.split(b"\n")
    trailing_newline = pieces[-1] == b""
    if trailing_newline:
        pieces.pop()
    records: list[bytes] = []
    last = len(pieces) - 1
    for index, piece in enumerate(pieces):
        ended_with_lf = trailing_newline or index < last
        if ended_with_lf and piece.endswith(b"\r"):
            piece = piece[:-1]
        records.append(piece)
    return records


def _check_invoice_columns(raw: bytes, line: int) -> VerificationResult | None:
    """Validate invoice columns 6-15 strictly in column order.

    Rule: 1-10 uppercase [A-Z0-9] characters padded with spaces only on
    the right. A missing column (truncated record) is a length error at
    that very column, so it still loses to an earlier illegal character.
    """
    pad_start: int | None = None
    for col in range(6, 16):
        if col > len(raw):
            return _length_error(line, col, DETAIL_LEN, len(raw), "detail")
        value = raw[col - 1]
        if value == _SPACE:
            if pad_start is None:
                pad_start = col
            continue
        if pad_start is not None:
            # Content follows a space, so that space cannot have been
            # right-side padding. The space is the earliest offending
            # byte whatever the following byte looks like (a legal
            # invoice character, a lowercase letter, punctuation, ...),
            # so report it at pad_start to preserve (line, column) order.
            return _reject(
                line,
                pad_start,
                ErrorCode.BAD_INVOICE,
                "invoice numbers may be padded with spaces only on the right",
            )
        if not _is_invoice_char(value):
            return _reject(
                line,
                col,
                ErrorCode.BAD_INVOICE,
                "invoice allows uppercase A-Z and 0-9 only, right-padded with spaces",
            )
    if pad_start == 6:
        return _reject(
            line,
            6,
            ErrorCode.BAD_INVOICE,
            "invoice number must contain 1-10 characters and be right-padded with spaces",
        )
    return None


def _earliest_non_ascii(blob: bytes) -> tuple[int, int] | None:
    """Locate the first byte outside ASCII as (1-based line, 1-based column)."""
    line = 1
    column = 1
    for value in blob:
        if value > 0x7F:
            return line, column
        if value == ord("\n"):
            line += 1
            column = 1
        else:
            column += 1
    return None


def parse_verification(blob: bytes) -> VerificationResult:
    """Verify a lockbox batch; never raises for malformed payloads."""
    if not blob:
        return _reject(1, 1, ErrorCode.EMPTY_LINE, "payload is empty")

    # ASCII gate. Structural validation below is itself byte-range strict,
    # so non-ASCII can never slip into an ACCEPT; this only makes the
    # earliest offending byte win when it precedes the first structural
    # error. Both scans are left-to-right and therefore report by
    # (line, column) order.
    ascii_position = _earliest_non_ascii(blob)

    structural = _parse_structure(blob)
    if structural.error is not None and ascii_position is not None:
        # A non-ASCII byte is itself the first structural fault at its own
        # position; at a tie report NON_ASCII, and if a structural fault
        # precedes it byte-for-byte, report that one.
        if (structural.error.line, structural.error.column) < ascii_position:
            return structural
    elif structural.error is not None:
        return structural
    if ascii_position is not None:
        line, column = ascii_position
        return _reject(
            line,
            column,
            ErrorCode.NON_ASCII,
            "only ASCII bytes are allowed",
        )
    return structural


def _parse_structure(blob: bytes) -> VerificationResult:
    records = _split_records(blob)

    header = records[0]
    if not header:
        return _reject(1, 1, ErrorCode.EMPTY_LINE, "header line is empty")
    if header[0] != _H:
        return _reject(
            1, 1, ErrorCode.BAD_RECORD_TYPE, "line 1 must be an H header record"
        )

    # Scan the header column by column so errors are ordered purely by
    # column: a wrong digit at column 2 is reported before an extra byte
    # at column 26, even though the record is then also the wrong length.
    header_error = _scan_digit_field(
        header, 1, 2, 9, HEADER_LEN,
        ErrorCode.BAD_BATCH_NUMBER,
        "batch number must be 8 decimal digits",
        "header",
    )
    if header_error is None:
        header_error = _scan_digit_field(
            header, 1, 10, 13, HEADER_LEN,
            ErrorCode.BAD_DECLARED_COUNT,
            "declared detail count must be 4 decimal digits",
            "header",
        )
    if header_error is None:
        header_error = _scan_digit_field(
            header, 1, 14, 25, HEADER_LEN,
            ErrorCode.BAD_DECLARED_TOTAL,
            "declared total must be 12 decimal digits",
            "header",
        )
    if header_error is not None:
        return header_error
    if len(header) > HEADER_LEN:
        return _length_error(1, HEADER_LEN + 1, HEADER_LEN, len(header), "header")

    declared = Declared(
        batch_number=header[_BATCH_SLICE].decode("ascii"),
        detail_count=int(header[_COUNT_SLICE]),
        total_amount_cents=int(header[_TOTAL_SLICE]),
    )

    details: list[Detail] = []
    computed_total = 0
    expected_sequence = 1

    for offset, raw in enumerate(records[1:], start=2):
        if not raw:
            return _reject(offset, 1, ErrorCode.EMPTY_LINE, "empty lines are not allowed")
        if raw[0] == _H:
            return _reject(
                offset, 1, ErrorCode.DUPLICATE_HEADER, "a second H header is not allowed"
            )
        if raw[0] != _D:
            return _reject(
                offset, 1, ErrorCode.BAD_RECORD_TYPE, "detail line must start with D"
            )

        # Column-by-column scan, mirroring the header: a wrong byte at an
        # early column is reported before a surplus byte at column 28.
        detail_error = _scan_digit_field(
            raw, offset, 2, 5, DETAIL_LEN,
            ErrorCode.BAD_SEQUENCE,
            "sequence must be 4 decimal digits",
            "detail",
        )
        if detail_error is not None:
            return detail_error
        sequence = int(raw[_SEQ_SLICE])
        if sequence != expected_sequence:
            return _reject(
                offset,
                2,
                ErrorCode.BAD_SEQUENCE,
                f"expected sequence {expected_sequence:04d}, got {sequence:04d}",
            )

        detail_error = _check_invoice_columns(raw, offset)
        if detail_error is not None:
            return detail_error

        detail_error = _scan_digit_field(
            raw, offset, 16, 27, DETAIL_LEN,
            ErrorCode.BAD_AMOUNT,
            "amount must be 12 decimal digits",
            "detail",
        )
        if detail_error is not None:
            return detail_error
        amount = int(raw[_AMOUNT_SLICE])
        if amount <= 0:
            return _reject(
                offset, 16, ErrorCode.BAD_AMOUNT, "amount must be a positive number of cents"
            )
        if len(raw) > DETAIL_LEN:
            return _length_error(offset, DETAIL_LEN + 1, DETAIL_LEN, len(raw), "detail")

        details.append(
            Detail(
                line=offset,
                sequence=sequence,
                invoice=raw[_INVOICE_SLICE].rstrip(b" ").decode("ascii"),
                amount_cents=amount,
            )
        )
        computed_total += amount
        expected_sequence += 1

    count_difference = len(details) - declared.detail_count
    amount_difference = computed_total - declared.total_amount_cents
    if count_difference or amount_difference:
        # Point at the earliest declared summary field that disagrees.
        column = 10 if count_difference else 14
        result = _reject(
            1,
            column,
            ErrorCode.SUMMARY_MISMATCH,
            "declared summary does not match the computed summary",
        )
        result.declared = declared
        result.computed_detail_count = len(details)
        result.computed_total_amount_cents = computed_total
        result.differences = {
            "detail_count": count_difference,
            "total_amount_cents": amount_difference,
        }
        return result

    return VerificationResult(
        status=Status.ACCEPT,
        declared=declared,
        computed_detail_count=len(details),
        computed_total_amount_cents=computed_total,
        details=details,
    )
