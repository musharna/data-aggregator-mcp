"""A trailing ``# pragma: no mutate`` is read by mutmut 3.7 only where it ends a statement
that starts on its line: on a continuation line inside brackets it is ignored, and after
a statement spanning lines it covers only that statement's first line. Two pragmas of
2026-10-19 (#342) sat inside a call and an ``or`` chain and suppressed nothing; the
nightly kept their mutants. A pragma that must cover several lines is a
``# pragma: no mutate start`` / ``end`` pair on lines of their own.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_PRAGMA = "pragma: no mutate"


def ignored_pragmas(text: str) -> list[int]:
    """Lines whose trailing pragma mutmut does not read as covering that line."""
    bad: list[int] = []
    start: int | None = None  # the line the current statement started on
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    for i, tok in enumerate(tokens):
        if tok.type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.NL):
            if tok.type != tokenize.NL:
                start = None
            continue
        if tok.type == tokenize.COMMENT:
            if _PRAGMA in tok.string and tok.line.strip() != tok.string.strip():
                ends_statement = tokens[i + 1].type == tokenize.NEWLINE
                if not (ends_statement and start == tok.start[0]):
                    bad.append(tok.start[0])
            continue
        if start is None:
            start = tok.start[0]
    return bad


def test_the_check_tells_a_read_pragma_from_an_ignored_one() -> None:
    read = (
        "x = f(a)  # pragma: no mutate\n# pragma: no mutate start\ny = 1\n# pragma: no mutate end\n"
    )
    inside = "x = f(\n    a,  # pragma: no mutate\n)\n"
    after = "x = f(\n    a,\n)  # pragma: no mutate\n"
    assert ignored_pragmas(read) == []
    assert ignored_pragmas(inside) == [2]
    assert ignored_pragmas(after) == [3]


def test_every_trailing_pragma_in_src_is_one_mutmut_reads() -> None:
    files = sorted(_SRC.rglob("*.py"))
    assert files  # the scan found the package
    bad = {
        f"{p.relative_to(_SRC)}:{n}"
        for p in files
        for n in ignored_pragmas(p.read_text(encoding="utf-8"))
    }
    assert bad == set()
