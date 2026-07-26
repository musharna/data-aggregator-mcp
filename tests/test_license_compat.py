"""Tests for the licence-compatibility preflight (license_compat.check).

check is PURE: no I/O, deterministic. It returns an ALLOW / REVIEW / DENY verdict for an
intended use of a resolved record, computed from a bundled licence matrix whose flags are
drawn verbatim from the choosealicense.com vocabulary. An unrecognized/absent licence is
REVIEW (spdx_id None) — never a fabricated ALLOW/DENY. Every verdict carries a
not-legal-advice disclaimer. An unknown intent fails loud (ValueError).
"""

from __future__ import annotations

import inspect
import os

import pytest

from data_aggregator_mcp import license_compat as lc
from data_aggregator_mcp.models import DataResource, LicenseVerdict

# --- normalize_spdx ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # bare SPDX ids (case-insensitive)
        ("MIT", "MIT"),
        ("mit", "MIT"),
        ("CC-BY-4.0", "CC-BY-4.0"),
        ("cc-by-nc-4.0", "CC-BY-NC-4.0"),
        ("Apache-2.0", "Apache-2.0"),
        ("GPL-3.0", "GPL-3.0"),
        ("CC0-1.0", "CC0-1.0"),
        # spaced / cased prose
        ("CC BY 4.0", "CC-BY-4.0"),
        ("CC BY-NC 4.0", "CC-BY-NC-4.0"),
        ("Creative Commons Attribution 4.0", "CC-BY-4.0"),
        ("Apache License 2.0", "Apache-2.0"),
        ("The MIT License", "MIT"),
        ("BSD 3-Clause", "BSD-3-Clause"),
        # CC + CC0 URLs
        ("https://creativecommons.org/licenses/by-nc/4.0/", "CC-BY-NC-4.0"),
        ("http://creativecommons.org/licenses/by/4.0", "CC-BY-4.0"),
        ("https://creativecommons.org/licenses/by-sa/4.0/", "CC-BY-SA-4.0"),
        ("creativecommons.org/publicdomain/zero/1.0", "CC0-1.0"),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "CC0-1.0"),
        # open data commons URLs
        ("https://opendatacommons.org/licenses/odbl/1-0/", "ODbL-1.0"),
        ("https://opendatacommons.org/licenses/by/1-0/", "ODC-By-1.0"),
        ("https://opendatacommons.org/licenses/pddl/1-0/", "PDDL-1.0"),
        ("https://opendatacommons.org/licenses/somethingelse/", None),
        # CC ND / SA prose (exercises every CC element branch)
        ("CC BY-ND 4.0", "CC-BY-ND-4.0"),
        ("Creative Commons Attribution-ShareAlike 4.0", "CC-BY-SA-4.0"),
        # public-domain mark is not a licence we model
        ("https://creativecommons.org/publicdomain/mark/1.0/", None),
        # CC URL with an unmodelled element combination → None
        ("https://creativecommons.org/licenses/nc/4.0/", None),
        # bare "public domain" is ambiguous prose → NOT mapped to CC0 (would fabricate ALLOW)
        ("public domain", None),
        ("Public Domain", None),
        # UK Open Government Licence v3.0: bare id (matrix key), prose, short code, and URL
        ("OGL-UK-3.0", "OGL-UK-3.0"),
        ("ogl-uk-3.0", "OGL-UK-3.0"),
        ("OGL 3.0", "OGL-UK-3.0"),
        ("OGL3", "OGL-UK-3.0"),
        ("Open Government Licence 3.0", "OGL-UK-3.0"),
        ("Open Government License 3.0", "OGL-UK-3.0"),  # US spelling
        ("http://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/", "OGL-UK-3.0"),
        # the URL handler identifies the other real OGL versions too (not matrix-profiled)
        (
            "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/2/",
            "OGL-UK-2.0",
        ),
        # bare "OGL" / versionless prose is ambiguous (v1/v2/v3 differ) → NOT mapped
        ("OGL", None),
        ("Open Government Licence", None),
        # junk / unknown / None → None
        ("see the paper", None),
        ("Contact authors", None),
        ("All rights reserved", None),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_spdx_table(raw, expected):
    assert lc.normalize_spdx(raw) == expected


