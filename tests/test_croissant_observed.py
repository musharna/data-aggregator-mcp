"""Croissant output pinned against the Croissant 1.1 vocabulary as MLCommons ships it.

``OFFICIAL_CONTEXT`` is the ``@context`` of mlcommons/croissant
``datasets/1.1/zenodo-head-mri/metadata.json``, copied verbatim on 2026-10-02 (the
example the 1.1 upgrade named as its reference). The live test re-fetches it, so a
change upstream fails here rather than drifting unseen.
"""

from __future__ import annotations

import asyncio
import os
import re

import httpx
import pytest

from data_aggregator_mcp import croissant
from data_aggregator_mcp.models import Creator, DataResource, FileEntry, Link

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

OFFICIAL_EXAMPLE_URL = (
    "https://raw.githubusercontent.com/mlcommons/croissant/main/"
    "datasets/1.1/zenodo-head-mri/metadata.json"
)

OFFICIAL_CONTEXT = {
    "@language": "en",
    "@vocab": "https://schema.org/",
    "citeAs": "cr:citeAs",
    "column": "cr:column",
    "conformsTo": "dct:conformsTo",
    "containedIn": "cr:containedIn",
    "cr": "http://mlcommons.org/croissant/",
    "rai": "http://mlcommons.org/croissant/RAI/",
    "data": {"@id": "cr:data", "@type": "@json"},
    "dataType": {"@id": "cr:dataType", "@type": "@vocab"},
    "dct": "http://purl.org/dc/terms/",
    "examples": {"@id": "cr:examples", "@type": "@json"},
    "extract": "cr:extract",
    "field": "cr:field",
    "fileProperty": "cr:fileProperty",
    "fileObject": "cr:fileObject",
    "fileSet": "cr:fileSet",
    "format": "cr:format",
    "includes": "cr:includes",
    "isLiveDataset": "cr:isLiveDataset",
    "jsonPath": "cr:jsonPath",
    "key": "cr:key",
    "md5": "cr:md5",
    "parentField": "cr:parentField",
    "path": "cr:path",
    "recordSet": "cr:recordSet",
    "references": "cr:references",
    "regex": "cr:regex",
    "repeated": "cr:repeated",
    "replace": "cr:replace",
    "samplingRate": "cr:samplingRate",
    "sc": "https://schema.org/",
    "separator": "cr:separator",
    "source": "cr:source",
    "subField": "cr:subField",
    "transform": "cr:transform",
}


def _expand(key: str, ctx: dict) -> str:
    """The IRI a JSON-LD processor gives ``key`` under ``ctx`` (terms, compact IRIs
    and ``@vocab``; enough for the flat keys this module emits)."""
    term = ctx.get(key)
    if isinstance(term, dict):
        term = term["@id"]
    if isinstance(term, str):
        key = term
    prefix, sep, suffix = key.partition(":")
    if sep and isinstance(ctx.get(prefix), str):
        return ctx[prefix] + suffix
    return ctx["@vocab"] + key


def _full_resource() -> DataResource:
    return DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="Rice genomes",
        description="d",
        doi="10.5281/zenodo.1",
        creators=[Creator(name="A. Author", orcid="0000-0002-1825-0097")],
        year=2024,
        last_updated="2025-01-02",
        subjects=["rice"],
        license="cc-by-4.0",
        citation="@dataset{a, title={Rice genomes}}",
        links=[Link(rel="is_derived_from", target_id="10.1/parent")],
        files=[
            FileEntry(
                name="a.csv", url="https://x/a.csv", mime="text/csv", size=10, checksum="md5:ab"
            ),
            FileEntry(name="b.bin", url="https://x/b.bin", checksum="sha256:cd"),
        ],
    )


def test_context_defines_every_croissant_term_as_mlcommons_does() -> None:
    # Without these definitions `conformsTo` is schema.org's (not Dublin Core's),
    # `citeAs` and `md5` are not Croissant terms, and mlcroissant cannot load the
    # manifest at all (it reads the context's `@language`).
    ctx = croissant.render(_full_resource())["@context"]
    assert {k: ctx.get(k) for k in OFFICIAL_CONTEXT} == OFFICIAL_CONTEXT
    # The provenance prefixes this export adds on top are still declared.
    assert ctx["prov"] == "http://www.w3.org/ns/prov#"
    assert ctx["odrl"] == "http://www.w3.org/ns/odrl/2/"
    assert set(ctx) == set(OFFICIAL_CONTEXT) | {"prov", "odrl"}


