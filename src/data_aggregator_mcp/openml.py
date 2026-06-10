"""OpenML — machine-learning datasets (CC-BY/public-domain corpus).

Discovery is NAME-SUBSTRING only: OpenML's stable JSON API filters the dataset
list by `data_name` substring, not free text over descriptions, and returns no
grand total — so we contribute first-page results only (mirror huggingface.py /
omicsdi.py). Resolve attaches the ARFF (md5-verified fetch) and the
auto-converted Parquet (operable: schema/preview/head/sql via the [operate] extra).
"""

from __future__ import annotations

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
    normalize_access,
)

LIST = "https://www.openml.org/api/v1/json/data/list/data_name/{q}/limit/{n}"
RECORD = "https://www.openml.org/api/v1/json/data/{did}"
_LANDING = "https://www.openml.org/d/{did}"
PREFIXES = {"openml"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


def _normalize_list_entry(d: dict) -> DataResource:
    did = d.get("did")
    return DataResource(
        id=f"openml:{did}",
        source="openml",
        kind="dataset",
        title=d.get("name") or "",
        files=[],
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    if offset:  # page-1-only (see module docstring)
        return 0, []
    body = await _http.request_json(
        client,
        "GET",
        LIST.format(q=quote(query, safe=""), n=min(size, MAX_SIZE)),
        service="OpenML search",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns={"data": {"dataset": []}},
    )
    datasets = ((body or {}).get("data") or {}).get("dataset") or []
    return len(datasets), [compact(_normalize_list_entry(d)) for d in datasets]


def _parquet_name(url: str, did: str) -> str:
    tail = url.rsplit("/", 1)[-1]
    return tail if tail.endswith((".pq", ".parquet")) else f"dataset_{did}.pq"


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    did = resource_id.split(":", 1)[1].strip() if ":" in resource_id else ""
    if not did:
        raise NotFoundError(f"malformed OpenML id {resource_id!r}")
    body = await _http.request_json(
        client,
        "GET",
        RECORD.format(did=did),
        service="OpenML resolve",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
    )
    desc = (body or {}).get("data_set_description")
    if not desc:
        raise NotFoundError(f"OpenML has no dataset {did}")

    files: list[FileEntry] = []
    arff_url = desc.get("url")
    if arff_url:
        md5 = desc.get("md5_checksum") or ""
        files.append(
            FileEntry(
                name=arff_url.rsplit("/", 1)[-1],
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

    creator = desc.get("creator")
    if isinstance(creator, str) and creator:
        creators = [Creator(name=creator)]
    elif isinstance(creator, list):
        creators = [Creator(name=str(c)) for c in creator if c]
    else:
        creators = []
    year = None
    up = desc.get("upload_date") or ""
    if up[:4].isdigit():
        year = int(up[:4])
    return DataResource(
        id=f"openml:{did}",
        source="openml",
        kind="dataset",
        title=desc.get("name") or "",
        description=desc.get("description"),
        creators=creators,
        year=year,
        license=desc.get("licence"),
        access=normalize_access("open"),
        subjects=list(desc.get("tag") or []),
        last_updated=desc.get("upload_date"),
        files=files,
        links=[Link(rel="landing_page", target_id=_LANDING.format(did=did))],
    )
