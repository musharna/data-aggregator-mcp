"""Shapes of real CSV files that the schema/preview sniff got wrong, each beside the file
it was seen in (probed live 2026-10-02).

- A UTF-8 byte-order mark: 6 of the 25 most recent Zenodo records with a CSV file
  (`q=filetype:csv`) start theirs with one (Excel's "CSV UTF-8" writes it). The sniff
  kept it as part of the first column's name, while DuckDB (`head`/`sql`) drops it.
- A quoted field holding line breaks: in merve/poetry's ``poetry.csv`` (Hugging Face)
  the 64 KB sniff window ends inside a poem, and preview returned the half poem as a
  row, its later columns ``None``.
"""

import itertools
import os

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("fsspec")
pytest.importorskip("pyarrow")

from data_aggregator_mcp import duckquery, tabular

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

# The first two lines of MAP_studies_data_dictionary_20260922.csv (Zenodo 22826870), verbatim.
_MAP_HEAD = (
    b"\xef\xbb\xbfVariable name,Questionnaire / Secondary variable,Variable type,"
    b"Branching logic,Question (English),Answers (English),Question (Ukrainian),"
    b"Answers (Ukrainian),Question (German),Answers (German),MAP-U,MAP-Z (UA),"
    b"MAP-Z (ZH),Comment\r\n"
    b"record_id,,character,,,,,,,,1,1,1,"
    b"Record id for identification of individual participants.\r\n"
)
_MAP_COLUMNS = [
    "Variable name",
    "Questionnaire / Secondary variable",
    "Variable type",
    "Branching logic",
    "Question (English)",
    "Answers (English)",
    "Question (Ukrainian)",
    "Answers (Ukrainian)",
    "Question (German)",
    "Answers (German)",
    "MAP-U",
    "MAP-Z (UA)",
    "MAP-Z (ZH)",
    "Comment",
]


async def test_a_byte_order_mark_is_not_part_of_the_first_column_name(tmp_path):
    """schema and preview named the first column ``'\\ufeffVariable name'`` while head and
    sql, through DuckDB, name it ``'Variable name'``: a name copied from preview into a
    query found no column. Positive control: a file without the mark whose first name
    starts with a non-ASCII letter keeps that letter."""
    bom = tmp_path / "map.csv"
    bom.write_bytes(_MAP_HEAD)
    duck = await duckquery.run_head(bom.as_uri(), "map.csv", n=1, columns=None)
    assert [c["name"] for c in duck["columns"]] == _MAP_COLUMNS
    sch = await tabular.schema(bom.as_uri(), "map.csv")
    assert [c["name"] for c in sch["columns"]] == _MAP_COLUMNS
    pre = await tabular.preview(bom.as_uri(), "map.csv", n=5)
    assert [c["name"] for c in pre["columns"]] == _MAP_COLUMNS
    assert pre["rows"][0]["Variable name"] == "record_id"

    plain = tmp_path / "plain.csv"
    plain.write_bytes("Ünit,value\r\nm,1\r\n".encode())
    assert [c["name"] for c in (await tabular.schema(plain.as_uri(), "plain.csv"))["columns"]] == [
        "Ünit",
        "value",
    ]
    assert (await tabular.preview(plain.as_uri(), "plain.csv", n=5))["rows"] == [
        {"Ünit": "m", "value": "1"}
    ]


def _poem_rows(pad: int, count: int) -> list[str]:
    return [f'{i},"line one{"x" * pad}\nline two {i}",{i}\n' for i in range(count)]


async def test_preview_never_returns_a_row_cut_inside_a_quoted_field(tmp_path):
    """The window is cut at its last line break so no half row is parsed, but a line
    break inside a quoted field does not end a row: when the cut fell there, the half
    row came back as data (``text`` cut short, ``n`` None). Every cut position is tried
    by varying the row length. Positive control: every row that ends inside the window
    comes back, whole and line break included, and the short page is flagged."""
    header = "id,text,n\n"
    for pad in range(40):
        rows = _poem_rows(pad, 4000)
        ends = list(itertools.accumulate(len(r) for r in rows))
        fit = sum(1 for e in ends if len(header) + e <= tabular._CSV_SNIFF_BYTES)
        f = tmp_path / f"p{pad}.csv"
        f.write_text(header + "".join(rows))
        out = await tabular.preview(f.as_uri(), f.name, n=4000)
        assert out["truncated"] is True
        assert len(out["rows"]) == fit
        for i, row in enumerate(out["rows"]):
            assert row == {"id": str(i), "text": f"line one{'x' * pad}\nline two {i}", "n": str(i)}


@_live_only
async def test_live_a_zenodo_csv_with_a_byte_order_mark_names_columns_as_sql_does():
    url = "https://zenodo.org/api/records/22826870/files/MAP_studies_data_dictionary_20260922.csv/content"
    name = "MAP_studies_data_dictionary_20260922.csv"
    sch = await tabular.schema(url, name)
    duck = await duckquery.run_head(url, name, n=1, columns=None)
    assert [c["name"] for c in sch["columns"]] == [c["name"] for c in duck["columns"]]
    assert [c["name"] for c in sch["columns"]] == _MAP_COLUMNS


@_live_only
async def test_live_preview_of_a_csv_cut_inside_a_poem_returns_whole_rows():
    url = (
        "https://huggingface.co/datasets/merve/poetry/resolve/"
        "956ae34eaf5d5086454b0667c7aa441fdd1fe0f7/poetry.csv"
    )
    out = await tabular.preview(url, "poetry.csv", n=500)
    assert out["truncated"] is True
    assert [c["name"] for c in out["columns"]] == ["author", "content", "poem name", "age", "type"]
    assert len(out["rows"]) >= 10
    assert all(None not in row.values() for row in out["rows"]), out["rows"][-1]