# --- matrix integrity -------------------------------------------------------


def test_matrix_flags_drawn_from_documented_vocab():
    """No invented flag names: every profile flag must be in the documented choosealicense
    permission/condition/limitation vocab."""
    for spdx, prof in lc.LICENSE_MATRIX.items():
        assert prof.permissions <= lc.PERMISSION_FLAGS, f"{spdx} permissions"
        assert prof.conditions <= lc.CONDITION_FLAGS, f"{spdx} conditions"
        assert prof.limitations <= lc.LIMITATION_FLAGS, f"{spdx} limitations"


def test_ogl_uk_3_profile_and_verdict():
    """OGL-UK-3.0 is identified AND assessed: a permissive attribution licence that
    permits commercial use (ALLOW), requires attribution, and grants no trademark right."""
    prof = lc.LICENSE_MATRIX["OGL-UK-3.0"]
    assert {"commercial-use", "modifications", "distribution", "private-use"} <= prof.permissions
    assert prof.conditions == frozenset({"include-copyright"})
    assert "trademark-use" in prof.limitations
    assert "patent-use" not in prof.limitations  # OGL is silent on patents → not asserted
    verdict = lc.check("OGL-UK-3.0", "commercial")
    assert verdict.spdx_id == "OGL-UK-3.0" and verdict.verdict == "ALLOW"


def test_intents_reference_real_permission_flags():
    for use, required in lc.INTENTS.items():
        for flag in required:
            assert flag in lc.PERMISSION_FLAGS, f"{use} → {flag}"


def test_matrix_covers_required_licences():
    expected = {
        "CC0-1.0",
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "CC-BY-NC-4.0",
        "CC-BY-ND-4.0",
        "CC-BY-NC-SA-4.0",
        "CC-BY-NC-ND-4.0",
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-2.0",
        "GPL-3.0",
        "LGPL-3.0",
        "AGPL-3.0",
        "MPL-2.0",
        "ODbL-1.0",
        "ODC-By-1.0",
        "PDDL-1.0",
        "Unlicense",
    }
    assert expected <= set(lc.LICENSE_MATRIX)


def test_anchor_profiles_literal():
    # CC-BY-NC-4.0 lacks commercial-use.
    assert "commercial-use" not in lc.LICENSE_MATRIX["CC-BY-NC-4.0"].permissions
    # CC-BY-ND-4.0 lacks modifications.
    assert "modifications" not in lc.LICENSE_MATRIX["CC-BY-ND-4.0"].permissions
    # MIT has commercial-use + modifications + distribution + private-use.
    mit = lc.LICENSE_MATRIX["MIT"].permissions
    assert {"commercial-use", "modifications", "distribution", "private-use"} <= mit
    # CC0-1.0 has all permissions and no conditions.
    cc0 = lc.LICENSE_MATRIX["CC0-1.0"]
    assert {"commercial-use", "modifications", "distribution", "private-use"} <= cc0.permissions
    assert cc0.conditions == frozenset()
    # GPL-3.0 has same-license + disclose-source conditions.
    gpl = lc.LICENSE_MATRIX["GPL-3.0"].conditions
    assert "same-license" in gpl
    assert "disclose-source" in gpl
    # MPL-2.0 and LGPL-3.0 use the VERBATIM choosealicense same-license variants
    # (file-level / library-level), not the plain "same-license" flag.
    assert "same-license--file" in lc.LICENSE_MATRIX["MPL-2.0"].conditions
    assert "same-license" not in lc.LICENSE_MATRIX["MPL-2.0"].conditions
    assert "same-license--library" in lc.LICENSE_MATRIX["LGPL-3.0"].conditions
    assert "same-license" not in lc.LICENSE_MATRIX["LGPL-3.0"].conditions


# --- check verdict matrix ---------------------------------------------------


def test_mit_commercial_allow():
    v = lc.check("MIT", "commercial")
    assert v.verdict == "ALLOW"
    assert v.spdx_id == "MIT"


