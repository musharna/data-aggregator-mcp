"""Provenance dossier — an RO-Crate 1.1 data-availability dossier for one record.

``render(resource)`` builds a single machine-readable artifact that COMPOSES every
provenance/integrity signal the server already computes for a resolved record:
version-currency (B1), licence + normalized SPDX (B3), FAIRness (B4), retraction /
expression-of-concern (trust), and the source/DOI/ID chain. It REUSES
``ro_crate.render`` as the base graph (metadata descriptor + root Dataset + file
entities) and EXTENDS ``@graph`` with a schema.org ``CreateAction`` provenance entity
plus one assessment ``PropertyValue`` per PRESENT signal — RO-Crate 1.1
"Provenance of entities" (a CreateAction whose ``instrument`` is the software agent
that performed the assessment, attached to the root data entity).

HONESTY (the heart of this wave): only signals actually present are represented. An
unknown retraction (``trust.retracted is None``) is reported as "unknown / not checked",
NEVER "not retracted". An unrecognized licence gets SPDX "unrecognized", never an
invented id. A missing version/FAIR/trust signal is OMITTED, not fabricated. We keep
``conformsTo`` ONLY on the metadata descriptor (RO-Crate 1.1) — no custom profile URI.

PURE: no network, no file I/O, deterministic — the handler (not this renderer) does
the FAIR/trust enrichment before calling ``render``.
"""

from __future__ import annotations

from typing import Any

from data_aggregator_mcp import __version__, ro_crate
from data_aggregator_mcp.license_compat import normalize_spdx
from data_aggregator_mcp.models import DataResource

AGENT_ID = "https://github.com/musharna/data-aggregator-mcp"
ASSESSMENT_ID = "#provenance-assessment"


def _property_value(eid: str, name: str, value: str, **extra: Any) -> dict[str, Any]:
    """A schema.org PropertyValue assessment entity. ``value`` is a human-readable
    string; ``extra`` carries machine-readable fields (e.g. normalized_spdx, score)."""
    ent: dict[str, Any] = {
        "@id": eid,
        "@type": "PropertyValue",
        "name": name,
        "value": value,
    }
    ent.update(extra)
    return ent


def _version_result(r: DataResource) -> dict[str, Any] | None:
    """Version-currency assessment — only when version info is present
    (``is_latest is not None``); absence of version links is NOT a claim of latest."""
    if r.is_latest is None:
        return None
    if r.is_latest:
        value = "this record is the latest known version"
    elif r.superseded_by:
        value = f"this record is superseded by a newer version: {r.superseded_by}"
    else:
        value = "this record is superseded by a newer version"
    extra: dict[str, Any] = {"is_latest": r.is_latest}
    if r.superseded_by:
        extra["superseded_by"] = r.superseded_by
    return _property_value("#version-currency", "version-currency", value, **extra)


def _license_result(r: DataResource) -> dict[str, Any] | None:
    """Licence assessment — only when a licence is stated. Carries the raw value and
    the normalized SPDX id; an unrecognized licence is "unrecognized", never invented."""
    if not r.license:
        return None
    spdx = normalize_spdx(r.license)
    spdx_label = spdx if spdx is not None else "unrecognized"
    value = f"stated licence {r.license!r}; normalized SPDX: {spdx_label}"
    return _property_value(
        "#licence",
        "licence",
        value,
        license_raw=r.license,
        normalized_spdx=spdx,
    )


def _fair_result(r: DataResource) -> dict[str, Any] | None:
    """FAIR assessment — only when ``resource.fair`` is attached (by the handler)."""
    fa = r.fair
    if fa is None:
        return None
    value = (
        f"FAIR score {fa.score}/100 "
        f"(F={fa.findable} A={fa.accessible} I={fa.interoperable} R={fa.reusable}); "
        f"{fa.assessed} indicators evaluated"
    )
    return _property_value(
        "#fair",
        "FAIRness",
        value,
        score=fa.score,
        findable=fa.findable,
        accessible=fa.accessible,
        interoperable=fa.interoperable,
        reusable=fa.reusable,
        assessed=fa.assessed,
        gaps=list(fa.gaps),
    )


