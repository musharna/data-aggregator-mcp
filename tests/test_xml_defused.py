"""Remote XML (PubMed, DataONE, NCBI taxonomy, omics) goes through defusedxml.

Negative control: an entity-expansion payload must be rejected. Positive control in
the same test: a plain document still parses, so a broken parser cannot read as
"blocked".
"""

import pytest
from defusedxml import DefusedXmlException

from data_aggregator_mcp import _http, dataone, omics, pubmed, taxonomy

BOMB = (
    '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "xxxxxxxxxx">'
    '<!ENTITY y "&x;&x;&x;&x;&x;&x;&x;&x;&x;&x;">]><a>&y;</a>'
)


@pytest.mark.parametrize("mod", [_http, dataone, omics, pubmed, taxonomy])
def test_xml_parser_rejects_entities_and_parses_plain(mod):
    assert mod.ET.fromstring("<a><b>1</b></a>").find("b").text == "1"
    with pytest.raises(DefusedXmlException, match=r"(?i)entit"):
        mod.ET.fromstring(BOMB)


def test_dataone_first_url_treats_entity_payload_as_unparseable():
    # _first_url's contract is "None on unparseable"; defusedxml's rejection is a
    # ValueError, not ParseError, so without the widened except it escapes
    assert dataone._first_url("<r><url>https://x/a</url></r>") == "https://x/a"
    assert dataone._first_url(BOMB) is None
