"""What OmicsDI answers, and what the adapter makes of it.

Every record here is a trimmed capture of the live ``/ws/dataset/{source}/{acc}`` answer
(2026-10-02; ``additional`` cut to the keys the adapter reads plus ``repository`` and
``author``). Also probed live that day: a search with no hits answers
``{"count": 0, "datasets": [], "facets": []}``; an unknown accession answers 404; the
accession is case-sensitive (``pride/pxd000001`` is 404, ``PRIDE/PXD000001`` is 200);
1,561 of 1,561 search hits sampled have a string ``id``, ``source`` and ``title`` and a
string or null ``description``. ``additional`` held only lists of strings in 30 of 30
records sampled at random across the six modality repos.
"""

import copy

import httpx
import pytest

from data_aggregator_mcp import _http, omicsdi
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import Creator, Link

# PRIDE: the dataset's own DOI under `additional.doi`; the paper's DOI ends the
# publication string, which has no leading PMID.
PXD026702 = {
    "database": "Pride",
    "accession": "PXD026702",
    "name": "LC-MSMS of the MHC-I immunopeptidome of human iPSCs",
    "description": "The development of cancer immunotherapeutics is limited by the discovery of acti",
    "additional": {
        "submitter": ["Courcelles Mathieu"],
        "repository": ["Pride"],
        "species": ["Homo Sapiens (human)"],
        "publication": [
            "Apavaloaei A, Hesnard L, Hardy MP, Benabdallah B, Ehx G, Thériault C, Laverdure"
            " JP, Durette C, Lanoix J, Courcelles M, Noronha N, Chauhan KD, Lemieux S,"
            " Beauséjour C, Bhatia M, Thibault P, Perreault C. Induced pluripotent stem cells"
            " display a distinct set of MHC I-associated peptides shared by human cancers."
            " Cell Rep. 2022 40(7):111241 10.1016/J.CELREP.2022.111241"
        ],
        "doi": ["10.6019/PXD026702"],
    },
}

# PRIDE: no own DOI; the publication string leads with the PMID and ends with the
# paper's DOI (a Mol Cell Proteomics article per Crossref, not the dataset).
PXD002724 = {
    "database": "Pride",
    "accession": "PXD002724",
    "name": "Analysis of proteins that rapidly change upon mTORC1 repression identifies"
    " Park7 as a novel protein aberrantly expressed in Tuberous Sclerosis Complex",
    "description": "Many biological processes involve the mechanistic/mammalian target of rapamycin ",
    "additional": {
        "submitter": ["Rui Zhu"],
        "repository": ["Pride"],
        "species": ["Rattus Rattus (black Rat)"],
        "publication": [
            "26419955 Niere F, Namjoshi S, Song E, Dilly GA, Schoenhard G, Zemelman BV,"
            " Mechref Y, Raab-Graham KF. Analysis of Proteins That Rapidly Change Upon"
            " Mechanistic/Mammalian Target of Rapamycin Complex 1 (mTORC1) Repression"
            " Identifies Parkinson Protein 7 (PARK7) as a Novel Protein Aberrantly Expressed"
            " in Tuberous Sclerosis Complex (TSC). Mol Cell Proteomics. 2016"
            " Feb;15(2):426-44 10.1074/mcp.M115.055079"
        ],
    },
}

# MetaboLights: `submitter_name`/`organism`, and a publication written
# "<title>. <doi>. PMID:<pmid>" (3 of the 4 sampled MetaboLights DOIs are followed by a PMID).
MTBLS806 = {
    "database": "MetaboLights",
    "accession": "MTBLS806",
    "name": "Ovothiol A is the Main Antioxidant in Fish Lens",
    "description": "Tissue protection from oxidative stress by antioxidants is of vital importance f",
    "additional": {
        "repository": ["MetaboLights"],
        "publication": [
            "Ovothiol A is the Main Antioxidant in Fish Lens. 10.3390/metabo9050095. PMID:31083459"
        ],
        "submitter_name": ["Vadim Yanshole"],
        "organism": ["Sander lucioperca", "Rutilus rutilus"],
        "author": [
            "Yuri Tsentalovich. International Tomography Center SB RAS. Institutskaya 3a,"
            " Novosibirsk, 630090, Russia. yura@tomo.nsc.ru.",
            "Vadim Yanshole. International Tomography Center SB RAS. Institutskaya 3a,"
            " Novosibirsk, 630090, Russia. vadim.yanshole@tomo.nsc.ru. +7-383-330-3136.",
        ],
    },
}

# MassIVE: discovery-only, no publication.
MSV000081764 = {
    "database": "MassIVE",
    "accession": "MSV000081764",
    "name": "A proteomic map of the human aqueous humor",
    "description": "The aqueous humor is a colourless, transparent fluid that fills the anterior cha",
    "additional": {
        "submitter": ["Akhilesh Pandey"],
        "repository": ["MassIVE"],
        "species": ["Homo Sapiens (ncbitaxon:9606)"],
    },
}

