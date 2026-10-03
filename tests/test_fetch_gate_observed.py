"""Pin what the fetch gate refuses, and that what it does not refuse still passes (#88).

Each refusal test holds its positive control: the same check, on a record it must
let through. Messages are compared whole, so a reworded or truncated refusal fails.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

from data_aggregator_mcp import fetch_gate, router, server
from data_aggregator_mcp.errors import FetchNotSupportedError
from data_aggregator_mcp.models import DataResource, FileEntry, Link

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_FILE = FileEntry(name="a.csv", size=1, url="https://example.org/a.csv")


def _res(
    rid: str, source: str, *, files: bool = False, links: tuple[Link, ...] = ()
) -> DataResource:
    return DataResource(
        id=rid,
        source=source,
        kind="dataset",
        title="t",
        files=[_FILE] if files else [],
        links=list(links),
    )


def _refused(check, fid: str, resource: DataResource) -> str:
    """The raw refusal message ``check`` raises (without the error-class prefix)."""
    with pytest.raises(FetchNotSupportedError) as exc:
        check(fid, resource)
    return str(exc.value.args[0])


def test_every_wired_prefix_has_a_backend_and_nothing_else_does() -> None:
    for prefix in fetch_gate.FETCHABLE_PREFIXES:
        assert fetch_gate.id_has_backend(f"{prefix}x1"), prefix
    assert fetch_gate.id_has_backend("7654321")  # a bare Zenodo id
    for fid in ("gwas:GCST000028", "nasacmr:C1-X", "zenodo", "12a", "", "x-zenodo:1"):
        assert not fetch_gate.id_has_backend(fid), fid


def test_the_no_backend_message_lists_every_fetchable_prefix() -> None:
    msg = fetch_gate.no_backend_message("gwas:GCST000028")
    head = "'gwas:GCST000028' has no wired fetch backend. Fetchable id prefixes: "
    tail = " (and bare Zenodo ids). Resolve it for the landing page / DOI instead."
    assert msg.startswith(head) and msg.endswith(tail)
    listed = msg[len(head) : -len(tail)].split(", ")
    assert listed == list(fetch_gate.FETCHABLE_PREFIXES)
    assert "zenodo:" in listed and "datacite:" in listed and "gwas:" not in listed
    # Positive control: an id with a backend gets no refusal at all.
    assert fetch_gate.refusal(_res("zenodo:1", "zenodo", files=True)) is None


def test_a_datacite_record_is_refused_unless_its_host_repo_streams() -> None:
    for repo in ("figshare", "dataverse", "osf", "zenodo", "openneuro"):
        fetch_gate.ensure_repo_fetchable("datacite:10.1/x", _res("datacite:10.1/x", repo))
    dryad = _refused(fetch_gate.ensure_repo_fetchable, "datacite:10.5061/d", _res("d", "dryad"))
    assert dryad == (
        "'datacite:10.5061/d' (repo: dryad) is discovery-only for fetch — its file manifest "
        "is available via resolve, but no adapter streams its bytes. Dryad downloads are "
        "token/bot-challenge gated."
    )
    mendeley = _refused(
        fetch_gate.ensure_repo_fetchable, "datacite:10.17632/m", _res("m", "mendeley")
    )
    assert mendeley == (
        "'datacite:10.17632/m' (repo: mendeley) is discovery-only for fetch — its file "
        "manifest is available via resolve, but no adapter streams its bytes."
    )
    # The repo check is for datacite: ids only; any other id is judged elsewhere.
    fetch_gate.ensure_repo_fetchable("zenodo:1", _res("zenodo:1", "dryad"))


def test_a_gbif_dataset_without_an_archive_is_refused() -> None:
    fid = "gbif:2654bd43"
    fetch_gate.ensure_gbif_fetchable(fid, _res(fid, "gbif", files=True))
    assert _refused(fetch_gate.ensure_gbif_fetchable, fid, _res(fid, "gbif")) == (
        "'gbif:2654bd43' is discovery-only for fetch — this GBIF dataset publishes no "
        "Darwin Core Archive (metadata-only). Resolve it for the DOI / landing page instead."
    )
    fetch_gate.ensure_gbif_fetchable("datagov:x", _res("datagov:x", "datagov"))


def test_a_datagov_package_without_a_download_is_refused() -> None:
    fid = "datagov:some-package"
    fetch_gate.ensure_datagov_fetchable(fid, _res(fid, "datagov", files=True))
    assert _refused(fetch_gate.ensure_datagov_fetchable, fid, _res(fid, "datagov")) == (
        "'datagov:some-package' is discovery-only for fetch — this data.gov package "
        "publishes no downloadable resource. Resolve it for the landing page / metadata instead."
    )
    fetch_gate.ensure_datagov_fetchable("gbif:x", _res("gbif:x", "gbif"))


def test_an_omicsdi_record_without_files_is_refused_with_its_landing_page() -> None:
    fid = "omicsdi:massive:MSV000001"
    fetch_gate.ensure_omicsdi_fetchable(fid, _res(fid, "omicsdi", files=True))
    base = (
        "'omicsdi:massive:MSV000001' is discovery-only for fetch — only PRIDE and "
        "MetaboLights records are streamable; this repo exposes no wired fetch backend."
    )
    assert _refused(fetch_gate.ensure_omicsdi_fetchable, fid, _res(fid, "omicsdi")) == base
    links = (
        Link(rel="described_in", target_id="https://other.example/x"),
        Link(rel="landing_page", target_id="https://massive.example/MSV000001"),
        Link(rel="landing_page", target_id="https://second.example/y"),
    )
    with_landing = _refused(
        fetch_gate.ensure_omicsdi_fetchable, fid, _res(fid, "omicsdi", links=links)
    )
    assert (
        with_landing
        == base + " Fetch from the source repo directly: https://massive.example/MSV000001"
    )
    only_other = _res(fid, "omicsdi", links=links[:1])
    assert _refused(fetch_gate.ensure_omicsdi_fetchable, fid, only_other) == base
    fetch_gate.ensure_omicsdi_fetchable("gbif:x", _res("gbif:x", "gbif"))


def test_a_literature_id_without_open_access_full_text_is_refused() -> None:
    for fid in ("pubmed:23066504", "openaire:oai123"):
        fetch_gate.ensure_fulltext_available(fid, _res(fid, "pubmed", files=True))
        assert _refused(fetch_gate.ensure_fulltext_available, fid, _res(fid, "pubmed")) == (
            f"no open-access full text was found for '{fid}' — it may be paywalled, absent "
            "from EuropePMC/Unpaywall, or the lookup itself may have failed. Resolve it for "
            "the landing page / DOI instead."
        )
    fetch_gate.ensure_fulltext_available("zenodo:1", _res("zenodo:1", "zenodo"))


@pytest.mark.parametrize(
    ("fid", "resource", "starts"),
    [
        (
            "datacite:10.5061/d",
            _res("d", "dryad", files=True),
            "'datacite:10.5061/d' (repo: dryad)",
        ),
        (
            "pubmed:1",
            _res("pubmed:1", "pubmed"),
            "no open-access full text was found for 'pubmed:1'",
        ),
        ("omicsdi:massive:M", _res("omicsdi:massive:M", "omicsdi"), "'omicsdi:massive:M' is"),
        ("gbif:k", _res("gbif:k", "gbif"), "'gbif:k' is discovery-only for fetch — this GBIF"),
        (
            "datagov:p",
            _res("datagov:p", "datagov"),
            "'datagov:p' is discovery-only for fetch — this data.gov",
        ),
    ],
)
def test_ensure_fetchable_runs_every_refusal(fid, resource, starts) -> None:
    assert _refused(fetch_gate.ensure_fetchable, fid, resource).startswith(starts)
    # Positive control: the same id, with a streamable record, passes every check.
    ok = resource.model_copy(update={"source": "figshare", "files": [_FILE]})
    fetch_gate.ensure_fetchable(fid, ok)


def test_refusal_returns_the_reason_fetch_would_give_and_none_when_it_streams() -> None:
    assert fetch_gate.refusal(_res("gwas:GCST000028", "gwas", files=True)) == (
        fetch_gate.no_backend_message("gwas:GCST000028")
    )
    gbif = _res("gbif:k", "gbif")
    reason = fetch_gate.refusal(gbif)
    assert reason == _refused(fetch_gate.ensure_fetchable, "gbif:k", gbif)
    assert reason is not None and not reason.startswith("[")  # the message, not str(exc)
    assert fetch_gate.refusal(_res("gbif:k", "gbif", files=True)) is None


@live_only
async def test_live_fetch_streams_through_the_gate_and_refuses_what_it_cannot(tmp_path) -> None:
    """Real execution: one record the gate lets through is downloaded by the fetch tool,
    and three it refuses fail before any download, each with its own reason; resolve's
    access_modes gives the same answer for both."""
    out = await server._dispatch(
        "fetch",
        {
            "id": "geo:GSE10072",
            "dest": str(tmp_path / "ok"),
            "files": "filelist.txt",
            "max_bytes": 10_000_000,
        },
    )
    assert out["paths"] and out["bytes"] > 0
    refused = {
        "gwas:GCST000028": "has no wired fetch backend",
        "gbif:2654bd43-7fbd-43e7-8807-782f98945e20": "publishes no Darwin Core Archive",
        "datacite:10.5061/dryad.98sf7m0wt": "(repo: dryad) is discovery-only for fetch",
    }
    for fid, reason in refused.items():
        dest = tmp_path / fid.replace(":", "_").replace("/", "_")
        with pytest.raises(FetchNotSupportedError, match=re.escape(reason)):
            await server._dispatch("fetch", {"id": fid, "dest": str(dest)})
        assert not dest.exists() or not any(dest.iterdir())
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        geo = await router.resolve(client, "geo:GSE10072")
        dryad = await router.resolve(client, "datacite:10.5061/dryad.98sf7m0wt")
    assert "fetch" in geo.access_modes and fetch_gate.refusal(geo) is None
    assert dryad.files and dryad.access_modes == []
    assert fetch_gate.refusal(dryad) is not None
