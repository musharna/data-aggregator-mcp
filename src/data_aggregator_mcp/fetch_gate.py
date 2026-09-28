"""Which records ``fetch`` will stream — one answer for every place that needs it.

The ``fetch`` tool refuses an id with no wired backend, and some resolved records whose
bytes no adapter can stream (Dryad, metadata-only GBIF / data.gov packages, ...).
``operate`` downloads the same files and ``resolve`` advertises ``access_modes``; both
used to judge a record by "has a file URL" instead, so a record fetch refuses was
advertised fetchable and operable. They now ask this module.
"""

from __future__ import annotations

from data_aggregator_mcp import sources
from data_aggregator_mcp.errors import FetchNotSupportedError
from data_aggregator_mcp.models import DataResource

# id prefixes with a working fetch backend — derived from the central registry (which
# also feeds router's adapter map + discovery-only set), so the gate can't drift from it.
FETCHABLE_PREFIXES = sources.FETCHABLE_PREFIXES


def id_has_backend(fid: str) -> bool:
    """True if ``fid`` has a wired fetch backend (allowlisted prefix or bare Zenodo id)."""
    return fid.startswith(FETCHABLE_PREFIXES) or fid.isdigit()


def no_backend_message(fid: str) -> str:
    return (
        f"'{fid}' has no wired fetch backend. Fetchable id prefixes: "
        f"{', '.join(FETCHABLE_PREFIXES)} (and bare Zenodo ids). "
        "Resolve it for the landing page / DOI instead."
    )


# DataCite ids all share the `datacite:` prefix, so fetchability is decided
# post-resolve from the detected host repo. Dryad is manifest-only (downloads are
# token/bot-challenge gated), so it is NOT here.
DATACITE_FETCHABLE = ("figshare", "dataverse", "osf", "zenodo", "openneuro")


def ensure_repo_fetchable(fid: str, resource: DataResource) -> None:
    """Fail loud when a datacite: id resolves to a host repo we can't stream."""
    if fid.startswith("datacite:") and resource.source not in DATACITE_FETCHABLE:
        hint = (
            " Dryad downloads are token/bot-challenge gated." if resource.source == "dryad" else ""
        )
        raise FetchNotSupportedError(
            f"'{fid}' (repo: {resource.source}) is discovery-only for fetch — its file "
            f"manifest is available via resolve, but no adapter streams its bytes.{hint}"
        )


def ensure_gbif_fetchable(fid: str, resource: DataResource) -> None:
    """Fail loud when a gbif: id resolved to no Darwin Core Archive — a metadata-only
    (or feed-only) dataset is discovery-only; occurrence/checklist/sampling-event
    datasets carry a DWC_ARCHIVE and pass."""
    if fid.startswith("gbif:") and not resource.files:
        raise FetchNotSupportedError(
            f"'{fid}' is discovery-only for fetch — this GBIF dataset publishes no "
            "Darwin Core Archive (metadata-only). Resolve it for the DOI / landing page instead."
        )


def ensure_datagov_fetchable(fid: str, resource: DataResource) -> None:
    """Fail loud when a datagov: id resolved to no downloadable resource — a metadata-
    only / link-only package is discovery-only; datasets with a downloadable distribution pass."""
    if fid.startswith("datagov:") and not resource.files:
        raise FetchNotSupportedError(
            f"'{fid}' is discovery-only for fetch — this data.gov package publishes no "
            "downloadable resource. Resolve it for the landing page / metadata instead."
        )


def ensure_omicsdi_fetchable(fid: str, resource: DataResource) -> None:
    """Fail loud when an omicsdi: id resolved to no files — its repo (MassIVE,
    Metabolomics Workbench, GNPS, PeptideAtlas) is discovery-only this wave.
    PRIDE/MetaboLights populate files[] at resolve and pass."""
    if fid.startswith("omicsdi:") and not resource.files:
        landing = next((lnk.target_id for lnk in resource.links if lnk.rel == "landing_page"), None)
        where = f" Fetch from the source repo directly: {landing}" if landing else ""
        raise FetchNotSupportedError(
            f"'{fid}' is discovery-only for fetch — only PRIDE and MetaboLights records "
            f"are streamable; this repo exposes no wired fetch backend.{where}"
        )


LITERATURE_PREFIXES = ("pubmed:", "openaire:")


def ensure_fulltext_available(fid: str, resource: DataResource) -> None:
    """Fail loud when a literature id has no open-access full text to fetch
    (paywalled, or not in EuropePMC/Unpaywall) — don't return a silently empty
    result (spec §8)."""
    if fid.startswith(LITERATURE_PREFIXES) and not resource.files:
        raise FetchNotSupportedError(
            f"no open-access full text was found for '{fid}' — it may be paywalled, absent "
            "from EuropePMC/Unpaywall, or the lookup itself may have failed. Resolve it for "
            "the landing page / DOI instead."
        )


def ensure_fetchable(fid: str, resource: DataResource) -> None:
    """Every post-resolve refusal, for ``resource`` requested as ``fid``."""
    ensure_repo_fetchable(fid, resource)
    ensure_fulltext_available(fid, resource)
    ensure_omicsdi_fetchable(fid, resource)
    ensure_gbif_fetchable(fid, resource)
    ensure_datagov_fetchable(fid, resource)


def refusal(resource: DataResource) -> str | None:
    """Why ``fetch`` of this record (by its own id) would be refused; None if it streams."""
    if not id_has_backend(resource.id):
        return no_backend_message(resource.id)
    try:
        ensure_fetchable(resource.id, resource)
    except FetchNotSupportedError as exc:
        return str(exc.args[0])
    return None
