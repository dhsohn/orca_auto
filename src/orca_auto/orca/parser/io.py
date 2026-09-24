"""File IO helpers for ORCA parser internals.

Every reader of an ORCA ``.out`` chooses its codec here, from the same bounded
prefix and by the same rule, whether it loads the file whole
(:func:`read_orca_text`) or streams it line by line (:func:`open_orca_text`).
The completion verdict, the frequency analysis and the scan tables therefore
see one decoding of one file.
"""

from __future__ import annotations

import codecs
import io
from pathlib import Path
from typing import TextIO

# Bytes inspected to choose the codec. A BOM sits in the first bytes; the
# UTF-16 heuristic needs a sample, not the file, so a multi-gigabyte output is
# never loaded to decide how to read it.
ENCODING_SNIFF_BYTES = 64 * 1024
# BOM-less UTF-16 signature: ORCA writes ASCII, so one byte of every code unit
# is NUL. The NUL bytes of a genuine UTF-16 sample sit on one parity only; a
# zero-filled hole in a UTF-8 file puts NULs on both parities and stays UTF-8.
_UTF16_SIGNATURE_RATIO = 0.20
_UTF16_OTHER_PARITY_RATIO = 0.05


def _utf16_byte_order(prefix: bytes) -> str | None:
    even = prefix[0::2]
    odd = prefix[1::2]
    if not even or not odd:
        return None
    even_ratio = even.count(0) / len(even)
    odd_ratio = odd.count(0) / len(odd)
    if odd_ratio > _UTF16_SIGNATURE_RATIO and even_ratio < _UTF16_OTHER_PARITY_RATIO:
        return "utf-16-le"
    if even_ratio > _UTF16_SIGNATURE_RATIO and odd_ratio < _UTF16_OTHER_PARITY_RATIO:
        return "utf-16-be"
    return None


def detect_orca_encoding(prefix: bytes) -> str:
    """Codec of an ORCA output whose first bytes are ``prefix``.

    A UTF-16 or UTF-8 BOM decides; otherwise NUL bytes confined to one byte
    parity mark BOM-less UTF-16 in that byte order when the sample decodes
    cleanly, and everything else is UTF-8. Callers decode with
    ``errors="replace"`` so a stray byte becomes U+FFFD instead of shifting or
    dropping text.
    """
    if prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if prefix.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    encoding = _utf16_byte_order(prefix)
    if encoding is not None:
        # A prefix may end mid code unit or mid surrogate pair; a non-final
        # incremental decode tolerates that truncation and nothing else.
        sample = prefix[: len(prefix) - len(prefix) % 2]
        try:
            codecs.getincrementaldecoder(encoding)().decode(sample, final=False)
        except UnicodeDecodeError:
            return "utf-8"
        return encoding
    return "utf-8"


def read_orca_text(file_path: str | Path) -> str:
    """Whole ORCA output decoded by :func:`detect_orca_encoding`."""
    with open(file_path, "rb") as handle:
        raw = handle.read()
    if not raw:
        return ""
    return raw.decode(detect_orca_encoding(raw[:ENCODING_SNIFF_BYTES]), errors="replace")


def open_orca_text(file_path: str | Path) -> TextIO:
    """Line-streaming handle over an ORCA output, decoded like :func:`read_orca_text`.

    Only :data:`ENCODING_SNIFF_BYTES` are read to choose the codec; the rest
    is decoded incrementally as the caller iterates, with universal newlines
    (CR, LF and CRLF, exactly as ``open(..., "r")`` splits). Use it as a
    context manager; ``OSError`` propagates as from ``open``.
    """
    binary = open(file_path, "rb")
    try:
        encoding = detect_orca_encoding(binary.read(ENCODING_SNIFF_BYTES))
        binary.seek(0)
        return io.TextIOWrapper(binary, encoding=encoding, errors="replace")
    except BaseException:
        binary.close()
        raise
