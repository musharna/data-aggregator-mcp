"""What doi.org content negotiation answers, per registration agency, and how render reads it.

Every answer here was probed live on 2026-10-02 (one DOI per agency, each of bibtex, ris,
csl-json and apa). An agency without content negotiation for a format still answers 200,
in some other format: Airiti sends CSL-JSON whatever was asked, ISTIC redirects to its HTML
landing page. Each was returned as the citation.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
import pytest

from data_aggregator_mcp import citation
from data_aggregator_mcp.models import DataResource

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_BIBTEX = "@article{x, title={T}}"
_CSL = '{"type":"article-journal","title":"T"}'
_HTML = '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'


def _rec(doi: str = "10.1038/x") -> DataResource:
    return DataResource(id=f"literature:{doi}", source="x", kind="publication", title="T", doi=doi)


async def _render(content_type: str, body: str, fmt: str) -> str | None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": content_type}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await citation.render(client, _rec(), fmt)


_AIRITI = "application/vnd.citationstyles.csl+json;charset=UTF-8"
_BIB, _RIS, _STYLE = "application/x-bibtex", "application/x-research-info-systems", "text/plain"

# (format asked for, content type answered, body, a type that does carry the format)
# — the wrong-format 200s probed live.
_WRONG = [
    # Airiti (10.6220/joq.2015.22(5).06): CSL-JSON for every format
    ("bibtex", _AIRITI, _CSL, _BIB),
    ("ris", _AIRITI, _CSL, _RIS),
    ("apa", _AIRITI, _CSL, _STYLE),
    # ISTIC (10.3969/j.issn.1000-5137.2017.05.018): the Wanfang landing page
    ("bibtex", "text/html", _HTML, _BIB),
    ("ris", "text/html", _HTML, _RIS),
    ("apa", "text/html", _HTML, _STYLE),
    # no content type at all
    ("bibtex", "", _BIBTEX, _BIB),
]


@pytest.mark.parametrize(("fmt", "content_type", "body", "right"), _WRONG)
async def test_an_answer_in_another_format_is_not_a_citation(
    caplog, fmt, content_type, body, right
) -> None:
    caplog.set_level(logging.WARNING, logger="data_aggregator_mcp.citation")
    assert await _render(content_type, body, fmt) is None
    media = content_type.partition(";")[0]
    [msg] = [r.getMessage() for r in caplog.records]
    assert msg == (
        f"citation: doi.org has no {fmt!r} citation of 10.1038/x (literature:10.1038/x); "
        f"it answered {media!r}"
    )
    # Positive control: the same body in the type that was asked for is the citation.
    assert await _render(right, body, fmt) == body


# (format, content type) — each agency's own spelling of the right answer, probed live.
_RIGHT = [
    ("bibtex", "application/x-bibtex"),  # Crossref
    ("bibtex", "application/x-bibtex; charset=utf-8"),  # DataCite
    ("bibtex", "application/x-bibtex;charset=UTF-8"),  # mEDRA, OP, KISTI
    ("ris", "application/x-research-info-systems"),  # Crossref
    ("ris", "application/x-Research-Info-Systems;charset=UTF-8"),  # KISTI
    ("csl-json", "application/vnd.citationstyles.csl+json"),  # Crossref
    ("csl-json", "application/vnd.citationstyles.csl+json;charset=UTF-8"),  # JaLC, Airiti
    ("apa", "text/x-bibliography"),  # Crossref
    ("apa", "text/x-bibliography; charset=utf-8"),  # DataCite
    ("apa", "text/plain;charset=UTF-8"),  # KISTI
    ("apa", "text/x-bibliography ; charset=utf-8"),  # RFC 9110 allows space before ';'
    ("mla", "TEXT/X-BIBLIOGRAPHY"),  # type and subtype are case-insensitive
]


@pytest.mark.parametrize(("fmt", "content_type"), _RIGHT)
async def test_each_agency_spelling_of_the_right_type_is_read(fmt, content_type) -> None:
    assert await _render(content_type, "  the citation \n", fmt) == "the citation"
    # Negative control: the same body as an HTML page is refused.
    assert await _render("text/html; charset=utf-8", "the citation", fmt) is None


@live_only
async def test_live_an_agency_without_the_format_gives_no_citation() -> None:
    istic = "10.3969/j.issn.1671-7104.230180"  # PubMed 38384230's DOI
    airiti = "10.6220/joq.2015.22(5).06"
    async with httpx.AsyncClient(timeout=30) as client:
        assert await citation.render(client, _rec(istic), "bibtex") is None
        assert await citation.render(client, _rec(airiti), "bibtex") is None
        # Positive controls: Airiti's CSL-JSON is still its csl-json citation, a
        # Crossref DOI still has BibTeX, and KISTI's mixed-case RIS type is read.
        csl = await citation.render(client, _rec(airiti), "csl-json")
        bib = await citation.render(client, _rec("10.1038/171737a0"), "bibtex")
        ris = await citation.render(client, _rec("10.5352/jls.2008.18.8.1123"), "ris")
    assert json.loads(csl)["page-first"] == "461"
    assert bib and bib.startswith("@article{")
    assert ris and ris.startswith("TY  - JOUR")
