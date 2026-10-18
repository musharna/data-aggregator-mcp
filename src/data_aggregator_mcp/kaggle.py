"""Kaggle — public datasets, searched natively.

Many machine-learning datasets are deposited only on Kaggle, with no DOI, so no other
source reaches them. In the round-3 benchmark (T4, labelled honey bee images) six key
datasets were held only there (S1 S7 S19 S20 S26, and S5 whose Kaggle DOI names a
version, not the dataset); Kaggle's own search returns all six for "honey bee" or "bee
varroa" (probed 2026-10-17). Kaggle DOIs (10.34740/kaggle/dsv/N) are registered at
DataCite for single versions and link to no dataset ref, so a DOI'd dataset can appear
once from each source. ``collapse_mirrors`` does not fold the two either: it needs a
year, which a search hit from here lacks.

The API is Kaggle's public one (https://github.com/Kaggle/kaggle-api), answered without
an account: ``/api/v1/datasets/list`` searches and ``/api/v1/datasets/view/{owner}/{slug}``
reads one dataset. Search matches every word, each as a prefix ("bee" also finds
"beef"; one word matching nothing empties it), reads no OR or quotes, returns 20 rows a
page whatever size is asked, and reports no total. A dataset that does not exist, or is
private, answers 403 "Permission 'datasets.get' was denied".

Discovery-only: the record names the dataset, its licence and landing page. Kaggle also
serves the whole dataset as one archive to anonymous callers, which is not wired.
"""

from __future__ import annotations

import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.license_compat import normalize_spdx
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    Link,
    Metrics,
    compact,
    local_id,
    year_from,
)

API = "https://www.kaggle.com/api/v1/datasets"
_LANDING = "https://www.kaggle.com/datasets/{ref}"
_GET = "GET"
_ACCEPT_JSON = {"Accept": "application/json"}
PREFIXES = {"kaggle"}
# A dataset ref: owner/slug, each of letters, digits, "-" and "_" (slugs also ".").
_REF_RE = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9._-]+")
# Kaggle answers 20 rows a page whatever page size is asked (pageSize=100: 20).
PAGE = 20
DEFAULT_SIZE = 10
MAX_SIZE = PAGE
# A dataset must hold every word of the query, so the router names this source when a
# multi-word search empties it.
REQUIRES_EVERY_WORD = True
# Kaggle licence names the SPDX normalizer does not read (2026-10-17, 300 hits of 8
# queries). The two database licences name a contents licence too, so stay as written.
_LICENSES = {
    "CC0: Public Domain": "CC0-1.0",
    "GPL 2": "GPL-2.0",
    "GPL 3": "GPL-3.0",
    "GNU Lesser General Public License 3.0": "LGPL-3.0",
}
_NO_LICENSE = "Unknown"
# "Attribution 4.0 International (CC BY 4.0)": the short name ends the long one.
_SHORT_NAME = re.compile(r"\(([^()]{1,40})\)\s*$")


def _opt(value: object, kind: type | tuple[type, ...]) -> bool:
    return value is None or isinstance(value, kind)


def _is_dataset(d: object) -> bool:
    """A public dataset, with every field ``_normalize`` reads at the type it reads it
    as (absent or null is fine)."""
    if not isinstance(d, dict):
        return False
    ref, tags, versions = d.get("ref"), d.get("tags"), d.get("versions")
    return (
        isinstance(ref, str)
        and _REF_RE.fullmatch(ref) is not None
        and isinstance(d.get("title"), str)
        and all(
            _opt(d.get(k), str)
            for k in ("subtitle", "description", "licenseName", "lastUpdated", "creatorName")
        )
        and _opt(d.get("ownerName"), str)
        and all(
            _opt(d.get(k), int) and not isinstance(d.get(k), bool)
            for k in ("downloadCount", "viewCount", "voteCount")
        )
        and (
            tags is None
            or (
                isinstance(tags, list)
                and all(isinstance(t, dict) and _opt(t.get("name"), str) for t in tags)
            )
        )
        and (
            versions is None
            or (
                isinstance(versions, list)
                and all(isinstance(v, dict) and _opt(v.get("creationDate"), str) for v in versions)
            )
        )
    )


