"""OmicsDI (Omics Discovery Index) — proteomics/metabolomics discovery.

Restricted to the mass-spec modality repos OmicsDI uniquely adds; GEO /
ArrayExpress / ENA hits are dropped (already covered by the omics leg, and
accession-keyed so the DOI dedup would miss the duplicates). Resolve (Task 6)
routes fetchable files to PRIDE / MetaboLights; other repos are discovery-only.

Page-1-only: we post-filter each page to the modality repos, so the router's
offset accounting (which counts records consumed from the merged stream) cannot
be reconciled with the upstream all-rows offset — mirror huggingface.py and
contribute first-page results only.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http, metabolights, pride
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import DataResource, Link, compact

SEARCH = "https://www.omicsdi.org/ws/dataset/search"
RECORD = "https://www.omicsdi.org/ws/dataset/{source}/{acc}"
_LANDING = "https://www.omicsdi.org/dataset/{source}/{acc}"
PREFIXES = {"omicsdi"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2

# OmicsDI `source` codes for the mass-spectrometry modality we uniquely add
# (proteomics + metabolomics). Deliberately excludes EGA (controlled-access human
# genomics, not MS) and the transcriptomics repos (GEO/ArrayExpress/ENA) the omics
# leg already covers.
_MODALITY_REPOS = {
    "pride",
    "massive",
    "metabolights_dataset",
    "metabolomics_workbench",
    "gnps",
    "peptide_atlas",
}


def _normalize(d: dict) -> DataResource:
    source, acc = d.get("source", ""), d.get("id", "")
    return DataResource(
        id=f"omicsdi:{source}:{acc}",
        source="omicsdi",
        kind="study",
        title=d.get("title") or "",
        description=d.get("description"),
        links=[Link(rel="landing_page", target_id=_LANDING.format(source=source, acc=acc))],
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
        SEARCH,
        service="OmicsDI search",
        params={"query": query, "size": min(size, MAX_SIZE)},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    datasets = (body or {}).get("datasets") or []
    kept = [d for d in datasets if d.get("source") in _MODALITY_REPOS]
    return len(kept), [compact(_normalize(d)) for d in kept]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    parts = resource_id.split(":", 2)  # omicsdi:<source>:<acc>
    if len(parts) != 3:
        raise NotFoundError(f"malformed OmicsDI id {resource_id!r}")
    _prefix, source, acc = parts
    body = await _http.request_json(
        client,
        "GET",
        RECORD.format(source=source, acc=acc),
        service="OmicsDI resolve",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
    )
    if body is None:
        raise NotFoundError(f"OmicsDI has no {source}/{acc}")
    resource = DataResource(
        id=resource_id,
        source="omicsdi",
        kind="study",
        title=body.get("name") or "",
        description=body.get("description"),
        links=[Link(rel="landing_page", target_id=_LANDING.format(source=source, acc=acc))],
        files=[],
    )
    if source == "pride":
        file_list = await pride.files(client, acc)
    elif source == "metabolights_dataset":
        file_list = await metabolights.files(client, acc)
    else:
        file_list = []
    return resource.model_copy(update={"files": file_list})
