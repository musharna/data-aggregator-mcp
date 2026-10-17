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
A word's count is kept ten minutes, for the next search that holds it.

``resolve`` adds the DOI and FTP location from ``MassiveServlet``. Discovery-only: the
files are on FTP, which fetch does not speak.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp._cache import MISS, TTLCache
from data_aggregator_mcp._relevance import _names, holding_words, query_terms
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
# A word's count, kept ten minutes. An agent's searches rephrase one another: 235 of the
# 356 counts of an 83-search session repeated a word already counted, and under that
# load MassIVE took up to 104 s a search (3.75 s alone) and answered 429 six times
# (2026-10-17). The rows are read with a fresh count, so a kept count costs a request,
# never a dataset; a kept zero hides a new dataset for at most the ten minutes.
_COUNTS = TTLCache(maxsize=4096, ttl=600.0)
# Its datasets naming only some of the words are searched too, below those naming every
# one (``router._source_streams``); matched here, so asked for with some_words. Each word
# is a request, and MassIVE answers 429 under load, so the fill reads only the rows of
# the 3 rarest words that match anything, and only while fewer than a page of datasets
# name every word.
ANY_WORD_FILL = True
FILLS_IN_CODE = True
_FILL_WORDS = 3
_FILL_BELOW = DEFAULT_SIZE
# Requests in flight, by what they ask: the every-word search and its fill run at once
# and ask for the same counts and the rarest word's rows, which are read once for both.
_PENDING: dict[tuple, asyncio.Task] = {}
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


def _forget(key: tuple, task: asyncio.Task) -> None:
    if _PENDING.get(key) is task:
        del _PENDING[key]
    if not task.cancelled():
        task.exception()  # retrieved: every caller awaiting it may have been cancelled


async def _once(key: tuple, make: Callable[[], Awaitable[Any]]) -> Any:
    """``make()``, awaited once for every caller asking for ``key`` while it runs."""
    loop = asyncio.get_running_loop()
    task = _PENDING.get(key)
    if task is None or task.get_loop() is not loop:
        task = _PENDING[key] = loop.create_task(make())
        task.add_done_callback(functools.partial(_forget, key))
    # One caller cut off (the router's deadline) must not cancel the others' request.
    return await asyncio.shield(task)


async def _count(client: httpx.AsyncClient, word: str) -> int:
    """The count of datasets matching ``word``, kept ``_COUNTS.ttl`` seconds."""
    n = _COUNTS.get(word)
    if n is MISS:
        n = await _once(("count", word), lambda: _counted(client, word))
    return n


async def _counted(client: httpx.AsyncClient, word: str) -> int:
    n, _ = await _query(client, word, size=1, offset=0, service="MassIVE search")
    _COUNTS.set(word, n)
    return n


async def _shared_rows(client: httpx.AsyncClient, word: str, total: int) -> list[dict]:
    return await _once(("rows", word, total), lambda: _newest_rows(client, word, total))


async def _newest_rows(client: httpx.AsyncClient, word: str, total: int) -> list[dict]:
    """The newest ``MAX_ROWS`` rows matching ``word``, read where ``total``, its count,
    puts them. The answer counts afresh: when that differs (the count was kept from an
    earlier search), they are read again where the fresh count puts them."""
    n = min(total, MAX_ROWS)
    fresh, rows = await _query(client, word, size=n, offset=total - n, service="MassIVE search")
    _COUNTS.set(word, fresh)
    if fresh not in (total, 0):
        n = min(fresh, MAX_ROWS)
        _, rows = await _query(client, word, size=n, offset=fresh - n, service="MassIVE search")
    return rows


def _value(r: dict, field: str) -> str | None:
    """A text field, None when absent, empty or the string "null" (a 2014 row's
    description is "null")."""
    v = r.get(field)
    if not isinstance(v, str) or v.strip() == "null":
        return None
    return v.strip() or None


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


def _newest_first(rows: list[dict]) -> list[dict]:
    return sorted(
        {r["dataset"]: r for r in rows}.values(), key=lambda r: r["dataset"], reverse=True
    )


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
    some_words: bool = False,
) -> tuple[int, list[DataResource]]:
    """MassIVE datasets naming every word of ``query``, newest first; with
    ``some_words``, those naming only some of them, the ones naming more first, while
    fewer than ``_FILL_BELOW`` name every word: (total, compact resources)."""
    terms = query_terms(query)
    if not terms:
        raise ValueError(f"MassIVE search needs a word to look for; {query!r} has none")
    sent = [t for t in terms if len(t) >= _MIN_SENT] or terms
    counts = await asyncio.gather(*(_count(client, t) for t in sent))
    # gather() returns one count per word sent, so strict= cannot change behaviour.
    rarest = sorted(zip(counts, sent, strict=True))  # pragma: no mutate
    total, term = rarest[0]

    def holds(r: dict, t: str) -> bool:
        return _names(_text(r), t)

    first = await _shared_rows(client, term, total) if total else []
    every = holding_words(_newest_first(first), holds, terms)
    if not some_words:
        matched = every
    elif len(every) >= _FILL_BELOW:
        matched = []
    else:
        # The rarest word's rows are read already when it matches anything.
        read = [(n, t) for n, t in rarest if n][:_FILL_WORDS]
        pages = await asyncio.gather(*(_shared_rows(client, t, n) for n, t in read[bool(total) :]))
        rows = _newest_first([*first, *(r for page in pages for r in page)])
        matched = holding_words(rows, holds, terms, some_words=True)
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