def _check_list(body: list) -> None:
    if not all(_is_dataset(d) for d in body):
        raise _http.UpstreamEnvelopeError(f"no Kaggle dataset list in {body!r:.200}")


def _check_dataset(body: dict) -> None:
    if not _is_dataset(body):
        raise _http.UpstreamEnvelopeError(f"no Kaggle dataset in {body!r:.200}")


def _license(name: str | None) -> str | None:
    """The SPDX id of a Kaggle licence name when it has one, else the name as written;
    "Unknown" is no licence."""
    if not name or name.strip() == _NO_LICENSE:
        return None
    if name in _LICENSES:
        return _LICENSES[name]
    short = _SHORT_NAME.search(name)
    # An IGO licence is its own, which the normalizer reads as the plain one.
    candidate = short.group(1) if short else name
    spdx = None if "IGO" in candidate else normalize_spdx(candidate)
    return spdx or name


def _normalize(d: dict) -> DataResource:
    """A dataset from the search list (``description`` is empty there and ``versions``
    absent) or from ``view``. The year is that of its first version, so known only on
    resolve: ``lastUpdated`` dates the latest version."""
    ref = d["ref"]
    creator = d.get("creatorName") or d.get("ownerName")
    first = min(
        (v["creationDate"] for v in d.get("versions") or [] if v.get("creationDate")),
        default=None,
    )
    views, downloads, votes = d.get("viewCount"), d.get("downloadCount"), d.get("voteCount")
    return DataResource(
        id=f"kaggle:{ref}",
        source="kaggle",
        kind="dataset",
        title=d["title"],
        creators=[Creator(name=creator)] if creator else [],
        year=year_from(first),
        description=d.get("description") or d.get("subtitle") or None,
        subjects=[t["name"] for t in d.get("tags") or [] if t.get("name")],
        license=_license(d.get("licenseName")),
        access="open",
        metrics=(
            Metrics(views=views, downloads=downloads, likes=votes)
            if any(v is not None for v in (views, downloads, votes))
            else None
        ),
        last_updated=d.get("lastUpdated"),
        links=[Link(rel="landing_page", target_id=_LANDING.format(ref=ref))],
    )


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
) -> tuple[int, list[DataResource]]:
    """Kaggle datasets holding every word of ``query``: (total, compact resources).

    Kaggle pages by 20 and reports no total, so ``offset`` is read from its page and the
    total is a lower bound: the rows up to this page's end, and one more when the page is
    full, so a cursor goes on to the next page (an empty one ends it)."""
    start = offset - offset % PAGE
    rows = await _http.request_json(
        client,
        _GET,
        f"{API}/list",
        service="Kaggle search",
        params={"search": query, "page": offset // PAGE + 1},
        headers=_ACCEPT_JSON,
        expect=list,
        check=_check_list,
    )
    window = rows[offset - start : offset - start + min(size, MAX_SIZE)]
    total = start + len(rows) + (1 if len(rows) == PAGE else 0)
    return total, [compact(_normalize(d)) for d in window]


def _refused(resp: httpx.Response) -> bool:
    """Kaggle's answer for a dataset that does not exist or is private."""
    return resp.status_code == 403 and "datasets.get" in resp.text


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    ref = local_id(resource_id, "kaggle")
    if not _REF_RE.fullmatch(ref):
        raise NotFoundError(f"malformed Kaggle dataset id {resource_id!r}")
    body = await _http.request_json(
        client,
        _GET,
        f"{API}/view/{ref}",
        service="Kaggle resolve",
        headers=_ACCEPT_JSON,
        expect=dict,
        check=_check_dataset,
        empty_answer=_refused,
        no_content_returns=None,
    )
    if body is None:
        raise NotFoundError(f"Kaggle has no public dataset {ref}")
    if body["ref"].lower() != ref.lower():
        raise NotFoundError(f"Kaggle answered {body['ref']!r} for {ref!r}")
    return _normalize(body)