def test_mit_ml_training_allow():
    assert lc.check("MIT", "ml-training").verdict == "ALLOW"


def test_cc_by_nc_commercial_deny_names_clause():
    v = lc.check("CC-BY-NC-4.0", "commercial")
    assert v.verdict == "DENY"
    assert v.spdx_id == "CC-BY-NC-4.0"
    assert "commercial-use" in v.reason
    assert "NonCommercial" in v.reason


def test_cc_by_nc_redistribute_allow_with_nc_note():
    # NC grants distribution → ALLOW, but the reason must warn the use stays non-commercial
    # (honesty: redistribution of an NC dataset must itself be non-commercial).
    v = lc.check("CC-BY-NC-4.0", "redistribute")
    assert v.verdict == "ALLOW"
    assert "non-commercial" in v.reason.lower()


def test_permissive_allow_has_no_nc_note():
    # A non-NC licence must NOT carry the NonCommercial note.
    assert "non-commercial" not in lc.check("MIT", "redistribute").reason.lower()


def test_cc_by_nd_modify_deny():
    v = lc.check("CC-BY-ND-4.0", "modify")
    assert v.verdict == "DENY"
    assert "modifications" in v.reason
    assert "NoDerivatives" in v.reason


def test_cc_by_nd_ml_training_deny():
    # ml-training needs modifications; ND lacks it.
    v = lc.check("CC-BY-ND-4.0", "ml-training")
    assert v.verdict == "DENY"
    assert "modifications" in v.reason


def test_gpl3_redistribute_review_copyleft():
    v = lc.check("GPL-3.0", "redistribute")
    assert v.verdict == "REVIEW"
    assert v.spdx_id == "GPL-3.0"
    assert "same-license" in v.reason or "disclose-source" in v.reason


def test_gpl3_commercial_allow():
    # bare commercial check on copyleft stays ALLOW.
    assert lc.check("GPL-3.0", "commercial").verdict == "ALLOW"


def test_mpl_lgpl_redistribute_review_via_variant_copyleft():
    # The file-level/library-level same-license variants must still drive the copyleft
    # downgrade — a regression guard for the verbatim-flag fix.
    mpl = lc.check("MPL-2.0", "redistribute")
    assert mpl.verdict == "REVIEW"
    assert "same-license--file" in mpl.reason or "disclose-source" in mpl.reason
    lgpl = lc.check("LGPL-3.0", "redistribute")
    assert lgpl.verdict == "REVIEW"
    assert "same-license--library" in lgpl.reason or "disclose-source" in lgpl.reason


def test_cc0_all_intents_allow():
    for use in lc.INTENTS:
        v = lc.check("CC0-1.0", use)
        assert v.verdict == "ALLOW", f"{use} → {v.verdict}"
        assert v.spdx_id == "CC0-1.0"


def test_all_rights_reserved_review():
    v = lc.check("All rights reserved", "commercial")
    assert v.verdict == "REVIEW"
    assert v.spdx_id is None
    assert "not stated" in v.reason or "not recognized" in v.reason


def test_none_licence_review():
    v = lc.check(None, "commercial")
    assert v.verdict == "REVIEW"
    assert v.spdx_id is None
    assert v.license_raw is None


def test_unrecognized_prose_review_spdx_none():
    v = lc.check("see the paper", "modify")
    assert v.verdict == "REVIEW"
    assert v.spdx_id is None


def test_unknown_use_raises_valueerror():
    with pytest.raises(ValueError):
        lc.check("MIT", "teleport")


# --- cross-cutting invariants ----------------------------------------------


@pytest.mark.parametrize(
    ("lic", "use"),
    [
        ("MIT", "commercial"),
        ("CC-BY-NC-4.0", "commercial"),
        ("GPL-3.0", "redistribute"),
        (None, "modify"),
        ("nonsense", "ml-training"),
    ],
)
def test_disclaimer_always_present(lic, use):
    v = lc.check(lic, use)
    assert v.disclaimer
    assert "not legal advice" in v.disclaimer.lower()


