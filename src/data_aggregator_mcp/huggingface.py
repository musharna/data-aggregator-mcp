"""HuggingFace Hub *datasets* as a discovery + fetch source."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from data_aggregator_mcp import _http, hf_datasets_server
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator, DataResource, FileEntry, Metrics, compact, local_id

API = "https://huggingface.co/api/datasets"
FILE_BASE = "https://huggingface.co/datasets"
PREFIXES = {"hf"}
# A dataset id is [owner/]name of ASCII letters, digits, "_", "." and "-", each part
# starting alphanumeric; the Hub forbids "..". It goes into the URL path.
_DATASET_ID_RE = re.compile(r"[A-Za-z0-9][\w.-]*(?:/[A-Za-z0-9][\w.-]*)?", re.ASCII)
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2

# Every field ``_normalize`` reads, at the type it reads it as; absent or null is fine.
_FIELDS: dict[str, type] = {
    "author": str,
    "createdAt": str,
    "lastModified": str,
    "cardData": dict,
    "downloads": int,
    "likes": int,
    "tags": list,
    "siblings": list,
}

logger = logging.getLogger(__name__)


def _is_dataset_id(ds_id: str) -> bool:
    return bool(_DATASET_ID_RE.fullmatch(ds_id)) and ".." not in ds_id


def _is_dataset(d: object) -> bool:
    """A well-formed dataset id, and every field ``_normalize`` reads at its type."""
    if not (isinstance(d, dict) and isinstance(d.get("id"), str) and _is_dataset_id(d["id"])):
        return False
    return (
        all(d.get(k) is None or isinstance(d[k], kind) for k, kind in _FIELDS.items())
        and all(isinstance(t, str) for t in d.get("tags") or [])
        and all(
            isinstance(s, dict) and isinstance(s.get("rfilename"), str)
            for s in d.get("siblings") or []
        )
    )


def _check_dataset(body: dict) -> None:
    if not _is_dataset(body):
        raise _http.UpstreamEnvelopeError(f"no HuggingFace dataset in {body!r:.200}")


def _check_datasets(body: list) -> None:
    if not all(_is_dataset(d) for d in body):
        raise _http.UpstreamEnvelopeError(f"no HuggingFace dataset list in {body!r:.200}")


def _license(tags: list[str], card: dict | None) -> str | None:
    for t in tags:
        if t.startswith("license:"):
            return t.split(":", 1)[1] or None
    # The card is the uploader's YAML: a licence id or a list of them (live,
    # priyank-m/SROIE_2019_text_recognition: `license: []`). The Hub tags each listed
    # licence, so the loop above answers for any list that is not empty.
    lic = (card or {}).get("license")
    if isinstance(lic, list):
        lic = lic[0] if lic else None
    return lic if isinstance(lic, str) and lic else None


def _metrics(d: dict[str, Any]) -> Metrics | None:
    """Pull HF's downloads/likes. Returns None when neither is present so the
    field stays absent rather than a zero-filled object."""
    dls, likes = d.get("downloads"), d.get("likes")
    if dls is None and likes is None:
        return None
    return Metrics(downloads=dls, likes=likes)


def _normalize(d: dict[str, Any]) -> DataResource:
    ds_id = d["id"]
    tags = d.get("tags") or []
    created = d.get("createdAt")
    year = int(created[:4]) if created and created[:4].isdigit() else None
    author = d.get("author")
    # The file path is escaped as huggingface_hub's own hf_hub_url escapes it: a space
    # made the URL invalid, and '#', '?' or '%' in a name requested a different file.
    files = [
        FileEntry(
            name=s["rfilename"], url=f"{FILE_BASE}/{ds_id}/resolve/main/{quote(s['rfilename'])}"
        )
        for s in (d.get("siblings") or [])
        if s.get("rfilename") and s["rfilename"] != ".gitattributes"
    ]
    return DataResource(
        id=f"hf:{ds_id}",
        source="huggingface",
        kind="dataset",
        title=ds_id,
        creators=[Creator(name=author)] if author else [],
        year=year,
        doi=None,
        license=_license(tags, d.get("cardData")),
        subjects=[t for t in tags if ":" not in t],
        access="restricted" if d.get("gated") else "open",
        metrics=_metrics(d),
        last_updated=d.get("lastModified"),
        files=files,
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    """Search HF datasets. HF paginates by Link-header cursor, not row offset, so
    this contributes to page 1 only — offset>0 returns no rows (see P4 spec)."""
    if offset:
        return 0, []
    data = await _http.request_json(
        client,
        "GET",
        API,
        service="HuggingFace search",
        params={"search": query, "limit": min(size, MAX_SIZE), "full": "true"},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        expect=list,  # an error envelope is an outage, not zero datasets
        check=_check_datasets,
    )
    return len(data), [compact(_normalize(d)) for d in data]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    ds_id = local_id(resource_id, "hf")
    if not _is_dataset_id(ds_id):
        raise NotFoundError(f"malformed HuggingFace id {resource_id!r}")
    try:
        body = await _http.request_json(
            client,
            "GET",
            f"{API}/{ds_id}",
            service="HuggingFace resolve",
            params={"full": "true"},
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
            expect=dict,
            check=_check_dataset,
        )
    except NotFoundError:
        raise NotFoundError(f"HuggingFace has no dataset {ds_id!r}") from None
    resource = _normalize(body)
    try:
        resource.files += await hf_datasets_server.parquet_files(client, ds_id)
    except NotFoundError:
        pass  # no converted view — normal (gated / too-big / non-tabular / pending)
    except Exception as exc:  # noqa: BLE001 — best-effort; never break resolve
        logger.warning("datasets-server enrichment failed for %s: %r", ds_id, exc)
        # Recorded, so the router does not cache a record missing its parquet files.
        resource = resource.model_copy(
            update={
                "errors": {
                    **resource.errors,
                    "files": f"HuggingFace datasets-server lookup failed: "
                    f"{type(exc).__name__}: {exc}; converted parquet files unknown",
                }
            }
        )
    return resource