NO_HITS = {"count": 0, "datasets": [], "facets": []}
HIT = {
    "id": "PXD006873",
    "source": "pride",
    "title": "A Generic HPLC Method for Absolute Quantification of Oxidation in Monoclonal"
    " Antibodies and Fc-Fusion Proteins Using UV and MS Detection",
    "description": None,
    "keywords": None,
    "omicsType": ["Proteomics"],
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


async def _no_files(_client, _acc):
    return []


@pytest.fixture(autouse=True)
def no_file_lists(monkeypatch):
    monkeypatch.setattr(omicsdi.pride, "files", _no_files)
    monkeypatch.setattr(omicsdi.metabolights, "files", _no_files)


async def _resolve(rid: str, body: object, sent: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(request)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await omicsdi.resolve(c, rid)


async def _search(body: object):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as c:
        return await omicsdi.search(c, "cancer")


# --- the record's DOI is its own; a paper's DOI is what it is described in ----------


@pytest.mark.asyncio
async def test_a_papers_doi_is_described_in_never_the_records_doi():
    """The paper DOI at the end of `publication` was the record's `doi`, so a PRIDE
    dataset claimed its Mol Cell Proteomics article's identity (DOI dedup and `relate`
    key on `doi`). It is a `described_in` link, as in pdb and biostudies; the record's
    own DOI is the one PRIDE lists under `additional.doi`."""
    paper_only = await _resolve("omicsdi:pride:PXD002724", PXD002724)
    assert paper_only.doi is None
    assert paper_only.links == [
        Link(rel="landing_page", target_id="https://www.omicsdi.org/dataset/pride/PXD002724"),
        Link(rel="described_in", target_id="10.1074/mcp.M115.055079"),
    ]
    assert paper_only.identifiers == {"pmid": "26419955"}
    own = await _resolve("omicsdi:pride:PXD026702", PXD026702)
    assert own.doi == "10.6019/PXD026702"
    assert own.links[1:] == [Link(rel="described_in", target_id="10.1016/J.CELREP.2022.111241")]
    assert own.identifiers == {}


@pytest.mark.asyncio
async def test_a_metabolights_publication_yields_its_doi_and_pmid():
    """MetaboLights writes "<title>. <doi>. PMID:<pmid>". The DOI pattern was anchored
    at the end of the line, so a DOI followed by its PMID was missed, and only a leading
    PMID was read."""
    r = await _resolve("omicsdi:metabolights_dataset:MTBLS806", MTBLS806)
    assert r.links[1:] == [Link(rel="described_in", target_id="10.3390/metabo9050095")]
    assert r.identifiers == {"pmid": "31083459"}
    assert r.doi is None
    assert r.creators == [Creator(name="Vadim Yanshole")]  # submitter_name, not author
    assert r.organism == ["Sander lucioperca", "Rutilus rutilus"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("publication", "pmid", "dois"),
    [
        (["Not available"], None, []),
        ([""], None, []),
        # a leading number glued to text is not a PMID; a later PMID label is
        (["2019-05 A study. 10.1/x PMID: 123"], "123", []),
        (["12 A study. doi 10.1234/a.b;"], "12", ["10.1234/a.b"]),
        # the first PMID wins; every distinct DOI is a paper, in order
        (
            ["  77 First 10.1000/one.", "88 Second 10.1000/two, 10.1000/one"],
            "77",
            ["10.1000/one", "10.1000/two"],
        ),
        (["Title 10.1000/q<b>"], None, ["10.1000/q"]),
        (['Title "10.1000/q"'], None, ["10.1000/q"]),
    ],
)
async def test_publication_strings_are_read_for_every_paper(publication, pmid, dois):
    body = copy.deepcopy(MSV000081764)
    body["additional"]["publication"] = publication
    r = await _resolve("omicsdi:massive:MSV000081764", body)
    assert r.identifiers == ({"pmid": pmid} if pmid else {})
    assert [link.target_id for link in r.links if link.rel == "described_in"] == dois


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("own", "expected"),
    [
        (["10.6019/PXD026702"], "10.6019/PXD026702"),
        (["not a doi", "10.6019/PXD1"], "10.6019/PXD1"),
        (["see 10.6019/PXD1"], None),
        ([], None),
    ],
)
async def test_the_own_doi_is_the_first_entry_that_is_a_doi(own, expected):
    body = copy.deepcopy(MSV000081764)
    body["additional"]["doi"] = own
    assert (await _resolve("omicsdi:massive:MSV000081764", body)).doi == expected


# --- an id that would leave the record path never reaches the network --------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rid",
    [
        "omicsdi:pride:../../evil?injected=1",
        "omicsdi:..:PXD000001",
        "omicsdi:pride:PXD000001/files",
        "omicsdi:pride:PXD000001#x",
        "omicsdi:pride:PXD%2F1",
        "omicsdi:pride:PXD000001:extra",
        "omicsdi:pride:.hidden",
        "omicsdi:pride:",
        "omicsdi::PXD000001",
        "omicsdi:pride:PXD 1",
    ],
)
async def test_resolve_refuses_an_id_that_would_leave_the_record_path(rid):
    """The source and accession went into the URL path unchecked, so
    `omicsdi:pride:../../evil?injected=1` requested `/ws/evil?injected=1`."""
    sent: list[httpx.Request] = []
    with pytest.raises(NotFoundError, match=r"^\[NotFoundError\] malformed OmicsDI id "):
        await _resolve(rid, MSV000081764, sent)
    assert sent == []
    # positive control: ids of every shape the live search returns reach the record path
    for good, path in [
        ("omicsdi:massive:MSV000081764", "/ws/dataset/massive/MSV000081764"),
        (
            "omicsdi:biostudies-arrayexpress:E-MTAB-1",
            "/ws/dataset/biostudies-arrayexpress/E-MTAB-1",
        ),
        ("omicsdi:pride:PXD000001.1_a", "/ws/dataset/pride/PXD000001.1_a"),
    ]:
        body = {**MSV000081764, "accession": good.split(":")[2]}
        await _resolve(good, body, sent)
        assert sent.pop().url.path == path