def test_spdx_none_iff_unrecognized_or_absent():
    # recognized → spdx set; unrecognized/absent → None.
    assert lc.check("MIT", "commercial").spdx_id == "MIT"
    assert lc.check("see the paper", "commercial").spdx_id is None
    assert lc.check(None, "commercial").spdx_id is None


def test_license_raw_is_input_string():
    assert lc.check("CC BY 4.0", "commercial").license_raw == "CC BY 4.0"
    assert lc.check(None, "commercial").license_raw is None


def test_verdict_is_a_license_verdict_model():
    assert isinstance(lc.check("MIT", "commercial"), LicenseVerdict)


# --- purity / determinism / signature --------------------------------------


def test_check_is_deterministic():
    a = lc.check("CC-BY-NC-4.0", "ml-training")
    b = lc.check("CC-BY-NC-4.0", "ml-training")
    assert a.model_dump() == b.model_dump()


def test_check_signature_is_two_positional_no_client():
    params = list(inspect.signature(lc.check).parameters)
    assert params == ["license_str", "use"]


def test_module_does_no_network_io():
    # PURE: the module must not pull in a network client.
    assert "httpx" not in dir(lc)
    src = inspect.getsource(lc)
    assert "import httpx" not in src
    assert "httpx" not in src


# --- server wiring ----------------------------------------------------------


def test_resolve_input_schema_has_use():
    from data_aggregator_mcp import server

    resolve = next(t for t in server.TOOLS if t.name == "resolve")
    props = resolve.inputSchema["properties"]
    assert "use" in props
    assert props["use"]["type"] == "string"
    assert "use" not in resolve.inputSchema.get("required", [])
    # documents the four intents
    desc = props["use"]["description"].lower()
    for intent in ("commercial", "redistribute", "modify", "ml-training"):
        assert intent in desc


def test_dataresource_has_optional_license_compat_field():
    r = DataResource(id="x:1", source="x", kind="dataset", title="t")
    assert r.license_compat is None


def test_handler_attaches_license_compat_and_model_dump_carries_it():
    r = DataResource(
        id="zenodo:1", source="zenodo", kind="dataset", title="t", license="CC-BY-NC-4.0"
    )
    enriched = r.model_copy(update={"license_compat": lc.check(r.license, "commercial")})
    dumped = enriched.model_dump()
    assert dumped["license_compat"] is not None
    assert dumped["license_compat"]["verdict"] == "DENY"
    assert dumped["license_compat"]["spdx_id"] == "CC-BY-NC-4.0"


def test_absent_use_leaves_license_compat_none():
    r = DataResource(id="x:1", source="x", kind="dataset", title="t", license="MIT")
    assert r.model_dump()["license_compat"] is None


# --- live real-execution check ----------------------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_check_on_real_records():
    """Run check on REAL resolved records: a CC-BY Zenodo DOI (→ commercial ALLOW) and a
    GEO record whose licence is typically NC/absent (→ DENY/REVIEW). Verify the actual
    licence string at runtime and assert normalization matched it."""
    import httpx

    from data_aggregator_mcp import router

    async with httpx.AsyncClient(timeout=60) as c:
        cc_by = await router.resolve(c, "10.5281/zenodo.3242074")
        other = await router.resolve(c, "geo:GSE100866")

    # The CC-BY record: verify the real licence string and that it normalized.
    spdx = lc.normalize_spdx(cc_by.license)
    v = lc.check(cc_by.license, "commercial")
    if spdx is not None and spdx in lc.LICENSE_MATRIX:
        assert v.spdx_id == spdx
        # Assert the ACTUAL verdict for whatever the source returned, not a forced one.
        if "commercial-use" in lc.LICENSE_MATRIX[spdx].permissions:
            assert v.verdict == "ALLOW"
        else:
            assert v.verdict == "DENY"
    else:
        assert v.verdict == "REVIEW"
        assert v.spdx_id is None

    # The second record: assert the verdict is sane for its real licence string.
    v2 = lc.check(other.license, "commercial")
    assert v2.verdict in {"ALLOW", "REVIEW", "DENY"}
    assert v2.disclaimer
    # spdx_id is None exactly when the licence could not be IDENTIFIED. Holding no
    # compatibility profile for an identified licence (e.g. CC-BY-SA-3.0) yields
    # REVIEW with the id still reported — identification and assessment are separate.
    assert (v2.spdx_id is None) == (lc.normalize_spdx(other.license) is None)
    if v2.spdx_id is not None and v2.spdx_id not in lc.LICENSE_MATRIX:
        assert v2.verdict == "REVIEW"
        assert "no compatibility profile" in v2.reason

    # Surface what we actually saw (visible with -s) for the orchestrator report.
    print(
        f"\nLIVE: cc_by.license={cc_by.license!r} → {v.spdx_id} {v.verdict}; "
        f"other.license={other.license!r} → {v2.spdx_id} {v2.verdict}"
    )


