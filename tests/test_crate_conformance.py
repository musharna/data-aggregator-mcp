"""The provenance crates are RO-Crate 1.1 as a JSON-LD processor and the validator read them.

Measured 2026-10-03 on nine crates built from live records (six dossiers, three run
crates) with rocrate-validator 0.12.1 (profile ro-crate-1.1) and pyld 3.3.0:
- every crate dropped 14 to 17 keys on JSON-LD expansion (``score``, ``is_latest``,
  ``normalized_spdx``, ``retracted``, ``errors``, ``sources_queried``, ...): the context
  did not map them, so to any JSON-LD reader the assessments said nothing;
- REQUIRED checks failed: run crates had no root description, licence or
  datePublished, and a dossier lacked a description or licence when its source did;
- ``errors``, ``ontology_expansions``, ``identifiers`` and ``links`` were nested objects,
  unflagged only because their keys were dropped before the validator saw them.

The offline checks expand each crate against a vendored copy of the RO-Crate 1.1
context; the live check runs the validator itself (it fetches the context).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pyld import jsonld

from data_aggregator_mcp import dossier, fair, ro_crate, run_crate
from data_aggregator_mcp.models import Creator, DataResource, Link, TrustSignals
from tests.test_dossier import NOW, _resource
from tests.test_run_crate_observed import _rich

_FIXTURES = Path(__file__).parent / "fixtures"
_RO_CRATE_CONTEXT = json.loads((_FIXTURES / "ro-crate-1.1-context.json").read_text())
_VOCAB_DOC = (Path(__file__).parent.parent / "docs" / "vocab.md").read_text()


def _offline_loader(url: str, options: dict[str, Any]) -> dict[str, Any]:
    """Serve RO-Crate's context from the vendored copy; refuse any other fetch."""
    if url != ro_crate.CONTEXT:
        raise AssertionError(f"a crate's context fetched {url}")
    return {"contextUrl": None, "documentUrl": url, "document": _RO_CRATE_CONTEXT}