def _retraction_result(r: DataResource) -> dict[str, Any] | None:
    """Retraction/integrity assessment — only when ``resource.trust`` is attached.

    HONESTY: ``retracted is None`` is reported as "unknown / not checked" and asserts
    NOTHING negative (never "not retracted"). A definitive ``False`` may state "no
    retraction on record (Crossref)". ``concern`` is reported the same way.
    """
    trust = r.trust
    if trust is None:
        return None

    if trust.retracted is None:
        retr = "retraction status unknown / not checked"
    elif trust.retracted:
        retr = "RETRACTED: a retraction is on record (Crossref)"
        if trust.retraction_doi:
            retr += f"; retraction notice {trust.retraction_doi}"
    else:
        retr = "no retraction on record (Crossref)"

    if trust.concern is None:
        conc = "expression-of-concern status unknown / not checked"
    elif trust.concern:
        conc = "an expression of concern is on record (Crossref)"
    else:
        conc = "no expression of concern on record (Crossref)"

    extra: dict[str, Any] = {"retracted": trust.retracted, "concern": trust.concern}
    if trust.retraction_doi:
        extra["retraction_doi"] = trust.retraction_doi
    return _property_value("#retraction", "retraction-status", f"{retr}; {conc}", **extra)


def _identifier_result(r: DataResource) -> dict[str, Any]:
    """Source/DOI/ID-chain assessment — always present (every record has a source +
    canonical id). Records the source repo, canonical id, DOI, cross-identifiers,
    accessions, and the qualified version/relation links (rel -> target)."""
    parts = [f"source repository {r.source}", f"canonical id {r.id}"]
    if r.doi:
        parts.append(f"DOI {r.doi}")
    value = "; ".join(parts)
    extra: dict[str, Any] = {"source": r.source, "canonical_id": r.id}
    if r.doi:
        extra["doi"] = r.doi
    if r.identifiers:
        extra["identifiers"] = dict(r.identifiers)
    if r.accessions:
        extra["accessions"] = list(r.accessions)
    if r.links:
        extra["links"] = [{"rel": lnk.rel, "target_id": lnk.target_id} for lnk in r.links]
    return _property_value("#identifier-chain", "source-identifier-chain", value, **extra)


def render(resource: DataResource) -> dict[str, Any]:
    """Render an RO-Crate 1.1 data-availability dossier for ``resource``. PURE,
    deterministic, no I/O. Reuses ``ro_crate.render`` as the base graph, then extends
    ``@graph`` with the provenance CreateAction + one assessment entity per present
    signal. ``conformsTo`` stays only on the metadata descriptor (RO-Crate 1.1)."""
    crate = ro_crate.render(resource)
    graph: list[dict[str, Any]] = crate["@graph"]

    agent = {
        "@id": AGENT_ID,
        "@type": "SoftwareApplication",
        "name": "data-aggregator-mcp",
        "version": __version__,
    }

    results: list[dict[str, Any]] = []
    for ent in (
        _version_result(resource),
        _license_result(resource),
        _fair_result(resource),
        _retraction_result(resource),
        _identifier_result(resource),
    ):
        if ent is not None:
            results.append(ent)

    action: dict[str, Any] = {
        "@id": ASSESSMENT_ID,
        "@type": "CreateAction",
        "name": "data-aggregator-mcp provenance assessment",
        "instrument": {"@id": AGENT_ID},
        "object": {"@id": "./"},
        "result": [{"@id": ent["@id"]} for ent in results],
    }
    if resource.last_updated:
        action["endTime"] = resource.last_updated

    # Link the assessment from the root data entity (keeps the crate navigable).
    for ent in graph:
        if ent.get("@id") == "./":
            ent["mentions"] = {"@id": ASSESSMENT_ID}
            break

    graph.extend([agent, action, *results])
    return crate
