"""NGDC (China National Center for Bioinformation) — BioProjects deposited in China.

Studies whose reads sit in the Genome Sequence Archive (GSA, CRA accessions) are
registered as NGDC BioProjects (PRJCA), which NCBI does not mirror. In the round-2
benchmark (2026-10-06) five keyed studies across three tasks were held only there, and
no arm found any of them, web search included.

Discovery-only: the record carries the GSA accessions (CRA) and the landing page; raw
reads download from GSA, which is not wired.

The API is the JSON endpoint behind NGDC's own search portal
(``/search/api/specific``, read from https://ngdc.cncb.ac.cn/search/specific); NGDC
publishes no documentation for it. Its BioProject index also mirrors INSDC projects
(PRJNA, PRJEB, PRJDB), 116 of 124 hits for "axolotl", which the omics source already
covers from NCBI. ``attrs.Center`` names where a project was deposited (SRA, ERA, DRA,
or GSA for NGDC's own), and ANDing ``attrs.Center:"GSA"`` onto the query keeps NGDC's
own projects with an exact total (probed 2026-10-06: "axolotl" 124 → 8). A failed query
answers HTTP 200 with ``{"code": "500", "result": null}`` (a date-range clause did).
"""

from __future__ import annotations

import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp._relevance import with_plurals
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import DataResource, Link, compact, local_id, year_from

SEARCH = "https://ngdc.cncb.ac.cn/search/api/specific"
_GET = "GET"
_ACCEPT_JSON = {"Accept": "application/json"}
PREFIXES = {"ngdc"}
# NGDC's own BioProjects: PRJCA + digits. The accession goes into a query clause.
_ACC_RE = re.compile(r"PRJCA[0-9]+", re.IGNORECASE)
_NATIVE = 'attrs.Center:"GSA"'
_TYPE_SEP = re.compile(r" {2,}")
DEFAULT_SIZE = 10
MAX_SIZE = 50
# Matches words exactly: "leopard" 13 native projects, "leopards" 1, "(leopard OR
# leopards)" 13 (probed 2026-10-06).
QUERY_PLURALS = True


def _is_project(p: object) -> bool:
    """An NGDC BioProject, with every field ``_normalize`` reads at the type it reads it
    as (absent or null is fine)."""
    if not isinstance(p, dict):
        return False
    acc, title, attrs = p.get("id"), p.get("title"), p.get("attrs")
    species = p.get("species")
    return (
        isinstance(acc, str)
        and _ACC_RE.fullmatch(acc) is not None
        and isinstance(title, str)
        and isinstance(p.get("description"), str | None)
        and (
            species is None
            or (isinstance(species, list) and all(isinstance(s, str) for s in species))
        )
        and (attrs is None or isinstance(attrs, dict))
    )


def _check_page(body: dict) -> None:
    """``code`` "200" and a ``result.data`` with an int total and a list of projects.
    The error envelope (``code`` "500", ``result`` null) comes with HTTP 200."""
    result = body.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    total = data.get("recordsTotal") if isinstance(data, dict) else None
    rows = data.get("data") if isinstance(data, dict) else None
    if not (
        body.get("code") == "200"
        and type(total) is int
        and isinstance(rows, list)
        and all(_is_project(p) for p in rows)
    ):
        raise _http.UpstreamEnvelopeError(f"no NGDC BioProject list in {body!r:.200}")


def _strings(value: object) -> list[str]:
    """A string or a list of strings from ``attrs``, as a list; anything else is none."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _normalize(p: dict) -> DataResource:
    attrs = p.get("attrs") or {}
    acc = p["id"].upper()
    released = attrs.get("ReleaseDate") if isinstance(attrs.get("ReleaseDate"), str) else None
    # The record's ``url`` is this page for every native project; built here from the
    # accession, it cannot point anywhere else.
    landing = f"https://ngdc.cncb.ac.cn/bioproject/browse/{acc}"
    return DataResource(
        id=f"ngdc:{acc}",
        source="ngdc",
        kind="study",
        title=p["title"],
        description=p.get("description") or None,
        year=year_from(released),
        # The GSA read sets (CRA) the project holds.
        accessions=_strings(attrs.get("CrasAcc")),
        organism=_strings(p.get("species")),
        # Several data types come as one string joined by two spaces: "Transcriptome or
        # Gene expression  Raw sequence reads".
        subjects=[
            t for v in _strings(attrs.get("DataType")) for t in _TYPE_SEP.split(v) if t.strip()
        ],
        last_updated=released,
        links=[Link(rel="landing_page", target_id=landing)],
    )


async def _query(client: httpx.AsyncClient, q: str, *, start: int, length: int, service: str):
    body = await _http.request_json(
        client,
        _GET,
        SEARCH,
        service=service,
        params={"db": "bioproject", "q": q, "sort": "desc", "start": start, "length": length},
        headers=_ACCEPT_JSON,
        expect=dict,
        check=_check_page,
    )
    data = body["result"]["data"]
    return data["recordsTotal"], data["data"]


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
    plurals: bool = True,
) -> tuple[int, list[DataResource]]:
    """NGDC's own BioProjects matching ``query``: (total, compact resources). The
    endpoint pages by ``start``/``length``, so ``offset`` is sent as is."""
    q = with_plurals(query) if plurals else query
    total, rows = await _query(
        client,
        f"({q}) AND {_NATIVE}",
        start=offset,
        length=min(size, MAX_SIZE),
        service="NGDC search",
    )
    return total, [compact(_normalize(p)) for p in rows]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    acc = local_id(resource_id, "ngdc", strip=True, upper=True)
    if not _ACC_RE.fullmatch(acc):
        raise NotFoundError(f"malformed NGDC BioProject id {resource_id!r}")
    _total, rows = await _query(
        client, f'attrs.Accession:"{acc}"', start=0, length=5, service="NGDC resolve"
    )
    for p in rows:
        if p["id"].upper() == acc:
            return _normalize(p)
    raise NotFoundError(f"NGDC has no BioProject {acc}")
