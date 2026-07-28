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
import re
import time

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
    # This asserted `"patent-use" not in prof.limitations` on the grounds that "OGL is
    # silent on patents". The licence text is not silent: v3.0's exemption list carves out
    # "other intellectual property rights, including patents, trade marks, and design
    # rights". The original claim was wrong about the licence, so the profile and this
    # assertion were corrected together on 2026-07-28 rather than the test being relaxed.
    assert "patent-use" in prof.limitations
    verdict = lc.check("OGL-UK-3.0", "commercial")
    assert verdict.spdx_id == "OGL-UK-3.0" and verdict.verdict == "ALLOW"


def test_intents_reference_real_permission_flags():
    for use, required in lc.INTENTS.items():
        for flag in required:
            assert flag in lc.PERMISSION_FLAGS, f"{use} → {flag}"


_PRE_4_0_CC_IDS = [
    f"{family}-{version}"
    for family in (
        "CC-BY",
        "CC-BY-SA",
        "CC-BY-ND",
        "CC-BY-NC",
        "CC-BY-NC-SA",
        "CC-BY-NC-ND",
    )
    for version in ("1.0", "2.0", "2.5", "3.0")
]


@pytest.mark.parametrize("spdx", _PRE_4_0_CC_IDS)
def test_pre_4_0_cc_is_assessed_not_merely_identified(spdx):
    """Every pre-4.0 CC id carries a profile, so it is assessed rather than REVIEWed.

    Before this, `CC BY 3.0` normalized to a valid SPDX id but had no profile, so a
    licence that plainly grants commercial use returned REVIEW — the same
    conservative-but-misleading answer that source-level blanket licences rejected.
    """
    assert spdx in lc.LICENSE_MATRIX
    assert lc.check(spdx, "commercial").verdict in {"ALLOW", "DENY"}


@pytest.mark.parametrize("family", ["CC-BY", "CC-BY-SA", "CC-BY-ND", "CC-BY-NC", "CC-BY-NC-ND"])
@pytest.mark.parametrize("version", ["1.0", "2.0", "2.5", "3.0"])
def test_pre_4_0_cc_agrees_with_its_4_0_counterpart_on_every_intent(family, version):
    """The grants a CC family makes did not change across versions, so the verdicts must not.

    This is the substantive claim behind hand-encoding these: for the MODELED
    vocabulary a 3.0 licence answers exactly as its 4.0 counterpart does.
    """
    for use in lc.INTENTS:
        older = lc.check(f"{family}-{version}", use)
        current = lc.check(f"{family}-4.0", use)
        assert older.verdict == current.verdict, f"{family}-{version} vs 4.0 on {use}"


@pytest.mark.parametrize("spdx", _PRE_4_0_CC_IDS)
def test_pre_4_0_cc_does_not_assert_patent_or_trademark_exclusions(spdx):
    """Pre-4.0 CC is SILENT on patents and on the licensor's trademarks.

    4.0 added "Patent and trademark rights are not licensed under this Public
    License"; 1.0-3.0 have no equivalent, and their only trademark clause disclaims
    *Creative Commons'* marks. Copying 4.0's limitations wholesale would assert an
    exclusion the text never makes — the same reasoning that omits `patent-use` from
    OGL-UK-3.0. Warranty and liability ARE disclaimed, so they stay.
    """
    prof = lc.LICENSE_MATRIX[spdx]
    assert "patent-use" not in prof.limitations
    assert "trademark-use" not in prof.limitations
    assert prof.limitations == frozenset({"liability", "warranty"})
    # ...and this is a real difference from 4.0, not a copy of it.
    assert "patent-use" in lc.LICENSE_MATRIX[f"{spdx.rsplit('-', 1)[0]}-4.0"].limitations


def test_intent_vocabulary_is_pinned_because_pre_4_0_profiles_depend_on_it():
    """Guard: the pre-4.0 CC profiles are only accurate for THIS intent vocabulary.

    Those licences genuinely differ from 4.0 on attribution mechanics and the
    DRM-circumvention clause. Those deltas fall outside the choosealicense flag
    vocabulary, which is why approximating them is defensible today. Add an intent
    that turns on either one and the approximation silently becomes wrong — there is
    nothing in the data structure that would notice.

    So the intent set is pinned here. If this test fails you are changing INTENTS:
    re-read the pre-4.0 legal code for the areas your new intent touches and either
    encode the difference or drop those profiles. Do not just update the constant.
    """
    assert lc.INTENTS == {
        "commercial": ("commercial-use",),
        "redistribute": ("distribution",),
        "modify": ("modifications",),
        "ml-training": ("commercial-use", "modifications"),
    }


