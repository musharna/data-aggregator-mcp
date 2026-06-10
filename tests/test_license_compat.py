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
    # spdx_id None exactly when unrecognized/absent.
    assert (v2.spdx_id is None) == (
        lc.normalize_spdx(other.license) is None
        or lc.normalize_spdx(other.license) not in lc.LICENSE_MATRIX
    )

    # Surface what we actually saw (visible with -s) for the orchestrator report.
    print(
        f"\nLIVE: cc_by.license={cc_by.license!r} → {v.spdx_id} {v.verdict}; "
        f"other.license={other.license!r} → {v2.spdx_id} {v2.verdict}"
    )
