"""data.gov (US government open-data catalog) — dataset search + resolve + fetch.

The unit is a CKAN *package* (a data.gov dataset): ``search`` + ``resolve`` cover the
catalog, and each package carries a title, a publishing agency, tags, a (frequently
unspecified) licence, and zero or more downloadable resources that ``fetch`` streams.

**Auth.** data.gov retired its keyless CKAN API in favour of a GSA-hosted Catalog API
(``api.gsa.gov/technology/datagov/v3``) that requires a free api.data.gov key. The key
is read from ``DATA_GOV_API_KEY`` (mirroring the optional ``NCBI_API_KEY`` pattern) and
sent as ``X-Api-Key``; absent that, it falls back to the public ``DEMO_KEY``, which is
rate-limited (~30/hour per IP) — fine for light discovery, but heavy use should set a
free key. See ``list_sources`` for the note surfaced to callers.

**Fetch is unverified.** CKAN resources rarely publish a usable checksum (``hash`` is
typically empty or an undeclared algorithm), so ``FileEntry.checksum`` is left None and
fetch runs unverified — the content sniff in ``fetch.py`` still rejects an HTML error
page served as a data file (the HuggingFace / GBIF precedent). A metadata-only package
(no resources) is discovery-only and fails loud on fetch.

Most data.gov packages carry no DOI, so DOI-dedup rarely fires here — the value is
breadth (government / economic / climate / civic data), not cross-source collapse.
"""

from __future__ import annotations

import logging
import os
import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.license_compat import normalize_spdx
from data_aggregator_mcp.models import Creator, DataResource, FileEntry, compact

logger = logging.getLogger(__name__)

_BASE = "https://api.gsa.gov/technology/datagov/v3/action"
SEARCH = f"{_BASE}/package_search"
SHOW = f"{_BASE}/package_show"
LANDING = "https://catalog.data.gov/dataset/{name}"
PREFIXES = {"datagov"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# CKAN short license_ids that normalize_spdx does not recognize but that map
# unambiguously to a canonical SPDX id. Versionless CC ids (e.g. bare "cc-by") are
# deliberately absent — mapping them would fabricate a version the record never stated.
_LICENSE_ID_SPDX = {
    "cc-zero": "CC0-1.0",
    "odc-pddl": "PDDL-1.0",
    "odc-by": "ODC-By-1.0",
    "odc-odbl": "ODbL-1.0",
}
# license_ids that denote openly-reusable / public-domain content (positive evidence
# of open access). US government works are public domain by default ("us-pd").
_OPEN_LICENSE_IDS = {"cc-zero", "us-pd", "other-pd", "odc-pddl", "odc-by", "odc-odbl"}
_OPEN_SPDX = {"PDDL-1.0", "ODC-By-1.0", "ODbL-1.0"}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_DEMO_KEY_WARNED = False


def _api_key() -> str:
    """The api.data.gov key: ``DATA_GOV_API_KEY`` if set, else the public DEMO_KEY
    (rate-limited). The DEMO_KEY path warns once so a rate-limit surprise is traceable."""
    key = os.environ.get("DATA_GOV_API_KEY")
    if key:
        return key
    global _DEMO_KEY_WARNED
    if not _DEMO_KEY_WARNED:
        logger.warning(
            "data.gov: DATA_GOV_API_KEY not set; using the public DEMO_KEY "
            "(~30 req/hour per IP). Get a free key at https://api.data.gov for heavier use."
        )
        _DEMO_KEY_WARNED = True
    return "DEMO_KEY"


def _headers() -> dict[str, str]:
    return {"X-Api-Key": _api_key(), "Accept": "application/json"}


def _clean(text: str | None) -> str | None:
    """CKAN notes may carry light HTML / markdown; reduce to collapsed plain text."""
    if not text:
        return None
    cleaned = _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()
    return cleaned or None


def _year(*vals: str | None) -> int | None:
    for v in vals:
        if v and isinstance(v, str) and len(v) >= 4 and v[:4].isdigit():
            return int(v[:4])
    return None


def _license_and_access(pkg: dict) -> tuple[str | None, str | None]:
    lid = (pkg.get("license_id") or "").strip().lower()
    spdx = (
        normalize_spdx(pkg.get("license_url")) or _LICENSE_ID_SPDX.get(lid) or normalize_spdx(lid)
    )
    is_open = bool(
        (spdx and (spdx.startswith("CC") or spdx in _OPEN_SPDX))
        or lid in _OPEN_LICENSE_IDS
        or lid.startswith("cc-")
    )
    return spdx, ("open" if is_open else None)


def _int_size(v: object) -> int | None:
    """CKAN stores resource ``size`` as a text field, so a populated size arrives as a
    numeric STRING (e.g. ``"12345"``), not an int — coerce it (cf. ena.py) rather than
    dropping it. Non-numeric / absent → None."""
    if isinstance(v, bool):  # bool is an int subclass; a stray True/False is not a size
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _files(pkg: dict) -> list[FileEntry]:
    """CKAN resources with a download url. No reliable checksum (hash is usually empty
    or an undeclared algorithm) → unverified fetch."""
    out: list[FileEntry] = []
    for res in pkg.get("resources") or []:
        url = res.get("url")
        if not url:
            continue
        out.append(
            FileEntry(
                name=res.get("name") or res.get("format") or url.rsplit("/", 1)[-1] or "resource",
                url=url,
                mime=res.get("mimetype") or None,
                size=_int_size(res.get("size")),
                source="datagov",
            )
        )
    return out


def _creators(pkg: dict) -> list[Creator]:
    """The publishing agency is the citation author for a government dataset."""
    org = ((pkg.get("organization") or {}).get("title") or "").strip()
    return [Creator(name=org)] if org else []


def _normalize(pkg: dict) -> DataResource:
    name = pkg.get("name") or pkg.get("id") or ""
    spdx, access = _license_and_access(pkg)
    return DataResource(
        id=f"datagov:{name}",
        source="datagov",
        kind="dataset",
        title=pkg.get("title") or "",
        creators=_creators(pkg),
        year=_year(pkg.get("metadata_created"), pkg.get("metadata_modified")),
        description=_clean(pkg.get("notes")),
        subjects=[t["name"] for t in (pkg.get("tags") or []) if t.get("name")],
        license=spdx,
        access=access,
        last_updated=pkg.get("metadata_modified"),
        links=[],
        files=_files(pkg),
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    body = await _http.request_json(
        client,
        "GET",
        SEARCH,
        service="data.gov search",
        params={"q": query, "rows": str(min(size, MAX_SIZE)), "start": str(offset)},
        headers=_headers(),
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    result = body.get("result") or {}
    total = int(result.get("count") or 0)
    return total, [compact(_normalize(p)) for p in (result.get("results") or [])]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    name = resource_id.split(":", 1)[1] if resource_id.startswith("datagov:") else resource_id
    body = await _http.request_json(
        client,
        "GET",
        SHOW,
        service="data.gov resolve",
        params={"id": name},
        headers=_headers(),
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
    )
    if not body or not body.get("success") or not body.get("result"):
        raise NotFoundError(f"data.gov has no package {name!r}")
    return _normalize(body["result"])
