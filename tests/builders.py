"""Helpers for constructing fixed-width batches byte by byte."""

from __future__ import annotations


def header(batch: str = "00000001", count: int | None = None, total: int | None = None,
           details: list[tuple[str, int]] | None = None) -> bytes:
    if details is not None:
        count = len(details) if count is None else count
        total = sum(amount for _invoice, amount in details) if total is None else total
    return b"H" + f"{batch}".encode() + f"{count:04d}".encode() + f"{total:012d}".encode()


def detail(sequence: int, invoice: str, amount: int) -> bytes:
    if len(invoice) > 10:
        raise ValueError("invoice max 10 characters")
    field = invoice.encode("ascii").ljust(10, b" ")
    return b"D" + f"{sequence:04d}".encode() + field + f"{amount:012d}".encode()


def batch(rows: list[bytes], *, crlf: bool = False, final_newline: bool = True,
         head: bytes | None = None) -> bytes:
    eol = b"\r\n" if crlf else b"\n"
    records = [head if head is not None else header(details=[])] + rows
    payload = eol.join(records)
    if final_newline:
        payload += eol
    return payload


def good_batch(*, count: int = 3, crlf: bool = False, final_newline: bool = True) -> bytes:
    pairs = [(f"INV{i}", 1000 * i) for i in range(1, count + 1)]
    rows = [detail(i, invoice, amount) for i, (invoice, amount) in enumerate(pairs, start=1)]
    return batch(rows, crlf=crlf, final_newline=final_newline,
                 head=header(details=pairs))
