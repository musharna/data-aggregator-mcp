"""OpenML — machine-learning datasets (CC-BY/public-domain corpus).

Discovery is NAME-SUBSTRING only: OpenML's stable JSON API filters the dataset
list by `data_name` substring, not free text over descriptions, and returns no
grand total — so we contribute first-page results only (mirror huggingface.py /
omicsdi.py). Resolve attaches the ARFF (md5-verified fetch) and the
auto-converted Parquet (operable: schema/preview/head/sql via the [operate] extra).
"""

from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    Link,
    compact,
    local_id,
    normalize_access,
    year_from,
)

LIST = "https://www.openml.org/api/v1/json/data/list/data_name/{q}/limit/{n}"
RECORD = "https://www.openml.org/api/v1/json/data/{did}"
_LANDING = "https://www.openml.org/d/{did}"
PREFIXES = {"openml"}
# A dataset id is a positive integer (OpenML answers id 0 with 412 code 110, "Please
# provide data_id"); it goes into the URL path.
_DID_RE = re.compile(r"0*[1-9][0-9]*")
DEFAULT_SIZE = 10
MAX_SIZE = 50
MAX_RETRIES = 2
_GET = "GET"
_ACCEPT_JSON = {"Accept": "application/json"}
# A query is one path segment: quote() escapes "/" too.
_SEGMENT_SAFE = ""
_NO_RESULTS = "372"  # a list query that matched nothing
_UNKNOWN_DATASET = "111"  # a record id OpenML has no dataset for
# The record fields resolve reads, by the JSON types OpenML sends for them.
_TEXT_FIELDS = ("url", "parquet_url", "md5_checksum", "upload_date", "description", "licence")
_TEXT_OR_LIST_FIELDS = ("creator", "tag")


def _error_code(resp: httpx.Response) -> str | None:
    """OpenML answers a query it cannot satisfy with ``412`` and
    ``{"error": {"code": ..., "message": ...}}``. Any other status or body has no code."""
    if resp.status_code != 412:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    err = body.get("error") if isinstance(body, dict) else None
    return str(err.get("code")) if isinstance(err, dict) else None


def _is_no_results(resp: httpx.Response) -> bool:
    """A list query that matches nothing is ``412`` code ``372`` ("No results"). Any
    other 412 code (bad filter, ...) is a real error."""
    return _error_code(resp) == _NO_RESULTS


def _is_unknown_dataset(resp: httpx.Response) -> bool:
    """A record id that names no dataset is ``412`` code ``111`` ("Unknown dataset")."""
    return _error_code(resp) == _UNKNOWN_DATASET


def _check_list(body: dict) -> None:
    data = body.get("data")
    datasets = data.get("dataset") if isinstance(data, dict) else None
    if not isinstance(datasets, list) or not all(
        isinstance(d, dict) and type(d.get("did")) is int and isinstance(d.get("name"), str)
        for d in datasets
    ):
        raise _http.UpstreamEnvelopeError(f"no dataset list in {body!r:.200}")


def _check_record(body: dict) -> None:
    desc = body.get("data_set_description")
    if not (
        isinstance(desc, dict)
        and isinstance(desc.get("name"), str)
        and all(isinstance(desc.get(k), str | None) for k in _TEXT_FIELDS)
        and all(isinstance(desc.get(k), str | list | None) for k in _TEXT_OR_LIST_FIELDS)
    ):
        raise _http.UpstreamEnvelopeError(f"no dataset description in {body!r:.200}")


def _str_list(raw: object) -> list[str]:
    """OpenML serialises a one-element list (``tag``, ``creator``) as a bare string;
    ``list()`` of that char-splits it. Normalise both forms to a list of whole values."""
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    return []


def _normalize_list_entry(d: dict) -> DataResource:
    return DataResource(id=f"openml:{d['did']}", source="openml", kind="dataset", title=d["name"])


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    if offset:  # page-1-only (see module docstring)
        return 0, []
    body = await _http.request_json(
        client,
        _GET,
        LIST.format(q=quote(query, safe=_SEGMENT_SAFE), n=min(size, MAX_SIZE)),
        service="OpenML search",
        headers=_ACCEPT_JSON,
        max_retries=MAX_RETRIES,
        # A 404 here means the list endpoint moved — an outage, not "no datasets".
        # OpenML's real "no match" is HTTP 412 with its error code 372.
        no_content_returns={"data": {"dataset": []}},
        empty_answer=_is_no_results,
        expect=dict,
        check=_check_list,
    )
    datasets = body["data"]["dataset"]
    return len(datasets), [compact(_normalize_list_entry(d)) for d in datasets]


def _last_segment(url: str) -> str:
    return url.rpartition("/")[2]


def _parquet_name(url: str, did: str) -> str:
    tail = _last_segment(url)
    return tail if tail.endswith((".pq", ".parquet")) else f"dataset_{did}.pq"


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    did = local_id(resource_id, "openml", strip=True)
    if not _DID_RE.fullmatch(did):
        raise NotFoundError(f"malformed OpenML id {resource_id!r}")
    # A zero-padded id names the same dataset, but OpenML builds `parquet_url` from the
    # id as sent: /data/0061 answers with a dataset_0061.pq that is a 404.
    did = str(int(did))
    body = await _http.request_json(
        client,
        _GET,
        RECORD.format(did=did),
        service="OpenML resolve",
        headers=_ACCEPT_JSON,
        max_retries=MAX_RETRIES,
        # OpenML's "no such dataset" is HTTP 412 code 111; the helper hands that answer
        # back as `no_content_returns`.
        no_content_returns=None,
        empty_answer=_is_unknown_dataset,
        expect=dict,
        check=_check_record,
    )
    if body is None:
        raise NotFoundError(f"OpenML has no dataset {did}")
    desc = body["data_set_description"]

    files: list[FileEntry] = []
    arff_url = desc.get("url")
    if arff_url:
        md5 = desc.get("md5_checksum")
        files.append(
            FileEntry(
                name=_last_segment(arff_url),
                url=arff_url,
                mime="text/plain",
                checksum=f"md5:{md5}" if md5 else None,
                source="openml",
            )
        )
    pq_url = desc.get("parquet_url")
    if pq_url:
        files.append(
            FileEntry(
                name=_parquet_name(pq_url, did),
                url=pq_url,
                mime="application/parquet",
                source="openml-parquet",
            )
        )

    return DataResource(
        id=f"openml:{did}",
        source="openml",
        kind="dataset",
        title=desc["name"],
        description=desc.get("description"),
        creators=[Creator(name=n) for n in _str_list(desc.get("creator"))],
        year=year_from(desc.get("upload_date")),
        license=desc.get("licence"),
        access=normalize_access("open"),
        subjects=_str_list(desc.get("tag")),
        last_updated=desc.get("upload_date"),
        files=files,
        links=[Link(rel="landing_page", target_id=_LANDING.format(did=did))],
    )