def _expand(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return jsonld.expand(doc, {"documentLoader": _offline_loader})


def _dropped(crate: dict[str, Any]) -> list[tuple[str, str]]:
    """(entity, key) for each non-null key the context maps to nothing: a JSON-LD
    processor drops it, so the crate does not say it."""
    out = []
    for ent in crate["@graph"]:
        for key, value in ent.items():
            if key.startswith("@") or value is None:
                continue
            probe = {"@context": crate["@context"], "@id": "#probe", key: "x"}
            if not _expand(probe):
                out.append((ent["@id"], key))
    return out


def _nested(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value, *(o for v in value.values() for o in _nested(v))]
    if isinstance(value, list):
        return [o for v in value for o in _nested(v)]
    return []


def _not_flat(crate: dict[str, Any]) -> list[tuple[str, str]]:
    """(entity, key) for each nested object that is more than a ``{"@id"}`` reference."""
    return [
        (ent["@id"], key)
        for ent in crate["@graph"]
        for key, value in ent.items()
        for obj in _nested(value)
        if set(obj) != {"@id"}
    ]


def _dangling(crate: dict[str, Any]) -> set[str]:
    """Referenced @ids with no entity in the graph (``conformsTo`` names the spec). A
    nested object without an @id is no reference; ``_not_flat`` reports it."""
    ids = {ent["@id"] for ent in crate["@graph"]}
    refs = {
        obj["@id"]
        for ent in crate["@graph"]
        for key, value in ent.items()
        if key != "conformsTo"
        for obj in _nested(value)
        if "@id" in obj
    }
    return refs - ids


def _root_gaps(crate: dict[str, Any]) -> list[str]:
    """The root properties RO-Crate 1.1 requires that the crate lacks or gets wrong."""
    root = next(ent for ent in crate["@graph"] if ent["@id"] == "./")
    gaps = [k for k in ("name", "description", "datePublished", "license") if not root.get(k)]
    published = root.get("datePublished")
    if published:
        try:
            if len(published) > 10:
                datetime.fromisoformat(published)
            elif len(published) > 4:
                date.fromisoformat(published)
            else:
                int(published)  # a year alone is ISO 8601 too
        except ValueError:
            gaps.append("datePublished format")
    return gaps


def _undefined_terms(crate: dict[str, Any]) -> list[str]:
    """Ad hoc terms the crate uses with no rdf:Property entity defining them."""
    defined = {ent["rdfs:label"] for ent in crate["@graph"] if ent["@type"] == "rdf:Property"}
    used = {key for ent in crate["@graph"] for key in ent if key in ro_crate.TERMS}
    return sorted(used - defined)


def _full_resource() -> DataResource:
    """A record with every signal and every optional part a crate can carry."""
    r = _resource(
        creators=[Creator(name="A", orcid="0000-0002-1825-0097"), Creator(name="B")],
        is_latest=False,
        superseded_by="zenodo:2",
        identifiers={"pmid": "12345", "pmcid": "PMC1"},
        accessions=["GSE1"],
        links=[Link(rel="is_supplement_to", target_id="pmid:12345")],
        last_updated="2024-01-02T00:00:00Z",
        trust=TrustSignals(retracted=True, concern=False, retraction_doi="10.1/retraction"),
    )
    return r.model_copy(update={"fair": fair.assess(r)})


def _bare_resource() -> DataResource:
    return DataResource(id="pdb:4HHB", source="pdb", kind="dataset", title="t")


CRATES = {
    "dossier, every signal": lambda: dossier.render(_full_resource(), now=NOW),
    "dossier, bare record": lambda: dossier.render(_bare_resource(), now=NOW),
    "run crate, rich page": lambda: run_crate.render(_rich(), now=NOW),
    "ro-crate export, bare record": lambda: ro_crate.render(_bare_resource(), created=NOW),
}


@pytest.mark.parametrize("name", CRATES)
def test_a_json_ld_processor_keeps_every_key(name: str) -> None:
    crate = CRATES[name]()
    assert _dropped(crate) == []
    # control: a key the context does not map is reported, so the check can fail
    planted = dict(crate, **{"@graph": [*crate["@graph"], {"@id": "#x", "unmapped": 1}]})
    assert _dropped(planted) == [("#x", "unmapped")]


@pytest.mark.parametrize("name", CRATES)
def test_the_graph_is_flat_and_every_reference_resolves(name: str) -> None:
    crate = CRATES[name]()
    assert _not_flat(crate) == []
    assert _dangling(crate) == set()
    # controls: a nested object and a reference to nothing are both reported
    planted = dict(crate, **{"@graph": [*crate["@graph"], {"@id": "#x", "p": {"q": 1}}]})
    assert _not_flat(planted) == [("#x", "p")]
    planted = dict(crate, **{"@graph": [*crate["@graph"], {"@id": "#x", "p": {"@id": "#y"}}]})
    assert _dangling(planted) == {"#y"}


@pytest.mark.parametrize("name", CRATES)
def test_the_root_has_what_ro_crate_requires(name: str) -> None:
    crate = CRATES[name]()
    assert _root_gaps(crate) == []
    root = next(ent for ent in crate["@graph"] if ent["@id"] == "./")
    # control: a root without a description, or with a date that is not ISO 8601, fails
    for change in ({"description": ""}, {"datePublished": "3 Oct 2026"}):
        broken = [dict(root, **change) if ent is root else ent for ent in crate["@graph"]]
        assert _root_gaps(dict(crate, **{"@graph": broken})) != []


@pytest.mark.parametrize("name", CRATES)
def test_every_ad_hoc_term_used_is_defined(name: str) -> None:
    crate = CRATES[name]()
    assert _undefined_terms(crate) == []
    # control: dropping the definitions leaves the dossier and run crates' terms undefined
    bare = dict(crate, **{"@graph": [e for e in crate["@graph"] if e["@type"] != "rdf:Property"]})
    uses_terms = name.startswith(("dossier", "run crate"))
    assert bool(_undefined_terms(bare)) == uses_terms


def test_a_term_definition_reads_as_an_rdfs_label_and_comment() -> None:
    """A definition is only one if a JSON-LD reader sees rdf:Property, rdfs:label and
    rdfs:comment: any ``x:y`` key reads as an absolute IRI, so a mistyped key is
    "mapped" and the dropped-key check cannot see it."""
    [definition] = ro_crate.term_definitions([{"@id": "#a", "score": 1}])
    doc = {"@context": ro_crate.context(), "@graph": [definition]}
    [node] = _expand(doc)
    rdfs = "http://www.w3.org/2000/01/rdf-schema#"
    assert node["@id"] == ro_crate.VOCAB_BASE + "score"
    assert node["@type"] == ["http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"]
    assert node[rdfs + "label"] == [{"@value": "score"}]
    assert node[rdfs + "comment"] == [{"@value": ro_crate.TERMS["score"]}]
    # control: a term the graph does not use gets no definition
    assert ro_crate.term_definitions([{"@id": "#a", "name": "n"}]) == []


@pytest.fixture
def far_east_host(monkeypatch: pytest.MonkeyPatch):
    """The host clock at UTC+14 (Kiribati), where local and UTC dates differ for ten
    hours a day; restored after the test."""
    monkeypatch.setenv("TZ", "Etc/GMT-14")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_crate_dates_are_utc_whatever_the_host_zone(far_east_host: None) -> None:
    # 2026-10-03 21:30 UTC is already 2026-10-04 11:30 on this host
    assert datetime.fromtimestamp(NOW.timestamp()).date() == date(2026, 10, 4)
    bare = DataResource(id="pdb:4HHB", source="pdb", kind="dataset", title="t")
    root = ro_crate.render(bare, created=NOW)["@graph"][1]
    assert root["datePublished"] == "2026-10-03"
    assert ro_crate.timestamp(NOW) == "2026-10-03T21:30:00Z"
    # a clock given in another zone is the same instant, written in UTC
    assert ro_crate.timestamp(NOW.astimezone(UTC).astimezone()) == "2026-10-03T21:30:00Z"


def test_every_term_is_documented_at_its_iri() -> None:
    headings = set(re.findall(r"^## (\S+)$", _VOCAB_DOC, re.M))
    assert headings == set(ro_crate.TERMS)
    assert ro_crate.VOCAB_BASE.endswith("/docs/vocab.md#")


# --- live: the validator itself --------------------------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def _validate(crate: dict[str, Any], tmp: Path) -> list[str]:
    """rocrate-validator's REQUIRED-level failures for ``crate`` (check identifiers)."""
    tmp.mkdir()
    (tmp / "ro-crate-metadata.json").write_text(json.dumps(crate))
    report = tmp / "report.json"
    validator = Path(sys.executable).parent / "rocrate-validator"
    args = [str(validator), "validate", str(tmp), "-p", "ro-crate-1.1", "-m"]
    args += ["--skip-availability-check", "-l", "required", "-f", "json", "--no-paging"]
    args += ["-nc", "-o", str(report)]
    subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)
    # the report carries terminal colour codes even when written to a file
    parsed = json.loads(re.sub(r"\x1b\[[0-9;]*m", "", report.read_text()))
    return sorted(issue["check"]["identifier"] for issue in parsed["issues"])


@_live_only
@pytest.mark.parametrize("name", CRATES)
def test_live_rocrate_validator_passes_each_crate(name: str, tmp_path: Path) -> None:
    crate = CRATES[name]()
    assert _validate(crate, tmp_path / "crate") == []
    # control: the same validator fails the crate once its root loses its description
    broken = [
        {k: v for k, v in e.items() if k != "description"} if e["@id"] == "./" else e
        for e in crate["@graph"]
    ]
    assert _validate(dict(crate, **{"@graph": broken}), tmp_path / "broken") != []
