"""MassIVE — UCSD's mass-spectrometry repository (GNPS metabolomics and proteomics),
searched natively.

The omicsdi source reaches MassIVE only through OmicsDI's copy, and DataCite holds every
MassIVE DOI (46,233; 2026-10-12) with its title alone: no description, no keywords. The
round-3 benchmark's coral study MSV000101299 (T5 S15) is titled "GNPS - Resilience of
Tropical Eastern Pacific corals" and names the marine heatwave only in its description,
so no source could match it to a heat-stress search.

The website's dataset query (``QueryDatasets``) searches titles, descriptions, keywords
and species, and answers with whole rows and a count. It ORs the words of a query ("coral
heatwave" 140 = "coral" 138 + "heatwave" 3; "AND" is one more word), matches each as a
substring ("heat" 264, of which 169 only say "wheat"), and cannot sort (oldest first). So
each word is counted first (one row per request), the rarest word's matches are read in
one request, and a dataset is kept when its title, description, keywords and species name
every word as the server reads one (``_relevance``: whole words, a word also naming its
plural). Past ``MAX_ROWS`` matches of the rarest word, its newest ``MAX_ROWS`` are read.

``resolve`` adds the DOI and FTP location from ``MassiveServlet``. Discovery-only: the
files are on FTP, which fetch does not speak.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp._relevance import _names, query_terms
from data_aggregator_mcp._relevance import _normalize as _normalize_words
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator, DataResource, Link, compact, local_id, year_from

API = "https://massive.ucsd.edu/ProteoSAFe/QueryDatasets"
INFO = "https://massive.ucsd.edu/ProteoSAFe/MassiveServlet"
_LANDING = "https://massive.ucsd.edu/ProteoSAFe/dataset.jsp?accession={id}"
_GET = "GET"
PREFIXES = {"massive"}
_ID_RE = re.compile(r"MSV[0-9]{9}")
_PX_RE = re.compile(r"PXD[0-9]{6}")
DEFAULT_SIZE = 10
MAX_SIZE = 50
# A dataset must name every word of the query, so the router names this source when a
# multi-word search empties it.
REQUIRES_EVERY_WORD = True
# A substring of one or two letters is in nearly every row. Such a word is still required
# of every dataset kept; it is sent only when it is all there is.
_MIN_SENT = 3
# The rows read for the rarest word: "metabolomics" is 2,778 rows, 7.5 MB, 3.3 s in one
# request (2026-10-12).
MAX_ROWS = 5000
DEFAULT_TIMEOUT = 60.0
# Lists come as one string joined with "###": keywords, species.
_SEP = "###"
# "Pocillopora verrucosa (NCBITaxon:203993)": the taxon id in brackets after the name.
_TAXON_SUFFIX = re.compile(r"\s*\(NCBITaxon:[0-9]+\)$")
_TEXT_FIELDS = ("title", "description", "keywords", "species_resolved")
_STR_FIELDS = ("description", "keywords", "species_resolved", "create_time", "px")


def _is_row(r: object) -> bool:
    """A dataset row, with every field ``_normalize`` reads at the type it reads it as."""
    return (
        isinstance(r, dict)
        and isinstance(r.get("dataset"), str)
        and _ID_RE.fullmatch(r["dataset"]) is not None
        and isinstance(r.get("title"), str)
        and all(isinstance(r.get(f), str | None) for f in _STR_FIELDS)
        and isinstance(r.get("pis"), list | None)
        and all(isinstance(p, dict) for p in r.get("pis") or [])
    )


def _check(body: dict) -> None:
    count, rows = body.get("total_rows"), body.get("row_data")
    if not (
        isinstance(count, str)
        and count.isdigit()
        and isinstance(rows, list)
        and all(_is_row(r) for r in rows)
    ):
        raise _http.UpstreamEnvelopeError(f"no MassIVE dataset list in {body!r:.200}")


async def _query(
    client: httpx.AsyncClient, text: str, *, size: int, offset: int, service: str
) -> tuple[int, list[dict]]:
    """(the count of datasets matching ``text``, the rows from ``offset``, oldest first)."""
    body = await _http.request_json(
        client,
        _GET,
        API,
        service=service,
        params={
            "pageSize": size,
            "offset": offset,
            "query": json.dumps({"full_search_input": text}),
        },
        expect=dict,
        check=_check,
        timeout=DEFAULT_TIMEOUT,
    )
    return int(body["total_rows"]), body["row_data"]


def _value(r: dict, field: str) -> str | None:
    """A text field, None when absent, empty or the string "null" (a 2014 row's
    description is "null")."""
    v = r.get(field)
    return v.strip() if isinstance(v, str) and v.strip() not in ("", "null") else None


def _list(r: dict, field: str) -> list[str]:
    return [p.strip() for p in (_value(r, field) or "").split(_SEP) if p.strip()]


def _species(r: dict) -> list[str]:
    return list(dict.fromkeys(_TAXON_SUFFIX.sub("", s) for s in _list(r, "species_resolved")))


def _normalize(r: dict) -> DataResource:
    acc = r["dataset"]
    px = _value(r, "px")
    return DataResource(
        id=f"massive:{acc}",
        source="massive",
        kind="study",
        title=r["title"],
        description=_value(r, "description"),
        year=year_from(r.get("create_time")),
        creators=[Creator(name=p["name"]) for p in r.get("pis") or [] if p.get("name")],
        accessions=[acc, px] if px and _PX_RE.fullmatch(px) else [acc],
        organism=_species(r),
        subjects=_list(r, "keywords"),
        links=[Link(rel="landing_page", target_id=_LANDING.format(id=acc))],
    )


def _text(r: dict) -> str:
    return _normalize_words(" ".join(v for f in _TEXT_FIELDS if (v := _value(r, f))))


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    """MassIVE datasets naming every word of ``query``, newest first: (total, compact
    resources)."""
    terms = query_terms(query)
    if not terms:
        raise ValueError(f"MassIVE search needs a word to look for; {query!r} has none")
    sent = [t for t in terms if len(t) >= _MIN_SENT] or terms
    counts = await asyncio.gather(
        *(_query(client, t, size=1, offset=0, service="MassIVE search") for t in sent)
    )
    total, term = min((c[0], t) for c, t in zip(counts, sent, strict=True))
    if total == 0:
        return 0, []
    n = min(total, MAX_ROWS)
    _, rows = await _query(client, term, size=n, offset=total - n, service="MassIVE search")
    matched = sorted(
        {r["dataset"]: r for r in rows if all(_names(_text(r), t) for t in terms)}.values(),
        key=lambda r: r["dataset"],
        reverse=True,
    )
    capped = min(size, MAX_SIZE)
    return len(matched), [compact(_normalize(r)) for r in matched[offset : offset + capped]]


def _info_check(acc: str) -> Callable[[dict], None]:
    """A check that the servlet answered for ``acc``, its DOI a string or absent."""

    def check(body: dict) -> None:
        if body.get("dataset_id") != acc or not isinstance(body.get("doi"), str | None):
            raise _http.UpstreamEnvelopeError(f"no MassIVE record of {acc} in {body!r:.200}")

    return check


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    acc = local_id(resource_id, "massive", strip=True, upper=True)
    if not _ID_RE.fullmatch(acc):
        raise NotFoundError(f"malformed MassIVE dataset id {resource_id!r}")
    (_, rows), info = await asyncio.gather(
        _query(client, acc, size=5, offset=0, service="MassIVE resolve"),
        _http.request_json(
            client,
            _GET,
            INFO,
            service="MassIVE resolve",
            params={"function": "massiveinformation", "massiveid": acc},
            expect=dict,
            check=_info_check(acc),
            timeout=DEFAULT_TIMEOUT,
            not_found_returns=None,
        ),
    )
    row = next((r for r in rows if r["dataset"] == acc), None)
    if row is None or info is None:
        raise NotFoundError(f"MassIVE has no public dataset {acc}")
    record = _normalize(row)
    doi = _value(info, "doi")
    ftp = _value(info, "ftp")
    return record.model_copy(
        update={
            "doi": doi.removeprefix("doi:") if doi else None,
            "links": record.links + ([Link(rel="data_access", target_id=ftp)] if ftp else []),
        }
    )