# IRON_LAW_OK


# --------------------------------------------------------------------------
# Domain checks must key on the URL HOST, not a substring
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spoofed",
    [
        "http://evil.example.com/creativecommons.org/licenses/by/4.0/",
        "https://evil.example.com/x?ref=creativecommons.org/licenses/by/4.0",
        "creativecommons.org.evil.example.com/licenses/by/4.0/",
        "http://evil.example.com/opendatacommons.org/licenses/odbl/",
        "opendatacommons.org.evil.example.com/licenses/odbl/",
    ],
)
def test_licence_domain_cannot_be_spoofed_by_substring(spoofed: str) -> None:
    """A domain appearing in someone else's path/query must not mint a licence.

    `"creativecommons.org" in low` accepted all of these and returned a real SPDX
    id, which then feeds the compatibility matrix, the access flag, and the FAIR
    score — a permissive verdict fabricated from attacker-controlled or merely
    malformed upstream metadata.
    """
    assert lc.normalize_spdx(spoofed) is None


@pytest.mark.parametrize(
    ("licence", "expected"),
    [
        ("https://creativecommons.org/licenses/by-nc/4.0/", "CC-BY-NC-4.0"),
        ("http://creativecommons.org/licenses/by/4.0", "CC-BY-4.0"),
        ("creativecommons.org/publicdomain/zero/1.0", "CC0-1.0"),
        ("https://opendatacommons.org/licenses/odbl/1-0/", "ODbL-1.0"),
        # A real host reached via subdomain still counts.
        ("https://wiki.creativecommons.org/licenses/by/4.0/", "CC-BY-4.0"),
        # Prose that merely mentions the URL must keep working.
        ("Licensed under https://creativecommons.org/licenses/by/4.0/ terms", "CC-BY-4.0"),
    ],
)
def test_legitimate_licence_urls_still_normalize(licence: str, expected: str) -> None:
    assert lc.normalize_spdx(licence) == expected


def test_host_matches_rejects_suffix_and_path_collisions() -> None:
    assert lc.host_matches("https://creativecommons.org/licenses/by/4.0/", "creativecommons.org")
    assert lc.host_matches("https://wiki.creativecommons.org/x", "creativecommons.org")
    assert not lc.host_matches("https://evil.com/creativecommons.org", "creativecommons.org")
    assert not lc.host_matches("https://creativecommons.org.evil.com/x", "creativecommons.org")
    # No URL at all -> no host, no match.
    assert not lc.host_matches("CC BY 4.0", "creativecommons.org")


def test_url_hosts_survives_malformed_input() -> None:
    """Must not raise on junk — licence fields are arbitrary upstream strings."""
    for junk in ["", "not a url", "http://", "://///", "http://[oops", "a.b" * 500]:
        assert isinstance(lc.url_hosts(junk), list)


# IRON_LAW_OK


