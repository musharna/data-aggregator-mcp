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
- 404 ``RenamedDatasetError`` for a dataset's old name (``imdb``, which the Hub
  redirects to stanfordnlp/imdb).
"""

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