def test_ported_cc_ids_fold_onto_the_unported_profile():
    """SPDX has jurisdiction ports (CC-BY-3.0-US); the normalizer folds them onto the
    unported id, so the hand-encoded profile answers for them too. Pinned because it is
    the assumption most likely to be wrong for a ported jurisdiction."""
    assert lc.normalize_spdx("CC BY 3.0 US") == "CC-BY-3.0"
    assert lc.normalize_spdx("https://creativecommons.org/licenses/by/3.0/us/") == "CC-BY-3.0"
    assert lc.check("CC BY 3.0 US", "commercial").verdict == "ALLOW"


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
    """The point of this guard is that nothing I/O-shaped reaches ``check``. Source-level
    blanket licences added two pure keyword-only params, so the assertion is now: the
    positional signature is unchanged, and every extra param is keyword-only, optional,
    and a plain string — a client could not be passed without failing this."""
    params = inspect.signature(lc.check).parameters
    positional = [
        n for n, p in params.items() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert positional == ["license_str", "use"]
    for name, p in params.items():
        if name in positional:
            continue
        assert p.kind is p.KEYWORD_ONLY, name
        assert p.default is None, name
        assert p.annotation == "str | None", name


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
    "spoofed",
    [
        # userinfo: everything before "@" is a username, never the host.
        "http://creativecommons.org@evil.example.com/licenses/by/4.0/",
        "https://creativecommons.org:8080@evil.example.com/licenses/by/4.0/",
        "https://creativecommons.org:65535@evil.example.com/licenses/by/4.0/",
        "http://user:pw@creativecommons.org@evil.example.com/licenses/by/4.0/",
        # fragment / query: content after "#" or "&" is not a host either.
        "https://evil.example.com#creativecommons.org/licenses/by/4.0/",
        "https://evil.example.com/?a=1&b=creativecommons.org/licenses/by/4.0/",
        "https://evil.example.com#opendatacommons.org/licenses/odbl/",
    ],
)
def test_licence_domain_cannot_be_spoofed_by_url_structure(spoofed: str) -> None:
    """The vectors the substring fix above did NOT close.

    `host_matches` killed "domain sits in someone else's path", and the test above pins
    that. But the scanner finds host-shaped tokens anywhere, so it split ONE url into TWO
    at a character it could not consume — and each half then looked like a standalone url:
    the userinfo of `creativecommons.org@evil.example.com` was read as the host, as was the
    fragment of `evil.example.com#creativecommons.org/...`. Every input here returned a
    real `ALLOW CC-BY-4.0` for a url pointing at an attacker's site.

    Generalizable: a fix that repels the attack you thought of can still be partial. This
    one guarded the matcher while the TOKENIZER stayed confused.
    """
    assert lc.normalize_spdx(spoofed) is None
    # The harm was never the id in isolation — it was the grant that followed.
    assert lc.check(spoofed, "commercial").verdict != "ALLOW"


def test_real_licence_urls_still_resolve() -> None:
    """Positive control for both spoofing tests.

    Rejecting everything would satisfy them and break the feature, so the legitimate
    shapes are asserted in their own right: scheme'd, scheme-less, and mentioned in prose.
    """
    assert lc.normalize_spdx("https://creativecommons.org/licenses/by/4.0/") == "CC-BY-4.0"
    assert lc.normalize_spdx("creativecommons.org/publicdomain/zero/1.0") == "CC0-1.0"
    assert lc.normalize_spdx("http://creativecommons.org/licenses/by-nc/3.0/") == "CC-BY-NC-3.0"
    assert (
        lc.normalize_spdx("Licensed under https://creativecommons.org/licenses/by-sa/4.0/ terms")
        == "CC-BY-SA-4.0"
    )
    assert lc.host_matches("https://wiki.creativecommons.org/x", "creativecommons.org")
    assert lc.check("https://creativecommons.org/licenses/by/4.0/", "commercial").verdict == "ALLOW"


