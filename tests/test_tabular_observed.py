"""What schema/preview read and return, pinned field by field (#88 mutant burn-down).

The answers here are the full dicts the `operate` tool hands back, so a renamed key, a
dropped flag or a wrong type string fails a test. The window boundary and the read
size are driven at their exact edges.
"""

import pathlib

import pytest

pytest.importorskip("fsspec")
pytest.importorskip("pyarrow")

from data_aggregator_mcp import tabular

FX = pathlib.Path(__file__).parent / "fixtures"
PARQUET_URL = (FX / "sample.parquet").as_uri()
CSV_URL = (FX / "sample.csv").as_uri()
N = tabular._CSV_SNIFF_BYTES

_PARQUET_COLUMNS = [
    {"name": "id", "type": "int64"},
    {"name": "name", "type": "string"},
    {"name": "temp", "type": "double"},
]
_CSV_COLUMNS = [
    {"name": "id", "type": "string"},
    {"name": "name", "type": "string"},
    {"name": "temp", "type": "string"},
]


async def test_parquet_schema_is_names_arrow_types_and_the_footer_row_count():
    assert await tabular.schema(PARQUET_URL, "sample.parquet") == {
        "format": "parquet",
        "columns": _PARQUET_COLUMNS,
        "row_estimate": 3,
    }


async def test_parquet_preview_is_the_first_n_rows_typed():
    assert await tabular.preview(PARQUET_URL, "sample.parquet", n=2) == {
        "format": "parquet",
        "columns": _PARQUET_COLUMNS,
        "rows": [{"id": 1, "name": "a", "temp": 29.5}, {"id": 2, "name": "b", "temp": 31.0}],
        "row_estimate": 3,
    }


async def test_csv_schema_and_preview_of_a_whole_small_file_are_not_flagged():
    """A file shorter than the window is read whole: no ``truncated`` key, even when
    fewer rows exist than were asked for."""
    assert await tabular.schema(CSV_URL, "sample.csv") == {
        "format": "csv",
        "columns": _CSV_COLUMNS,
        "row_estimate": None,
    }
    assert await tabular.preview(CSV_URL, "sample.csv", n=20) == {
        "format": "csv",
        "columns": _CSV_COLUMNS,
        "rows": [
            {"id": "1", "name": "a", "temp": "29.5"},
            {"id": "2", "name": "b", "temp": "31.0"},
            {"id": "3", "name": "c", "temp": "33.2"},
        ],
        "row_estimate": None,
    }


async def test_an_empty_csv_has_no_columns_and_no_rows(tmp_path):
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    assert await tabular.schema(empty.as_uri(), "empty.csv") == {
        "format": "csv",
        "columns": [],
        "row_estimate": None,
    }
    assert await tabular.preview(empty.as_uri(), "empty.csv", n=5) == {
        "format": "csv",
        "columns": [],
        "rows": [],
        "row_estimate": None,
    }


def _rows_of(size: int) -> tuple[bytes, int]:
    """A CSV of exactly ``size`` bytes ending in a line break, and its data row count."""
    header = b"id,v\n"
    body = b""
    i = 0
    while len(header) + len(body) + len(f"{i},x\n") <= size:
        body += f"{i},x\n".encode()
        i += 1
    pad = size - len(header) - len(body)
    assert pad < len(f"{i},x\n")
    # widen the last row so the file ends on a line break at exactly `size` bytes
    body = body[:-1] + b"x" * pad + b"\n"
    data = header + body
    assert len(data) == size
    return data, i


async def test_a_file_exactly_the_window_size_is_whole_and_one_byte_more_is_cut(tmp_path):
    whole, count = _rows_of(N)
    exact = tmp_path / "exact.csv"
    exact.write_bytes(whole)
    out = await tabular.preview(exact.as_uri(), "exact.csv", n=count + 10)
    assert "truncated" not in out
    assert len(out["rows"]) == count

    over = tmp_path / "over.csv"
    over.write_bytes(whole + b"9")  # a partial row starts past the window
    out = await tabular.preview(over.as_uri(), "over.csv", n=count + 10)
    assert out["truncated"] is True
    assert len(out["rows"]) == count
    assert out["rows"][-1]["id"] == str(count - 1)