# --------------------------------------------------------------------------
# Identification is separate from assessment
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("licence", "expected"),
    [
        # Every CC version actually published, URL form.
        ("https://creativecommons.org/licenses/by/1.0/", "CC-BY-1.0"),
        ("https://creativecommons.org/licenses/by-nc-nd/2.0/", "CC-BY-NC-ND-2.0"),
        ("creativecommons.org/licenses/by/2.5/", "CC-BY-2.5"),
        ("https://creativecommons.org/licenses/by-sa/3.0/", "CC-BY-SA-3.0"),
        ("https://creativecommons.org/licenses/by/4.0/", "CC-BY-4.0"),
        # Prose form must agree with the URL form on the version string.
        ("CC BY 3.0", "CC-BY-3.0"),
        ("CC BY-NC 2.5", "CC-BY-NC-2.5"),
        ("CC BY-SA 3.0", "CC-BY-SA-3.0"),
    ],
)
def test_all_published_cc_versions_are_identified(licence: str, expected: str) -> None:
    """Identity must not depend on whether we bundle compatibility flags.

    These previously returned None because `_canonical_spdx_for_cc` gated the id it
    had just built on LICENSE_MATRIX membership, which carries only the 4.0 line.
    `2.5` was rejected outright by both the URL and prose regexes, and the two
    disagreed on whether the captured group already included the ".0" — so the
    prose path would have produced "CC-BY-3.0.0".
    """
    assert lc.normalize_spdx(licence) == expected


@pytest.mark.parametrize(
    "elements",
    [
        ["nc"],  # no BY
        ["sa"],  # no BY
        ["by", "nd", "sa"],  # ND and SA are mutually exclusive
        ["by", "nc", "nd", "sa"],  # ditto
    ],
)
def test_combinations_creative_commons_does_not_issue_stay_none(elements: list[str]) -> None:
    """Loosening the matrix gate must not let us invent licences that do not exist."""
    assert lc._canonical_spdx_for_cc(elements, "4.0") is None


def test_identified_but_unassessed_reports_the_id_and_reviews() -> None:
    v = lc.check("https://creativecommons.org/licenses/by-sa/3.0/", "redistribute")
    assert v.verdict == "REVIEW"
    assert v.spdx_id == "CC-BY-SA-3.0"  # identified...
    assert "CC-BY-SA-3.0" not in lc.LICENSE_MATRIX  # ...but not assessed
    assert "no compatibility profile" in v.reason
    # No flags were invented for it.
    assert "grants" not in v.reason and "does not grant" not in v.reason


def test_truly_unidentifiable_still_reports_no_id() -> None:
    v = lc.check("some bespoke institutional terms", "redistribute")
    assert v.verdict == "REVIEW"
    assert v.spdx_id is None
    assert "not recognized" in v.reason


def test_assessed_licences_are_unaffected() -> None:
    """The 4.0 line and non-CC licences keep their existing verdicts."""
    assert (
        lc.check("https://creativecommons.org/licenses/by/4.0/", "redistribute").verdict == "ALLOW"
    )
    assert lc.check("CC0-1.0", "commercial").verdict == "ALLOW"
    assert lc.check("MIT", "commercial").spdx_id == "MIT"
    assert lc.check("CC-BY-ND-4.0", "modify").verdict == "DENY"


def test_dossier_reports_the_real_id_for_an_unassessed_licence() -> None:
    """dossier only wants identification, and was collateral damage of the gate."""
    from data_aggregator_mcp.dossier import _license_result

    r = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        license="https://creativecommons.org/licenses/by-sa/3.0/",
    )
    prop = _license_result(r, "")
    assert prop is not None
    assert "CC-BY-SA-3.0" in str(prop)
    assert "unrecognized" not in str(prop)


def test_normalize_spdx_strips_the_spdx_scheme_prefix():
    """DANDI publishes the id scheme-qualified as 'spdx:CC-BY-4.0'. Nothing matched that,
    so an unambiguous SPDX id was reported as an unknown licence — silently degrading
    every downstream compatibility verdict to 'unknown' for that whole source."""
    assert lc.normalize_spdx("spdx:CC-BY-4.0") == "CC-BY-4.0"
    assert lc.normalize_spdx("SPDX:CC0-1.0") == "CC0-1.0"
    assert lc.normalize_spdx("spdx: MIT") == "MIT"
    # the bare form must be unaffected
    assert lc.normalize_spdx("CC-BY-4.0") == "CC-BY-4.0"
    # a prefix with nothing behind it is still unknown, not a crash
    assert lc.normalize_spdx("spdx:") is None
    # and an unrecognized id stays unrecognized after stripping
    assert lc.normalize_spdx("spdx:NOT-A-LICENCE") is None


