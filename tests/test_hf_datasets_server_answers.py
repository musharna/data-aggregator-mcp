"""Live datasets-server answers, captured 2026-10-02, and the code read against them.

``GET https://datasets-server.huggingface.co/parquet?dataset=<id>`` answers:

- 200 with ``parquet_files`` (each entry: ``dataset``, ``config``, ``split``, ``url``,
  ``filename``, ``size``), ``pending``, ``failed`` and ``partial``. The list is not
  paginated (nyu-mll/glue 34 files, cais/mmlu 176, wikimedia/wikipedia 555,
  allenai/c4 1,006).
- a split HF converted only in part (its first 5 GB) sits under ``partial-<split>`` in
  the URL while ``split`` still says ``<split>`` and ``filename`` says nothing
  (allenai/c4: 825 of 1,006 files; alexandrainst/da-wit: all 8).
- 401 ``ExternalUnauthenticatedError`` for a gated, private or missing dataset
  (bigcode/the-stack, whose Hub record resolves).
- 501 ``DatasetWithScriptNotSupportedError`` for a script dataset
  (bookcorpus/bookcorpus), ``TooBigContentError`` for a file list over 10 MB
  (HuggingFaceFW/fineweb).
- a split of over 10,000 files sits under ``<split>-part<n>`` directories, each
  numbering its files from ``0000.parquet`` (Lichess/standard-chess-games: 26,138
  files in ``train-part0`` to ``train-part2``).
- 404 ``RenamedDatasetError`` for a dataset's old name (``imdb``, which the Hub
  redirects to stanfordnlp/imdb).
"""

import os

import httpx
import pytest

from data_aggregator_mcp import hf_datasets_server, huggingface
from data_aggregator_mcp.errors import NotFoundError

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_CONVERTED = "https://huggingface.co/datasets/allenai/c4/resolve/refs%2Fconvert%2Fparquet"

# allenai/c4, verbatim: its first file (config "af", converted in part) and the first
# file of config "am" (converted whole).
_C4 = {
    "parquet_files": [
        {
            "dataset": "allenai/c4",
            "config": "af",
            "split": "train",
            "url": f"{_CONVERTED}/af/partial-train/0000.parquet",
            "filename": "0000.parquet",
            "size": 303312530,
        },
        {
            "dataset": "allenai/c4",
            "config": "am",
            "split": "train",
            "url": f"{_CONVERTED}/am/train/0000.parquet",
            "filename": "0000.parquet",
            "size": 252105372,
        },
    ],
    "pending": [],
    "failed": [
        {"kind": "config-parquet", "dataset": "allenai/c4", "config": "multilingual", "split": None}
    ],
    "partial": True,
}

# Lichess/standard-chess-games, verbatim: the first file of two of the three parts HF
# split its 26,138-file train split into.
_LICHESS = {
    "parquet_files": [
        {
            "dataset": "Lichess/standard-chess-games",
            "config": "default",
            "split": "train",
            "url": "https://huggingface.co/datasets/Lichess/standard-chess-games/resolve/"
            f"refs%2Fconvert%2Fparquet/default/train-part{n}/0000.parquet",
            "filename": "0000.parquet",
            "size": size,
        }
        for n, size in ((0, 37303902), (1, 167375975))
    ],
    "pending": [],
    "failed": [],
    "partial": False,
}

# The 401 (bigcode/the-stack) and 501 (bookcorpus/bookcorpus) bodies, verbatim.
_GATED = {
    "error": "The dataset does not exist, or is not accessible without authentication "
    "(private or gated). Please check the spelling of the dataset name or retry with "
    "authentication."
}
_SCRIPT = {
    "error": "The dataset viewer doesn't support this dataset because it runs arbitrary "
    "Python code. You can convert it to a Parquet data-only dataset by using the "
    "convert_to_parquet CLI from the datasets library. See: "
    "https://huggingface.co/docs/datasets/main/en/cli#convert-to-parquet"
}


@_live_only
@pytest.mark.parametrize("ds_id", ["bigcode/the-stack", "bookcorpus/bookcorpus"])
@pytest.mark.asyncio
async def test_live_gated_and_script_datasets_have_no_view_and_resolve_clean(ds_id):
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        with pytest.raises(NotFoundError, match=r"has no readable converted view"):
            await hf_datasets_server.parquet_files(c, ds_id)
        r = await huggingface.resolve(c, f"hf:{ds_id}")
    assert r.id == f"hf:{ds_id}"
    assert r.errors == {}  # an answer, not a failed lookup: the record is cacheable


@_live_only
@pytest.mark.asyncio
async def test_live_a_partial_conversion_is_named_partial():
    async with httpx.AsyncClient(timeout=60) as c:
        files = await hf_datasets_server.parquet_files(c, "alexandrainst/da-wit")
    assert files
    assert all("/partial-" in f.name and f.name.endswith(".parquet") for f in files)
    # Positive control: a whole conversion is named by its plain split.
    async with httpx.AsyncClient(timeout=60) as c:
        whole = await hf_datasets_server.parquet_files(c, "stanfordnlp/imdb")
    assert sorted(f.name for f in whole) == [
        "plain_text/test/0000.parquet",
        "plain_text/train/0000.parquet",
        "plain_text/unsupervised/0000.parquet",
    ]
