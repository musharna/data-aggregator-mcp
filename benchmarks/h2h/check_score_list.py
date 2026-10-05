"""Check ``run.score_list`` on hand-made run directories.

    python3 benchmarks/h2h/check_score_list.py

Seen to fail on two mutants of ``run.py``: matching ids as substrings (GSE123 inside
GSE1234) and dropping the ``zenodo.org/records/<n>`` alias.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run  # noqa: E402

TASK = {
    "id": "X",
    "kind": "list",
    "studies": [
        {"key": "S1", "ids": ["GSE123", "PRJNA9"]},
        {"key": "S2", "ids": ["10.5281/zenodo.555"]},
        {"key": "S3", "ids": ["PXD000001"]},
        {"key": "S4", "ids": ["zenodo:4895080"]},
    ],
    "optional": [{"key": "B1", "ids": ["KU212370"]}],
}
ADJUDICATION = {"X": {"add": [{"key": "A1", "ids": ["E-MTAB-7"]}], "reject": ["GSE999"]}}


def run_dir(listed: list | str) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "out").mkdir()
    body = listed if isinstance(listed, str) else json.dumps(listed)
    (d / "out" / "datasets.json").write_text(body)
    return d


def main() -> None:
    r = run.score_list(
        TASK,
        run_dir(
            [
                {"id": "GSE123", "archive": "GEO", "same_as": ["PRJNA9"]},  # S1
                {"id": "PRJNA9", "archive": "SRA"},  # S1 again: a duplicate
                {"id": "https://zenodo.org/records/555", "archive": "Zenodo"},  # S2
                {"id": "GSE1234", "archive": "GEO"},  # not S1: whole tokens only
                {"id": "doi:10.1/x", "archive": "Dryad"},  # unknown: pending
                {"id": "GSE999", "archive": "GEO"},  # rejected in adjudication
                {"id": "E-MTAB-7", "archive": "ArrayExpress"},  # added in adjudication
                {"id": "KU212370", "archive": "GenBank"},  # borderline: relevant, no recall
                {"id": "10.5281/zenodo.4895080", "archive": "Zenodo"},  # S4, keyed zenodo:N
            ]
        ),
        ADJUDICATION,
    )
    assert r["json_ok"] and r["listed"] == 9 and r["key_size"] == 5, r
    assert r["found_keys"] == ["A1", "S1", "S2", "S4"] and r["recall"] == 0.8, r
    assert r["duplicates"] == 1, r
    assert [p["id"] for p in r["pending"]] == ["GSE1234", "doi:10.1/x"], r
    assert r["relevant"] == 6 and r["irrelevant"] == 1 and r["precision"] == 0.857, r
    # A file that is not a JSON list scores as an empty list, not as an error.
    bad = run.score_list(TASK, run_dir("not json"), ADJUDICATION)
    assert not bad["json_ok"] and bad["recall"] == 0.0 and bad["listed"] == 0, bad
    print("OK")


if __name__ == "__main__":
    main()