# --- a malformed answer is an outage, not "no hits" or a bare error -----------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"count": 3},
        {"datasets": None},
        {"datasets": "x"},
        {"datasets": [1]},
        {"datasets": [{**HIT, "source": None}]},
        {"datasets": [{**HIT, "id": 6873}]},
        {"datasets": [{**HIT, "title": ["x"]}]},
        {"datasets": [{**HIT, "description": 7}]},
        {"datasets": [HIT, {"id": "GSE1", "source": 3}]},
    ],
)
async def test_search_a_malformed_answer_is_an_outage_not_no_hits(body):
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] OmicsDI search returned an unparseable 200 body after 2 tries: "
        r"UpstreamEnvelopeError\(.no OmicsDI dataset list in \{",
    ):
        await _search(body)
    # positive controls: the live no-hit answer is zero hits, and a real hit reads whole
    assert await _search(NO_HITS) == (0, [])
    total, [rec] = await _search({"count": 41763, "datasets": [HIT], "facets": []})
    assert total == 1
    assert (rec.id, rec.title, rec.description) == ("omicsdi:pride:PXD006873", HIT["title"], None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {**MSV000081764, "accession": None},
        {**MSV000081764, "accession": "MSV000081765"},
        {**MSV000081764, "accession": "msv000081764"},
        {**MSV000081764, "name": 7},
        {**MSV000081764, "description": ["x"]},
        {**MSV000081764, "additional": ["x"]},
        {**MSV000081764, "additional": {"publication": "10.1000/x"}},
        {**MSV000081764, "additional": {"submitter": [None]}},
        {**MSV000081764, "additional": {"organism": "Homo sapiens"}},
        {**MSV000081764, "additional": {"doi": [7]}},
    ],
)
async def test_resolve_a_malformed_answer_is_an_outage(body):
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] OmicsDI resolve returned an unparseable 200 body after 2 tries: "
        r"UpstreamEnvelopeError\(.no OmicsDI record MSV000081764 in \{",
    ):
        await _resolve("omicsdi:massive:MSV000081764", body)
    # positive control: the live record, and the same record with every optional field null
    r = await _resolve("omicsdi:massive:MSV000081764", MSV000081764)
    assert r.title == "A proteomic map of the human aqueous humor"
    sparse = {"accession": "MSV000081764", "name": None, "description": None, "additional": None}
    s = await _resolve("omicsdi:massive:MSV000081764", sparse)
    assert (s.title, s.description, s.creators, s.organism) == ("", None, [], [])


_WRONG = ["x", 7, True, 1.5, [1], {"k": 1}, None]
_FULL = copy.deepcopy(PXD026702)
_FULL["additional"].update(submitter_name=["X"], organism=["Y"])


def _paths(node, path=()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], (*path, 0))


def _with(path, value):
    rec = copy.deepcopy(_FULL)
    *parents, last = path
    node = rec
    for p in parents:
        node = node[p]
    node[last] = value
    return rec


def test_no_wrong_typed_field_escapes_as_a_bare_error():
    """Every field of a full record, at every wrong JSON type, is refused by the
    record check or read cleanly; so a field the reader starts using without the
    check fails here (the datacite lesson, 2026-10-01)."""
    omicsdi._check_record(_FULL, "PXD026702")  # positive control: the full record passes
    full = omicsdi._record("omicsdi:pride:PXD026702", "pride", "PXD026702", _FULL)
    assert full.doi == "10.6019/PXD026702" and full.creators == [Creator(name="Courcelles Mathieu")]
    escaped = []
    for path in _paths(_FULL):
        if not path:
            continue
        for value in _WRONG:
            rec = _with(path, value)
            try:
                omicsdi._check_record(rec, "PXD026702")
            except _http.UpstreamEnvelopeError:
                continue
            try:
                omicsdi._record("omicsdi:pride:PXD026702", "pride", "PXD026702", rec)
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
    assert escaped == []