# --- Versionless Creative Commons prose ------------------------------------------------
# EuropePMC states its licence without a version: over 300 sampled OA records, 231 carried
# a versionless CC string and not one carried a version. Every one of them normalized to
# None, so the single largest licence-bearing path in the product reported "licence not
# stated / not recognized" for licences that were, in fact, plainly stated.


@pytest.mark.parametrize(
    "raw,family",
    [
        ("cc by", "CC-BY"),
        ("cc by-nc", "CC-BY-NC"),
        ("cc by-nc-nd", "CC-BY-NC-ND"),
        ("cc by-nc-sa", "CC-BY-NC-SA"),
        ("CC BY-SA", "CC-BY-SA"),
        ("cc-by", "CC-BY"),
        ("Creative Commons Attribution", "CC-BY"),
    ],
)
def test_versionless_cc_prose_is_identified_as_a_family(raw: str, family: str) -> None:
    assert lc.identify_cc_family(raw) == family


def test_versionless_cc_reviews_naming_the_family_without_inventing_a_version() -> None:
    v = lc.check("cc by-nc", "redistribute")
    assert v.verdict == "REVIEW"
    # There is no SPDX id for a versionless CC licence, so the field stays honest...
    assert v.spdx_id is None
    # ...but the reason must say what we do know, and why we cannot assess it.
    assert "CC-BY-NC" in v.reason
    assert "version" in v.reason
    # No version was guessed. 3.0 and 4.0 differ materially; picking one is fabrication.
    assert "4.0" not in v.reason and "3.0" not in v.reason


def test_versionless_cc_does_not_claim_the_licence_was_unstated() -> None:
    """The old reason text was self-contradicting: it reported license_raw='cc by'
    alongside 'licence not stated / not recognized'."""
    v = lc.check("cc by", "redistribute")
    assert v.license_raw == "cc by"
    assert "not stated" not in v.reason


def test_versionless_combinations_creative_commons_does_not_issue_are_not_identified() -> None:
    """Loosening identification must not invent licences. ND and SA are exclusive."""
    assert lc.identify_cc_family("cc by-nd-sa") is None
    assert lc.identify_cc_family("cc nc") is None  # every CC licence but CC0 carries BY


def test_non_cc_text_is_still_unidentified() -> None:
    assert lc.identify_cc_family("some bespoke institutional terms") is None
    assert lc.identify_cc_family("MIT") is None
    v = lc.check("some bespoke institutional terms", "redistribute")
    assert v.spdx_id is None and "not recognized" in v.reason


def test_versioned_and_assessed_licences_are_unaffected() -> None:
    """A version present anywhere still wins — the family path is a fallback only."""
    assert lc.normalize_spdx("cc by 4.0") == "CC-BY-4.0"
    assert (
        lc.check("https://creativecommons.org/licenses/by/4.0/", "redistribute").verdict == "ALLOW"
    )
    assert lc.check("CC-BY-ND-4.0", "modify").verdict == "DENY"
    assert lc.identify_cc_family("cc by 4.0") is None  # versioned: not a family fallback


@_live_only
@pytest.mark.asyncio
async def test_live_europepmc_licences_are_identified() -> None:
    """Positive control for the claim above: EuropePMC's real licence strings must now
    identify. If EuropePMC ever starts emitting versions, this still passes via
    normalize_spdx — which is the point of checking both."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={
                "query": "OPEN_ACCESS:Y",
                "format": "json",
                "resultType": "core",
                "pageSize": 100,
            },
        )
    stated = [rec["license"] for rec in r.json()["resultList"]["result"] if rec.get("license")]
    assert stated, "no EuropePMC record in the page stated a licence — harness suspect"
    unidentified = [
        s for s in stated if lc.normalize_spdx(s) is None and lc.identify_cc_family(s) is None
    ]
    assert not unidentified, f"stated licences we still discard: {sorted(set(unidentified))}"