def test_host_scan_does_not_blow_up_on_hostile_input() -> None:
    """The host scanner was quadratic, and licence text is attacker-supplied.

    `(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\\.)+` nests a star inside a plus, so "by-by-by-…"
    backtracked: 12 KB cost ~1.5 s of CPU, and `host_matches` runs it three times per
    `normalize_spdx`. Anyone can upload a record with a licence field on Zenodo,
    HuggingFace or OpenML, so a search page of such records was minutes of wall clock.

    The bound is deliberately loose — 40x the measured post-fix cost — because this must
    fail on a quadratic regression, not on a slow CI runner.
    """
    payload = "cc " + ("by-" * 4000) + "4.0"  # 12 KB, the size that used to cost ~1.5 s
    start = time.perf_counter()
    lc.normalize_spdx(payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"host scan took {elapsed:.2f}s on 12 KB — quadratic regression?"


def test_host_scan_is_length_capped() -> None:
    """Belt and braces with the bounded regex: past the cap we are looking at licence
    TEXT, not an identifier, and an attacker-supplied field has no natural size limit."""
    buried = "x" * (lc._MAX_SCAN_CHARS + 10) + " https://creativecommons.org/licenses/by/4.0/"
    assert lc.url_hosts(buried) == []
    # Immediately before the cap it is still found — the cap is the reason, not an accident.
    near = "x" * 100 + " https://creativecommons.org/licenses/by/4.0/"
    assert "creativecommons.org" in lc.url_hosts(near)


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


def test_identified_but_unassessed_reports_the_id_and_reviews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identified-but-unassessed branch, exercised by REMOVING a profile.

    This test has now been hollowed out twice by the licence-coverage work: it used
    CC-BY-SA-3.0 until the pre-4.0 CC family was encoded, then OGL-UK-1.0 until the OGL
    family was. Both times the licence it relied on became assessed and the test quietly
    degraded into a test of nothing. Repointing it a third time would just queue up the
    same failure, and there is nothing left to repoint it AT — see
    test_every_identifiable_licence_is_also_assessable, which pins that the unassessed
    set is now empty.

    So stop sourcing the negative example from the real world. The branch's actual job is
    to catch a FUTURE identifier added without a matching profile, and deleting an entry
    from the matrix simulates exactly that — while staying valid no matter how much
    licence coverage grows. Rule: a test for a defensive branch should construct its own
    trigger, not borrow one from production data that someone is actively trying to fix.
    """
    monkeypatch.delitem(lc.LICENSE_MATRIX, "CC-BY-4.0")
    v = lc.check("https://creativecommons.org/licenses/by/4.0/", "redistribute")
    assert v.verdict == "REVIEW"
    assert v.spdx_id == "CC-BY-4.0"  # identified...
    assert "CC-BY-4.0" not in lc.LICENSE_MATRIX  # ...but (for this test) not assessed
    assert "no compatibility profile" in v.reason
    # No flags were invented for it.
    assert "grants" not in v.reason and "does not grant" not in v.reason


def test_identified_but_unassessed_positive_control() -> None:
    """The negative above only means something if the same input passes when assessed.

    Without this, a normalize_spdx regression that broke CC-BY-4.0 identification would
    leave the test above passing for entirely the wrong reason.
    """
    v = lc.check("https://creativecommons.org/licenses/by/4.0/", "redistribute")
    assert v.verdict == "ALLOW"
    assert v.spdx_id == "CC-BY-4.0"
    assert "no compatibility profile" not in v.reason


def test_stated_but_unrecognized_reads_differently_from_not_stated() -> None:
    """The whole point of the fix: these are different facts and must not share a sentence.

    A caller told "not stated" has nothing to chase. A caller told the record said
    'Public' has one concrete lead. Both still REVIEW — the verdict was never the problem.
    """
    stated = lc.check("Public", "commercial")
    silent = lc.check(None, "commercial")

    assert stated.verdict == silent.verdict == "REVIEW"
    assert stated.spdx_id is None and silent.spdx_id is None
    assert stated.reason != silent.reason, "the two states still share one message"

    assert "'Public'" in stated.reason
    assert "not recognized" in stated.reason
    # The old message claimed nothing was stated, while license_raw sat there holding the
    # value. That contradiction is the bug.
    assert "not stated" not in stated.reason
    assert stated.license_raw == "Public"

    assert "not stated" in silent.reason
    assert silent.license_raw is None


def test_openml_public_is_reported_but_not_promoted() -> None:
    """OpenML states `licence: 'Public'` on every dataset — measured live 2026-07-28.

    REVIEW is the correct verdict and must stay: "publicly available" is not "public
    domain", so promoting this to CC0-1.0 would invent a specific grant from a vague word.
    This test exists to make that a deliberate decision rather than an oversight someone
    later "fixes".
    """
    for intent in lc.INTENTS:
        v = lc.check("Public", intent)
        assert v.verdict == "REVIEW", f"{intent}: {v.reason}"
        assert v.spdx_id is None, "'Public' must not be promoted to an SPDX id"
        assert "'Public'" in v.reason


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "\n\t "],
)
def test_whitespace_only_licence_counts_as_not_stated(raw: str) -> None:
    """A field holding only whitespace stated nothing, however non-empty it looks."""
    v = lc.check(raw, "commercial")
    assert v.verdict == "REVIEW"
    assert "not stated" in v.reason
    assert "stated as" not in v.reason


def test_unrecognized_licence_excerpt_is_bounded_and_single_line() -> None:
    """A licence field is arbitrary upstream text — it can be a paragraph, or carry
    newlines that would wreck a one-line reason. Neither may leak into the message."""
    blob = "Terms of use:\n" + ("x" * 500)
    v = lc.check(blob, "commercial")
    assert "\n" not in v.reason
    assert len(v.reason) < 200, "an upstream blob is being echoed wholesale"
    assert "…" in v.reason, "a long value should be visibly truncated, not silently cut"
    # Truncated in the message, but preserved in full where callers can still read it.
    assert v.license_raw == blob


def test_a_stated_but_unrecognized_licence_does_not_fall_back_to_the_source_default() -> None:
    """The default fills silence only. A record that stated something we could not parse
    has NOT stated nothing — treating it as silent would let a permissive archive default
    speak over a record whose actual terms we failed to read."""
    v = lc.check("Public", "commercial", source_default="CC0-1.0", source_policy=_WWPDB)
    assert v.verdict == "REVIEW"
    assert v.spdx_id is None
    assert v.license_raw == "Public"
    assert "blanket policy" not in v.reason


def test_every_identifiable_licence_is_also_assessable() -> None:
    """If we can name it, we should be able to assess it.

    Encoding the OGL family emptied the identified-but-unassessed set: every id the
    normalizer can emit now has a profile. That is worth pinning, because the failure it
    guards is silent — adding an alias or URL pattern without a matching LICENSE_MATRIX
    entry degrades a plainly-stated licence to REVIEW, which reads as caution rather than
    as the gap it is.

    Deliberately NOT asserting the reverse (matrix entries with no way to reach them):
    matrix keys are matched directly against normalized ids, so an unaliased entry is
    still reachable by its bare SPDX id.
    """
    reachable = set(lc._PROSE_ALIASES.values())
    for version in (1, 2, 3):
        ogl = lc.normalize_spdx(
            f"http://www.nationalarchives.gov.uk/doc/open-government-licence/version/{version}/"
        )
        assert ogl is not None
        reachable.add(ogl)
    for family in ("by", "by-sa", "by-nc", "by-nd", "by-nc-sa", "by-nc-nd"):
        for version in ("1.0", "2.0", "2.5", "3.0", "4.0"):
            cc = lc.normalize_spdx(f"https://creativecommons.org/licenses/{family}/{version}/")
            assert cc is not None
            reachable.add(cc)

    unassessed = sorted(i for i in reachable if i not in lc.LICENSE_MATRIX)
    assert unassessed == [], (
        f"these ids can be identified but carry no compatibility profile: {unassessed}. "
        f"Either hand-encode them from the licence text, or record here why they are "
        f"deliberately left unassessed."
    )


def test_ogl_asserts_patent_and_trademark_exclusions_unlike_pre_4_0_cc() -> None:
    """The one place the OGL and pre-4.0 CC profiles legitimately disagree.

    Both are hand-encoded under the same "silence is not an explicit exclusion" rule, and
    they land on opposite answers because the texts differ, not because the rule was
    applied inconsistently: every OGL version's exemption list names "other intellectual
    property rights, including patents, trade marks, and design rights", while pre-4.0 CC
    says nothing about patents at all.
    """
    for version in ("1.0", "2.0", "3.0"):
        prof = lc.LICENSE_MATRIX[f"OGL-UK-{version}"]
        assert prof.limitations == frozenset(
            {"liability", "warranty", "trademark-use", "patent-use"}
        ), f"OGL-UK-{version}"
        # The grant itself is unrestricted commercial reuse with attribution.
        assert prof.permissions == frozenset(
            {"commercial-use", "modifications", "distribution", "private-use"}
        ), f"OGL-UK-{version}"
        assert prof.conditions == frozenset({"include-copyright"}), f"OGL-UK-{version}"

    # Contrast, so this stays a real comparison rather than a restatement of the matrix.
    assert "patent-use" not in lc.LICENSE_MATRIX["CC-BY-3.0"].limitations


@pytest.mark.parametrize("version", ["1.0", "2.0", "3.0"])
def test_ogl_is_assessed_for_every_intent(version: str) -> None:
    """OGL is an unrestricted-with-attribution licence, so no intent should REVIEW."""
    for intent in lc.INTENTS:
        v = lc.check(f"OGL-UK-{version}", intent)
        assert v.verdict == "ALLOW", f"OGL-UK-{version} / {intent}: {v.reason}"
        assert v.spdx_id == f"OGL-UK-{version}"


def test_bare_ogl_still_refuses_to_guess_a_version() -> None:
    """All three OGL versions now share a profile, which makes guessing tempting.

    It is still wrong: spdx_id is the field callers cite, and answering "OGL-UK-3.0" for
    a source that only said "Open Government Licence" attributes a version the source
    never stated.
    """
    assert lc.normalize_spdx("Open Government Licence") is None
    assert lc.normalize_spdx("OGL") is None


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


# --- Source-level blanket licences ------------------------------------------------------
# Measuring all 17 sources found the real gap is licence ABSENCE, not misparse: 10 of them
# state no licence at all, and resolve confirms it is genuinely absent rather than a
# search-time omission. For a source whose operator dedicates the whole archive under one
# licence, answering "all-rights-reserved" is a wrong answer, not a safe one.

_WWPDB = "wwPDB usage policy — https://www.wwpdb.org/about/usage-policies"
_CELLXGENE = (
    "CZ CELLxGENE Discover publishing policy — "
    "https://cellxgene.cziscience.com/docs/032__Contribute%20and%20Publish%20Data"
)


def test_source_default_is_used_when_the_record_states_nothing() -> None:
    v = lc.check(None, "commercial", source_default="CC0-1.0", source_policy=_WWPDB)
    assert v.verdict == "ALLOW"
    assert v.spdx_id == "CC0-1.0"
    # The RECORD said nothing, and the verdict must not imply it did.
    assert v.license_raw is None
    assert "states no licence" in v.reason and "blanket policy" in v.reason
    # The WHOLE citation has to survive into the reason, not merely a domain somewhere in
    # it — a bare-domain substring check is both a weaker assertion and the shape CodeQL
    # flags as incomplete URL sanitization (py/incomplete-url-substring-sanitization).
    assert _WWPDB in v.reason


def test_a_licence_on_the_record_always_beats_the_source_default() -> None:
    """The default fills a hole; it never overrides. A permissive archive default must not
    launder a record that carries a more restrictive licence of its own."""
    v = lc.check("CC-BY-NC-4.0", "commercial", source_default="CC0-1.0", source_policy=_WWPDB)
    assert v.verdict == "DENY"
    assert v.spdx_id == "CC-BY-NC-4.0"
    assert v.license_raw == "CC-BY-NC-4.0"
    assert "blanket policy" not in v.reason


def test_without_a_source_default_the_answer_is_unchanged() -> None:
    """The control: absent a default, an unstated licence still reviews as before."""
    v = lc.check(None, "commercial")
    assert v.verdict == "REVIEW" and v.spdx_id is None
    assert "not stated" in v.reason


def test_registry_defaults_are_only_the_ones_with_a_verified_policy() -> None:
    from data_aggregator_mcp import sources

    assert sources.default_license_for("pdb") == ("CC0-1.0", _WWPDB)
    assert sources.default_license_for("uniprot") == (
        "CC-BY-4.0",
        "UniProt licence — https://www.uniprot.org/help/license",
    )
    assert sources.default_license_for("cellxgene") == ("CC-BY-4.0", _CELLXGENE)
    # GWAS is mostly CC0 but individual studies carry their own Usage License, so a blanket
    # default would be wrong exactly where it matters. Deliberately absent.
    assert sources.default_license_for("gwas") == (None, None)
    assert sources.default_license_for("nope") == (None, None)
    assert sources.default_license_for(None) == (None, None)


@pytest.mark.parametrize("source", ["dataone", "omicsdi", "omics", "biostudies", "gwas"])
def test_sources_without_a_blanket_grant_stay_absent(source: str) -> None:
    """Five sources state no per-record licence yet still get NO default, and the reason is
    the same in every case: nobody with authority granted one.

    - dataone / omicsdi federate other repositories, so the terms are the member repo's.
    - omics (NCBI) and biostudies (EMBL-EBI) both publish the *same* careful wording —
      they place no ADDITIONAL restrictions beyond the original data owner's. NCBI goes
      further and says outright that it has no rights to transfer.

    "No additional restrictions" is not permission. Defaulting a licence here would invent
    a grant the operator explicitly declined to make, which is the one thing this module
    refuses to do — so this test exists to keep a future coverage push from "fixing" them.
    """
    from data_aggregator_mcp import sources

    assert sources.default_license_for(source) == (None, None)


def test_cellxgene_default_answers_the_intent_it_was_added_for() -> None:
    """Wired end to end the way server.py does it, not just asserted in the registry.

    Before this, every cellxgene record returned REVIEW "all-rights-reserved" for every
    intent, because the curation API exposes no licence field on any of its 386 published
    collections — so the archive that publishes ALL its data under CC-BY 4.0 was our most
    consistently pessimistic answer.
    """
    from data_aggregator_mcp import sources

    default_lic, policy = sources.default_license_for("cellxgene")
    for intent in lc.INTENTS:
        v = lc.check(None, intent, source_default=default_lic, source_policy=policy)
        assert v.verdict == "ALLOW", f"{intent}: {v.reason}"
        assert v.spdx_id == "CC-BY-4.0"
        # The record itself said nothing, and that must stay visible.
        assert v.license_raw is None
        # The citation has to reach the caller — an uncited blanket grant is the thing the
        # registry comment warns against. Compared whole rather than by URL substring.
        assert _CELLXGENE in v.reason


def test_every_declared_default_is_assessable_and_cited() -> None:
    """A typo'd default would silently degrade to REVIEW, and an uncited one is a claim
    about someone else's data with nothing behind it. Both fail loud here instead."""
    from data_aggregator_mcp import sources

    for name, (lic, policy) in sources.DEFAULT_LICENSES.items():
        assert lc.normalize_spdx(lic) == lic, f"{name}: {lic!r} is not a canonical SPDX id"
        assert lic in lc.LICENSE_MATRIX, f"{name}: {lic!r} has no compatibility profile"
        assert policy and "https://" in policy, f"{name}: default licence has no citation"


@_live_only
@pytest.mark.asyncio
async def test_live_cellxgene_really_states_no_licence() -> None:
    """The cellxgene default rests on a claim about someone else's API — check it for real.

    A source default is only correct while the source stays silent. The moment CZI adds a
    licence field, a record-stated licence would start winning (which is the designed
    behaviour), and the interesting case becomes a record that disagrees with the blanket
    policy. This fails loudly at that transition instead of leaving a stale assumption
    buried in a registry comment.
    """
    import httpx

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get("https://api.cellxgene.cziscience.com/curation/v1/collections")
    r.raise_for_status()
    collections = r.json()
    # Positive control: a broken harness (empty page, shape change) must not read as
    # "no licence field found".
    assert collections, "cellxgene returned no collections — harness suspect"
    assert "collection_id" in collections[0], "cellxgene payload shape changed — harness suspect"

    pattern = re.compile(r"licen[cs]|rights|terms|copyright", re.I)
    offenders: set[str] = set()
    for coll in collections:
        offenders.update(k for k in coll if pattern.search(k))
        for ds in coll.get("datasets") or []:
            offenders.update(f"datasets/{k}" for k in ds if pattern.search(k))
    assert not offenders, (
        f"cellxgene now exposes licence-ish fields {sorted(offenders)} — re-check whether the "
        f"blanket CC-BY-4.0 default is still the right answer, and whether the adapter should "
        f"read the field instead."
    )