async def test_a_full_page_from_a_cut_window_is_not_flagged(tmp_path):
    """``truncated`` means fewer rows than asked for, not "the file goes on": a page
    of exactly ``n`` rows from a long file is complete. Positive control: asking for
    more rows than the window holds is flagged."""
    data, count = _rows_of(N)
    long = tmp_path / "long.csv"
    long.write_bytes(data + b"9,x\n" * 1000)
    full = await tabular.preview(long.as_uri(), "long.csv", n=count)
    assert "truncated" not in full
    assert [r["id"] for r in full["rows"]] == [str(i) for i in range(count)]
    short = await tabular.preview(long.as_uri(), "long.csv", n=count + 1)
    assert short["truncated"] is True
    assert len(short["rows"]) == count


async def test_a_header_wider_than_the_window_is_kept_and_flagged(tmp_path):
    """With no line break in the window there is no whole line to keep: the cut
    header is returned, cut at exactly the window's last byte, and flagged. Positive
    controls: a cut file whose header fits is not flagged by schema, and a whole file
    without a trailing line break is not cut."""
    names = [f"column_{i:06d}" for i in range(N // 10)]
    header = ",".join(names)
    wide = tmp_path / "wide.csv"
    wide.write_text(header + "\n1\n")
    cut = header[:N].split(",")
    assert 1000 < len(cut) < len(names) and cut[-1] != names[len(cut) - 1]
    sch = await tabular.schema(wide.as_uri(), "wide.csv")
    assert sch["truncated"] is True
    assert [c["name"] for c in sch["columns"]] == cut
    pre = await tabular.preview(wide.as_uri(), "wide.csv", n=1)
    assert pre["truncated"] is True and pre["rows"] == []
    assert [c["name"] for c in pre["columns"]] == cut

    long = tmp_path / "long.csv"
    long.write_text("a,b\n" + "1,2\n" * N)
    assert await tabular.schema(long.as_uri(), "long.csv") == {
        "format": "csv",
        "columns": [{"name": "a", "type": "string"}, {"name": "b", "type": "string"}],
        "row_estimate": None,
    }
    bare = tmp_path / "bare.csv"
    bare.write_text("a,b")
    assert await tabular.schema(bare.as_uri(), "bare.csv") == {
        "format": "csv",
        "columns": [{"name": "a", "type": "string"}, {"name": "b", "type": "string"}],
        "row_estimate": None,
    }


async def test_the_sniff_reads_one_byte_past_the_window_and_no_more(tmp_path, monkeypatch):
    """Range reads only: a CSV is never read past ``N + 1`` bytes, however long it is
    (one byte more than the window tells a cut file from one that ends there)."""
    real_open = tabular.fsspec.open
    reads: list = []

    class _Spy:
        def __init__(self, cm):
            self._cm = cm

        def __enter__(self):
            f = self._cm.__enter__()
            real_read = f.read

            def read(size=-1):
                reads.append(size)
                return real_read(size)

            f.read = read
            return f

        def __exit__(self, *exc):
            return self._cm.__exit__(*exc)

    monkeypatch.setattr(tabular.fsspec, "open", lambda *a, **k: _Spy(real_open(*a, **k)))
    big = tmp_path / "big.csv"
    big.write_text("id\n" + "1\n" * (3 * N))
    out = await tabular.preview(big.as_uri(), "big.csv", n=5)
    assert reads == [N + 1]
    assert len(out["rows"]) == 5
    await tabular.schema(big.as_uri(), "big.csv")
    assert reads == [N + 1, N + 1]


async def test_bytes_that_are_not_utf8_are_replaced_not_raised(tmp_path):
    """A Latin-1 file (as some Zenodo CSVs are) reads with U+FFFD in place of each bad
    byte rather than failing. Positive control: the same text in UTF-8 reads exactly."""
    latin = tmp_path / "latin.csv"
    latin.write_bytes("café,n\nnaïve,1\n".encode("latin-1"))
    sch = await tabular.schema(latin.as_uri(), "latin.csv")
    assert [c["name"] for c in sch["columns"]] == ["caf�", "n"]
    pre = await tabular.preview(latin.as_uri(), "latin.csv", n=1)
    assert pre["rows"] == [{"caf�": "na�ve", "n": "1"}]

    utf8 = tmp_path / "utf8.csv"
    utf8.write_bytes("café,n\nnaïve,1\n".encode())
    assert (await tabular.preview(utf8.as_uri(), "utf8.csv", n=1))["rows"] == [
        {"café": "naïve", "n": "1"}
    ]