def test_every_emitted_key_expands_to_the_property_croissant_reads() -> None:
    m = croissant.render(_full_resource())
    ctx = m["@context"]
    dataset = {k: _expand(k, ctx) for k in m if not k.startswith("@")}
    assert dataset["conformsTo"] == "http://purl.org/dc/terms/conformsTo"
    assert dataset["citeAs"] == "http://mlcommons.org/croissant/citeAs"
    assert dataset["prov:wasAttributedTo"] == "http://www.w3.org/ns/prov#wasAttributedTo"
    assert dataset["prov:wasDerivedFrom"] == "http://www.w3.org/ns/prov#wasDerivedFrom"
    # Positive control: plain schema.org properties stay schema.org's.
    assert dataset["name"] == "https://schema.org/name"
    assert dataset["usageInfo"] == "https://schema.org/usageInfo"
    files = [{k: _expand(k, ctx) for k in f if not k.startswith("@")} for f in m["distribution"]]
    assert files[0]["md5"] == "http://mlcommons.org/croissant/md5"
    assert files[1]["sha256"] == "https://schema.org/sha256"
    assert files[0]["contentSize"] == "https://schema.org/contentSize"


@pytest.mark.parametrize(("size", "text"), [(10, "10 B"), (0, "0 B"), (25585843, "25585843 B")])
def test_content_size_is_text_in_bytes(size: int, text: str) -> None:
    # schema.org contentSize is Text; mlcroissant rejects an int.
    r = _full_resource()
    r.files = [FileEntry(name="a.csv", url="https://x/a.csv", size=size)]
    assert croissant.render(r)["distribution"][0]["contentSize"] == text
    r.files = [FileEntry(name="a.csv", url="https://x/a.csv")]
    assert "contentSize" not in croissant.render(r)["distribution"][0]


def test_file_id_is_an_iri_the_name_is_kept() -> None:
    # A file name is not an IRI: Zenodo names carry spaces, and mlcroissant refuses
    # an @id with whitespace, failing the whole manifest. `#` and `?` would start a
    # fragment or a query. The @id is the percent-encoded name; the name is unchanged.
    r = _full_resource()
    r.files = [
        FileEntry(name="Long-Term Climate Average Models 2.zip"),
        FileEntry(name="my file #1?.csv"),
        FileEntry(name="100%.csv"),
        FileEntry(name="data/train-0.parquet"),
    ]
    dist = croissant.render(r)["distribution"]
    assert [f["@id"] for f in dist] == [
        "Long-Term%20Climate%20Average%20Models%202.zip",
        "my%20file%20%231%3F.csv",
        "100%25.csv",
        "data/train-0.parquet",
    ]
    assert [f["name"] for f in dist] == [f.name for f in r.files]
    assert not any(re.search(r"\s", f["@id"]) for f in dist)


@_live_only
def test_live_context_matches_the_published_croissant_1_1_example() -> None:
    async def fetch() -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.get(OFFICIAL_EXAMPLE_URL)
            resp.raise_for_status()
            return resp.json()

    published = asyncio.run(fetch())
    assert published["conformsTo"] == croissant.render(_full_resource())["conformsTo"]
    assert published["@context"] == OFFICIAL_CONTEXT


@_live_only
def test_live_zenodo_record_with_spaced_file_names_renders_valid_ids() -> None:
    from data_aggregator_mcp import router

    async def resolve() -> DataResource:
        async with httpx.AsyncClient(timeout=60) as c:
            return await router.resolve(c, "zenodo:5112139")

    r = asyncio.run(resolve())
    # Premise: a published Zenodo record's files are fixed; this one has spaced names.
    assert any(re.search(r"\s", f.name) for f in r.files)
    dist = croissant.render(r)["distribution"]
    assert len(dist) == len(r.files)
    assert len({f["@id"] for f in dist}) == len(dist)
    assert not any(re.search(r"\s", f["@id"]) for f in dist)
    sized = [f for f in dist if "contentSize" in f]
    assert sized and all(re.fullmatch(r"\d+ B", f["contentSize"]) for f in sized)
