"""Licence-compatibility preflight — a PURE function over a normalized licence string.

``check(license_str, use)`` returns an ALLOW / REVIEW / DENY verdict for an intended
use of a resolved record, naming the governing licence clause and the canonical SPDX id.
It is computed from a BUNDLED licence matrix — no network, no file I/O, deterministic.
Unlike ``trust.annotate`` (which calls Crossref), ``check`` takes only the licence string
and the intent; there is no client argument.

The matrix flag vocabulary is sourced from **choosealicense.com** (the
``github/choosealicense.com`` ``_licenses`` front-matter, vendored into Licensee, which
powers GitHub's Licenses API), fetched 2026-06-10. Each licence carries three flag sets:

- ``permissions``: ``commercial-use``, ``modifications``, ``distribution``,
  ``private-use``, ``patent-use``
- ``conditions``: ``include-copyright``, ``document-changes``, ``disclose-source``,
  ``network-use-disclose``, ``same-license``, ``same-license--file`` (MPL weak/file-level
  copyleft), ``same-license--library`` (LGPL library-level copyleft)
- ``limitations``: ``liability``, ``warranty``, ``trademark-use``, ``patent-use``

We bundle a CURATED SUBSET covering the licences actually seen on research data. An
unrecognized or absent licence yields **REVIEW** (defaults to all-rights-reserved) —
never a fabricated ALLOW/DENY.

**Not legal advice.** Every verdict carries a disclaimer: it is a metadata-derived
compatibility *advisory* computed from the stated licence, not a legal determination.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from data_aggregator_mcp.models import LicenseVerdict

DISCLAIMER = (
    "Advisory only: this verdict is derived from the record's stated licence metadata "
    "and a bundled licence-flag matrix (choosealicense.com vocabulary). It is not legal "
    "advice and not a legal determination — verify the licence terms before relying on it."
)

# Documented choosealicense.com flag vocabulary — the matrix MUST draw flags only from
# these sets (no invented flag names). Used for matrix-integrity assertions.
PERMISSION_FLAGS = frozenset(
    {"commercial-use", "modifications", "distribution", "private-use", "patent-use"}
)
CONDITION_FLAGS = frozenset(
    {
        "include-copyright",
        "document-changes",
        "disclose-source",
        "network-use-disclose",
        "same-license",
        "same-license--file",  # MPL-2.0 file-level (weak) copyleft
        "same-license--library",  # LGPL-3.0 library-level copyleft
    }
)
LIMITATION_FLAGS = frozenset({"liability", "warranty", "trademark-use", "patent-use"})


@dataclass(frozen=True)
class LicenseProfile:
    """One licence's choosealicense-sourced permission/condition/limitation flag sets."""

    permissions: frozenset[str]
    conditions: frozenset[str]
    limitations: frozenset[str]


# --- SPDX-id → flag profile (2026-06-10) ----------------------------------------------
# Software licences (MIT/Apache/BSD/GPL/LGPL/AGPL/MPL/Unlicense/CC0) and CC-BY-4.0/-SA-4.0
# carry choosealicense.com flags VERBATIM. The CC NC/ND variants (not separately catalogued
# by choosealicense) follow the Creative Commons deed semantics, and the Open Data Commons
# licences (ODbL/ODC-By/PDDL — also not in choosealicense) are hand-encoded from the ODC
# licence texts. All flag NAMES are still drawn only from the documented vocab above.

LICENSE_MATRIX: dict[str, LicenseProfile] = {
    # Public-domain dedication: everything permitted, nothing required.
    "CC0-1.0": LicenseProfile(
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset(),
        limitations=frozenset({"liability", "trademark-use", "patent-use", "warranty"}),
    ),
    "Unlicense": LicenseProfile(
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset(),
        limitations=frozenset({"liability", "warranty"}),
    ),
    "PDDL-1.0": LicenseProfile(
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset(),
        limitations=frozenset({"liability", "warranty"}),
    ),
    # UK Open Government Licence — hand-encoded from the licence texts (not in
    # choosealicense). All three published versions grant the same shape, so they share
    # one profile: copy, publish, distribute and transmit the Information; adapt it; and
    # exploit it commercially (v2.0/v3.0 say "commercially and non-commercially"). The
    # sole condition is attribution — acknowledge the source via any attribution
    # statement the Information Provider specifies; v2.0/v3.0 add "where possible,
    # provide a link to this licence", which is the same include-copyright flag. The
    # Information is licensed "as is" and the provider "excludes all representations,
    # warranties, obligations and liabilities ... to the maximum extent permitted by
    # law", giving warranty + liability.
    #
    # patent-use is asserted here and NOT for pre-4.0 CC, which looks inconsistent until
    # you read the texts: every OGL version's exemption list explicitly carves out
    # "other intellectual property rights, including patents, trade marks, and design
    # rights". That is an EXPLICIT exclusion, so the "silence is not an explicit
    # exclusion" rule does not apply — unlike pre-4.0 CC, which says nothing about
    # patents at all. Verified against the live legalcode for v1.0, v2.0 and v3.0 on
    # 2026-07-28; the v3.0 entry previously claimed "Patents are not addressed", which
    # its own exemption list contradicts.
    #
    # All three are Open Definition compliant and each text states CC-BY interoperability
    # (v1.0 "any Creative Commons Attribution Licence"; v2.0/v3.0 name CC-BY 4.0).
    "OGL-UK-1.0": LicenseProfile(
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright"}),
        limitations=frozenset({"liability", "warranty", "trademark-use", "patent-use"}),
    ),
    "OGL-UK-2.0": LicenseProfile(
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright"}),
        limitations=frozenset({"liability", "warranty", "trademark-use", "patent-use"}),
    ),
    "OGL-UK-3.0": LicenseProfile(
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright"}),
        limitations=frozenset({"liability", "warranty", "trademark-use", "patent-use"}),
    ),
    # Creative Commons 4.0 family. Attribution = include-copyright condition.
    "CC-BY-4.0": LicenseProfile(
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright"}),
        limitations=frozenset({"liability", "trademark-use", "patent-use", "warranty"}),
    ),
    "CC-BY-SA-4.0": LicenseProfile(
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright", "same-license"}),
        limitations=frozenset({"liability", "trademark-use", "patent-use", "warranty"}),
    ),
    "CC-BY-NC-4.0": LicenseProfile(
        # NonCommercial: NO commercial-use.
        permissions=frozenset({"modifications", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright"}),
        limitations=frozenset({"liability", "trademark-use", "patent-use", "warranty"}),
    ),
    "CC-BY-ND-4.0": LicenseProfile(
        # NoDerivatives: NO modifications.
        permissions=frozenset({"commercial-use", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright"}),
        limitations=frozenset({"liability", "trademark-use", "patent-use", "warranty"}),
    ),
    "CC-BY-NC-SA-4.0": LicenseProfile(
        permissions=frozenset({"modifications", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright", "same-license"}),
        limitations=frozenset({"liability", "trademark-use", "patent-use", "warranty"}),
    ),
    "CC-BY-NC-ND-4.0": LicenseProfile(
        permissions=frozenset({"distribution", "private-use"}),
        conditions=frozenset({"include-copyright"}),
        limitations=frozenset({"liability", "trademark-use", "patent-use", "warranty"}),
    ),
    # Permissive software licences.
    "MIT": LicenseProfile(
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright"}),
        limitations=frozenset({"liability", "warranty"}),
    ),
    "Apache-2.0": LicenseProfile(
        permissions=frozenset(
            {"commercial-use", "modifications", "distribution", "private-use", "patent-use"}
        ),
        conditions=frozenset({"include-copyright", "document-changes"}),
        limitations=frozenset({"liability", "trademark-use", "warranty"}),
    ),
    "BSD-2-Clause": LicenseProfile(
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright"}),
        limitations=frozenset({"liability", "warranty"}),
    ),
    "BSD-3-Clause": LicenseProfile(
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright"}),
        limitations=frozenset({"liability", "warranty"}),
    ),
    # Copyleft software licences: disclose-source + same-license conditions.
    "GPL-2.0": LicenseProfile(
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset(
            {"include-copyright", "document-changes", "disclose-source", "same-license"}
        ),
        limitations=frozenset({"liability", "warranty"}),
    ),
    "GPL-3.0": LicenseProfile(
        permissions=frozenset(
            {"commercial-use", "modifications", "distribution", "private-use", "patent-use"}
        ),
        conditions=frozenset(
            {"include-copyright", "document-changes", "disclose-source", "same-license"}
        ),
        limitations=frozenset({"liability", "warranty"}),
    ),
    "LGPL-3.0": LicenseProfile(
        permissions=frozenset(
            {"commercial-use", "modifications", "distribution", "private-use", "patent-use"}
        ),
        conditions=frozenset(
            {"include-copyright", "document-changes", "disclose-source", "same-license--library"}
        ),
        limitations=frozenset({"liability", "warranty"}),
    ),
    "AGPL-3.0": LicenseProfile(
        permissions=frozenset(
            {"commercial-use", "modifications", "distribution", "private-use", "patent-use"}
        ),
        conditions=frozenset(
            {
                "include-copyright",
                "document-changes",
                "disclose-source",
                "network-use-disclose",
                "same-license",
            }
        ),
        limitations=frozenset({"liability", "warranty"}),
    ),
    "MPL-2.0": LicenseProfile(
        permissions=frozenset(
            {"commercial-use", "modifications", "distribution", "private-use", "patent-use"}
        ),
        conditions=frozenset({"disclose-source", "include-copyright", "same-license--file"}),
        limitations=frozenset({"liability", "trademark-use", "warranty"}),
    ),
    # Open-data licences (Open Data Commons).
    "ODbL-1.0": LicenseProfile(
        # Attribution + Share-Alike database licence.
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright", "disclose-source", "same-license"}),
        limitations=frozenset({"liability", "warranty"}),
    ),
    "ODC-By-1.0": LicenseProfile(
        # Attribution-only database licence.
        permissions=frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        conditions=frozenset({"include-copyright"}),
        limitations=frozenset({"liability", "warranty"}),
    ),
}


# --- pre-4.0 Creative Commons (1.0 / 2.0 / 2.5 / 3.0) ---------------------------------
# Hand-encoded from the licence texts, following the OGL-UK-3.0 precedent. These are
# GENERATED from six family shapes rather than written out 24 times, so the one claim
# that matters — that they differ from their 4.0 counterparts ONLY in the limitations —
# is structural and cannot drift entry by entry.
#
# Verified against the legal code (creativecommons.org/licenses/<id>/<ver>/legalcode,
# read 2026-07-27), spot-checking BY 1.0 §3/§4/§5/§6/§8, BY 2.0 §3/§4.2/§5/§6, BY 3.0
# §3/§4(b)/§5/§6/§8(f), and BY-NC-ND 3.0 for the NC and ND restrictions:
#
#   - Grants are the SAME as 4.0 per family: BY permits commercial-use, modifications,
#     distribution, private-use; NC removes commercial-use; ND removes modifications;
#     SA adds the same-license condition; attribution is required throughout.
#   - Warranty (BY 3.0 §5) and liability (§6) are disclaimed, as in 4.0.
#   - PATENTS ARE NOT ADDRESSED in any pre-4.0 version, and the only trademark clause
#     (e.g. BY 3.0 §8(f)) disclaims *Creative Commons'* own marks, NOT the licensor's.
#     4.0 added the explicit "Patent and trademark rights are not licensed" sentence.
#     So `patent-use` and `trademark-use` are OMITTED here — silence is not an explicit
#     exclusion, the same rule applied to OGL-UK-3.0 above.
#
# Because `check()` decides from `permissions` alone, these yield the same verdicts as
# their 4.0 counterparts; the limitation difference is reported, never fabricated.
#
# The 1.0/2.0/2.5/3.0 divergences that ARE real — attribution mechanics and the
# DRM-circumvention clause — fall outside this flag vocabulary entirely, so they cannot
# be represented here either way. That approximation is only valid for the CURRENT
# ``INTENTS`` vocabulary; ``tests/test_license_compat.py`` pins it so that adding an
# intent which touches those areas fails loudly instead of silently mis-answering.
#
# Jurisdiction note: ``normalize_spdx`` folds ported ids (e.g. CC-BY-3.0-US) onto the
# unported id, so these profiles also answer for ported variants. Porting could alter
# terms in a given jurisdiction, which is a reason the verdict stays an advisory.
_PRE_4_0_CC_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # family suffix -> (permissions, conditions)
    "CC-BY": (
        frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        frozenset({"include-copyright"}),
    ),
    "CC-BY-SA": (
        frozenset({"commercial-use", "modifications", "distribution", "private-use"}),
        frozenset({"include-copyright", "same-license"}),
    ),
    "CC-BY-NC": (
        frozenset({"modifications", "distribution", "private-use"}),
        frozenset({"include-copyright"}),
    ),
    "CC-BY-ND": (
        frozenset({"commercial-use", "distribution", "private-use"}),
        frozenset({"include-copyright"}),
    ),
    "CC-BY-NC-SA": (
        frozenset({"modifications", "distribution", "private-use"}),
        frozenset({"include-copyright", "same-license"}),
    ),
    "CC-BY-NC-ND": (
        frozenset({"distribution", "private-use"}),
        frozenset({"include-copyright"}),
    ),
}

# Warranty and liability are disclaimed in every pre-4.0 version; patent-use and
# trademark-use are deliberately absent (see above).
_PRE_4_0_CC_LIMITATIONS = frozenset({"liability", "warranty"})
_PRE_4_0_CC_VERSIONS = ("1.0", "2.0", "2.5", "3.0")


def _pre_4_0_cc_profiles() -> dict[str, LicenseProfile]:
    """Expand the six family shapes across the four pre-4.0 versions.

    A function rather than a module-level loop so the iteration variables do not
    leak into the module namespace.
    """
    return {
        f"{family}-{version}": LicenseProfile(
            permissions=permissions,
            conditions=conditions,
            limitations=_PRE_4_0_CC_LIMITATIONS,
        )
        for family, (permissions, conditions) in _PRE_4_0_CC_SHAPES.items()
        for version in _PRE_4_0_CC_VERSIONS
    }


LICENSE_MATRIX.update(_pre_4_0_cc_profiles())


# --- intended-use → required permission flags -----------------------------------------
# ``ml-training`` maps to commercial-use + modifications: training a model is a derivative
# use that is usually commercial, so ND/NC licences DENY. This is OUR stated interpretation,
# documented here, not a property of the licences themselves.
INTENTS: dict[str, tuple[str, ...]] = {
    "commercial": ("commercial-use",),
    "redistribute": ("distribution",),
    "modify": ("modifications",),
    "ml-training": ("commercial-use", "modifications"),
}

# Human-readable labels for permission flags, used to name the governing clause in a DENY.
_PERMISSION_LABELS: dict[str, str] = {
    "commercial-use": "NonCommercial",
    "modifications": "NoDerivatives",
    "distribution": "no-redistribution",
    "private-use": "no-private-use",
    "patent-use": "no-patent-grant",
}

# Copyleft conditions that turn an otherwise-ALLOW redistribute/ml-training into a REVIEW.
# Includes the MPL file-level and LGPL library-level same-license variants.
_COPYLEFT_CONDITIONS = (
    "same-license",
    "same-license--file",
    "same-license--library",
    "disclose-source",
)
_COPYLEFT_SENSITIVE_INTENTS = frozenset({"redistribute", "ml-training"})

# Canonical SPDX-id aliases for spaced/cased prose forms.
_PROSE_ALIASES: dict[str, str] = {
    "mit": "MIT",
    "mit license": "MIT",
    "the mit license": "MIT",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "bsd 2 clause": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd 3 clause": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "gpl 2.0": "GPL-2.0",
    "gpl-2.0": "GPL-2.0",
    "gplv2": "GPL-2.0",
    "gpl 3.0": "GPL-3.0",
    "gpl-3.0": "GPL-3.0",
    "gplv3": "GPL-3.0",
    "lgpl 3.0": "LGPL-3.0",
    "lgpl-3.0": "LGPL-3.0",
    "agpl 3.0": "AGPL-3.0",
    "agpl-3.0": "AGPL-3.0",
    "mpl 2.0": "MPL-2.0",
    "mpl-2.0": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "unlicense": "Unlicense",
    "the unlicense": "Unlicense",
    "cc0": "CC0-1.0",
    "cc0 1.0": "CC0-1.0",
    "cc-0": "CC0-1.0",
    "cc zero": "CC0-1.0",
    # NOTE: bare "public domain" is deliberately NOT aliased to CC0 — it is ambiguous
    # (CC0 vs PDM vs US-gov work vs informal prose) and mapping it would fabricate a
    # confident ALLOW from an unrecognized string. It falls through to None → REVIEW.
    "odbl": "ODbL-1.0",
    "odbl 1.0": "ODbL-1.0",
    "open database license": "ODbL-1.0",
    "odc-by": "ODC-By-1.0",
    "odc by 1.0": "ODC-By-1.0",
    "pddl": "PDDL-1.0",
    "pddl 1.0": "PDDL-1.0",
    # UK Open Government Licence (the bare "ogl-uk-N.0" id is already handled by the
    # matrix-key match; these are the prose / short-code forms). Bare "OGL" with no version
    # stays deliberately unmapped: the three versions now share one profile, so the VERDICT
    # would be the same, but reporting a specific spdx_id for an unversioned string would
    # invent a fact the source never stated — and spdx_id is the field callers cite.
    "ogl 1.0": "OGL-UK-1.0",
    "ogl v1.0": "OGL-UK-1.0",
    "ogl1": "OGL-UK-1.0",
    "open government licence 1.0": "OGL-UK-1.0",
    "open government license 1.0": "OGL-UK-1.0",
    "open government licence v1.0": "OGL-UK-1.0",
    "open government license v1.0": "OGL-UK-1.0",
    "ogl 2.0": "OGL-UK-2.0",
    "ogl v2.0": "OGL-UK-2.0",
    "ogl2": "OGL-UK-2.0",
    "open government licence 2.0": "OGL-UK-2.0",
    "open government license 2.0": "OGL-UK-2.0",
    "open government licence v2.0": "OGL-UK-2.0",
    "open government license v2.0": "OGL-UK-2.0",
    "ogl 3.0": "OGL-UK-3.0",
    "ogl v3.0": "OGL-UK-3.0",
    "ogl3": "OGL-UK-3.0",
    "open government licence 3.0": "OGL-UK-3.0",
    "open government license 3.0": "OGL-UK-3.0",
    "open government licence v3.0": "OGL-UK-3.0",
    "open government license v3.0": "OGL-UK-3.0",
}

# Creative Commons element ordering for canonical SPDX construction (BY, NC, ND/SA).
# Versions CC actually published: 1.0, 2.0, 2.5, 3.0, 4.0. Defined ONCE — the URL
# form and the prose form both derive from it, so they cannot drift apart (2.5 was
# previously accepted by neither, and the two sites disagreed on whether the capture
# included the ".0").
_CC_VERSION_ALT = r"(?:[1-4]\.0|2\.5)"
_CC_VERSION_RE = re.compile(rf"\b({_CC_VERSION_ALT})\b")
_CC_URL_RE = re.compile(rf"/licenses/([a-z-]+)/({_CC_VERSION_ALT})")

# The six element combinations Creative Commons actually issues. ND and SA are
# mutually exclusive (you cannot both forbid derivatives and dictate their licence),
# and every CC licence except CC0 carries BY. This is the licence FAMILY's own
# identity rule — it is deliberately independent of LICENSE_MATRIX, which says only
# which licences we hold compatibility flags for.
_CC_VALID_ELEMENTS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("by",),
        ("by", "nc"),
        ("by", "nd"),
        ("by", "sa"),
        ("by", "nc", "nd"),
        ("by", "nc", "sa"),
    }
)


def _canonical_spdx_for_cc(elements: list[str], version: str) -> str | None:
    """Build a canonical CC SPDX id from ordered element tokens (by, nc, nd, sa) and a
    version like '4.0'. Returns None only when the combination is not one Creative
    Commons issues — NOT when we merely lack compatibility flags for it.

    Identification and assessment are separate questions: ``check`` decides what a
    licence permits and answers REVIEW when no profile is bundled, while this
    function answers only *which licence is this*. Gating identity on
    ``LICENSE_MATRIX`` previously made ``CC-BY-SA-3.0`` — correctly constructed one
    line above — come back as "unrecognized".
    """
    order = ["by", "nc", "nd", "sa"]
    present = tuple(e for e in order if e in elements)
    if present not in _CC_VALID_ELEMENTS:
        return None
    return "CC-" + "-".join(p.upper() for p in present) + f"-{version}"


def _cc_elements_from_prose(collapsed: str) -> list[str]:
    """Pull CC element tokens (by, nc, nd, sa) out of already-collapsed lowercase prose.

    Defined once so the versioned path (``normalize_spdx``) and the versionless one
    (``identify_cc_family``) cannot disagree about what "cc by-nc" contains — the same
    single-definition reasoning as ``_CC_VERSION_ALT`` above.
    """
    tokens = re.split(r"[\s-]+", collapsed)
    elements: list[str] = []
    if "attribution" in collapsed or "by" in tokens:
        elements.append("by")
    if "noncommercial" in collapsed or "non-commercial" in collapsed or "nc" in tokens:
        elements.append("nc")
    if "noderivatives" in collapsed or "noderiv" in collapsed or "nd" in tokens:
        elements.append("nd")
    if "sharealike" in collapsed or "share-alike" in collapsed or "sa" in tokens:
        elements.append("sa")
    return elements


def _looks_like_cc_prose(collapsed: str) -> bool:
    return (
        collapsed.startswith("cc ")
        or collapsed.startswith("cc-")
        or "creative commons" in collapsed
    )


# A URL-ish token: optional scheme, a dotted host, optional path. Used to pull the
# HOST out of a licence string so domain checks cannot be satisfied by a substring
# sitting in someone else's path.
#
# Every repetition is BOUNDED. The unbounded form
# ``(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+`` nests a star inside a plus, which backtracks
# quadratically on input like "by-by-by-…": 12 KB of it cost ~1.5 s, and licence strings
# come from records that any user can upload on Zenodo/HuggingFace/OpenML. The bounds below
# are DNS's own limits (label <= 63 chars, and no real licence host is 8 labels deep), so
# they cost nothing real while making the worst case bounded.
_URLISH_RE = re.compile(
    r"(?:https?://)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,8}[a-z]{2,24}"
    r"(?:/[^\s,;)\]]*)?",
    re.I,
)

# Characters that, sitting immediately around a token, prove it is a PIECE of another URL
# rather than a URL of its own. See _token_is_a_real_host.
_NOT_A_HOST_BEFORE = frozenset("@#?&=")

# A licence identifier is short. Past this, we are looking at a licence TEXT, and scanning
# it for host tokens buys nothing — while an attacker-supplied field has no natural size
# limit at all. Belt and braces with the bounded regex above.
_MAX_SCAN_CHARS = 8192


def _token_is_a_real_host(text: str, start: int, end: int) -> bool:
    """Reject a token that URL syntax says is not a host.

    The scanner finds host-shaped substrings anywhere, which is what lets a bare
    ``creativecommons.org/licenses/by/4.0`` work. The cost is that it happily splits ONE
    URL into TWO tokens at a character it cannot consume, and each half then looks like a
    standalone URL:

    - ``http://creativecommons.org@evil.com/…`` — the token ends at ``@``, so the
      USERINFO is read as the host. The real host is ``evil.com``.
    - ``https://evil.com#creativecommons.org/…`` — the fragment is read as a bare host.

    Both previously produced ``ALLOW CC-BY-4.0`` for a URL pointing at an attacker's site.
    A token followed by ``@`` is userinfo; a token preceded by ``@#?&=`` sits inside
    another URL's userinfo, fragment or query. Neither is ever a host.

    The ``@`` may be separated from the token by a port, because the scanner stops before
    ``:`` — ``creativecommons.org:8080@evil.com`` is still userinfo, and checking only the
    single next character missed it.
    """
    cursor = end
    if cursor < len(text) and text[cursor] == ":":
        digits = cursor + 1
        while digits < len(text) and text[digits].isdigit():
            digits += 1
        if digits > cursor + 1:  # an actual :port, not a bare colon
            cursor = digits
    if cursor < len(text) and text[cursor] == "@":
        return False
    return not (start > 0 and text[start - 1] in _NOT_A_HOST_BEFORE)


def url_hosts(text: str) -> list[str]:
    """Lowercased hostnames of every URL-ish token in *text*.

    A licence field is free-form: it may be a bare URL, a bare host+path with no
    scheme (``creativecommons.org/publicdomain/zero/1.0``), or prose that merely
    mentions one. All three need to work, which is why this scans for tokens rather
    than parsing the whole string as a single URL — and why each token has to be checked
    against its neighbours before it counts as a host (``_token_is_a_real_host``).
    """
    hosts: list[str] = []
    for m in _URLISH_RE.finditer(text[:_MAX_SCAN_CHARS]):
        if not _token_is_a_real_host(text, m.start(), m.end()):
            continue
        token = m.group(0)
        candidate = token if "://" in token else "//" + token
        try:
            host = urlsplit(candidate).hostname
        except ValueError:  # malformed IPv6 literal, bad port, etc.
            continue
        if host:
            hosts.append(host.lower())
    return hosts


def host_matches(text: str, domain: str) -> bool:
    """True when *text* contains a URL whose HOST is *domain* or a subdomain of it.

    The substring test this replaces (``"creativecommons.org" in low``) also accepted
    ``http://evil.example.com/creativecommons.org/licenses/by/4.0/`` and reported it as
    ``CC-BY-4.0`` — an attacker-controlled or merely malformed upstream ``rightsUri``
    could mint a permissive verdict that then feeds the compatibility matrix, the
    access flag, and the FAIR score.
    """
    suffix = "." + domain
    return any(h == domain or h.endswith(suffix) for h in url_hosts(text))


def normalize_spdx(license_str: str | None) -> str | None:
    """Map a free/varied licence string to a canonical SPDX id, or None if unrecognized.

    Handles bare SPDX ids (``MIT``, ``CC-BY-4.0``), spaced/cased prose (``CC BY 4.0``,
    ``Apache License 2.0``) and CC/CC0 URLs
    (``https://creativecommons.org/licenses/by-nc/4.0/`` → ``CC-BY-NC-4.0``;
    ``creativecommons.org/publicdomain/zero/1.0`` → ``CC0-1.0``). Conservative — only maps
    what it can confidently recognize; ambiguous/unknown → None. Pure, deterministic."""
    if not license_str:
        return None
    raw = license_str.strip()
    if not raw:
        return None
    low = raw.lower()

    # DANDI (and other schema.org/DataCite-style feeds) publish the id scheme-qualified
    # as "spdx:CC-BY-4.0". Nothing below matched that, so a perfectly unambiguous SPDX
    # id was reported as an unknown licence. Strip the scheme, then fall through.
    if low.startswith("spdx:"):
        raw = raw[len("spdx:") :].strip()
        if not raw:
            return None
        low = raw.lower()

    # 1. Bare SPDX id already in the matrix (case-insensitive match on keys).
    for key in LICENSE_MATRIX:
        if low == key.lower():
            return key

    # 2. Creative Commons URLs.
    if host_matches(low, "creativecommons.org"):
        if "publicdomain/zero" in low:
            return "CC0-1.0"
        if "publicdomain/mark" in low:
            return None  # public-domain mark is not a licence we model
        m = _CC_URL_RE.search(low)
        if m:
            elements = [e for e in m.group(1).split("-") if e]
            return _canonical_spdx_for_cc(elements, m.group(2))
        return None

    # 3. Open Data Commons URLs.
    if host_matches(low, "opendatacommons.org"):
        if "/odbl" in low:
            return "ODbL-1.0"
        if "/by/" in low or low.endswith("/by"):
            return "ODC-By-1.0"
        if "/pddl" in low:
            return "PDDL-1.0"
        return None

    # 3b. UK Open Government Licence URLs (nationalarchives.gov.uk). The version lives in
    # the path (…/open-government-licence/version/3/); SPDX ids are OGL-UK-1.0/2.0/3.0.
    if host_matches(low, "nationalarchives.gov.uk") and "open-government-licence" in low:
        m = re.search(r"/version/([123])\b", low)
        return f"OGL-UK-{m.group(1)}.0" if m else None

    # 4. Prose / spaced forms via the alias table (normalize internal whitespace).
    collapsed = re.sub(r"\s+", " ", low).strip(" .")
    if collapsed in _PROSE_ALIASES:
        return _PROSE_ALIASES[collapsed]

    # 5. Spaced/cased CC prose, e.g. "CC BY 4.0", "CC BY-NC 4.0", "Creative Commons Attribution 4.0".
    if _looks_like_cc_prose(collapsed):
        ver = _CC_VERSION_RE.search(collapsed)
        if ver:
            spdx = _canonical_spdx_for_cc(_cc_elements_from_prose(collapsed), ver.group(1))
            if spdx:
                return spdx

    return None


def identify_cc_family(license_str: str | None) -> str | None:
    """Identify a Creative Commons licence stated *without* a version: ``"cc by-nc"`` →
    ``"CC-BY-NC"``. Returns None for anything versioned, non-CC, or not a combination CC
    issues. Pure, deterministic.

    Deliberately NOT part of ``normalize_spdx``: there is no SPDX id for a versionless CC
    licence, and returning an invented one would put a value into ``spdx_id`` that no
    registry recognizes and that a matrix lookup could silently mismatch. This is the
    third identification outcome the module needed and did not have — not "unknown", not
    "identified precisely", but "family known, version not" — and the distinction is
    load-bearing: CC 3.0 and 4.0 differ on attribution and on the effect of a DRM clause,
    so guessing a version is exactly the fabrication ``_canonical_spdx_for_cc`` refuses.

    Why it matters: EuropePMC states its licence without a version. Across 300 sampled OA
    records, 231 carried a versionless CC string and none carried a version, so the
    largest licence-bearing path in the product reported every one of them as "licence not
    stated / not recognized".
    """
    if not license_str:
        return None
    raw = license_str.strip()
    if raw.lower().startswith("spdx:"):
        raw = raw[len("spdx:") :].strip()
    low = raw.lower()
    if not low:
        return None
    # A stated version is normalize_spdx's job; never shadow a precise identification.
    if _CC_VERSION_RE.search(low):
        return None

    elements: list[str] = []
    if host_matches(low, "creativecommons.org"):
        # A versionless CC URL, e.g. creativecommons.org/licenses/by-nc/ — the licences
        # path segment carries the elements even when the version segment is absent.
        m = re.search(r"/licenses/([a-z-]+)", low)
        if not m:
            return None
        elements = [e for e in m.group(1).split("-") if e]
    elif _looks_like_cc_prose(low):
        elements = _cc_elements_from_prose(re.sub(r"\s+", " ", low).strip(" ."))
    else:
        return None

    order = ["by", "nc", "nd", "sa"]
    present = tuple(e for e in order if e in elements)
    if present not in _CC_VALID_ELEMENTS:
        return None
    return "CC-" + "-".join(p.upper() for p in present)


_STATED_EXCERPT_CAP = 60


def _stated_licence_excerpt(license_str: str | None) -> str | None:
    """Quote an unrecognized licence string back to the caller, safely.

    Returns None when the record genuinely stated nothing, so the caller can tell the two
    apart. A licence field is arbitrary upstream text — it can be a whole paragraph, or
    carry newlines that would wreck a one-line reason — so collapse whitespace and cap the
    length. Quoted rather than interpolated bare, so an empty-ish or punctuation-only value
    still reads as a value.
    """
    if not license_str:
        return None
    collapsed = " ".join(license_str.split())
    if not collapsed:
        return None  # whitespace-only is "stated nothing" in every sense that matters
    if len(collapsed) > _STATED_EXCERPT_CAP:
        collapsed = collapsed[: _STATED_EXCERPT_CAP - 1].rstrip() + "…"
    return f"'{collapsed}'"


def check(
    license_str: str | None,
    use: str,
    *,
    source_default: str | None = None,
    source_policy: str | None = None,
) -> LicenseVerdict:
    """Return a licence-compatibility verdict for ``use`` against ``license_str``. PURE:
    no I/O, deterministic.

    ``source_default`` is a licence the SOURCE publishes for its whole archive (see
    ``sources.SourceSpec.default_license``). It applies ONLY when the record itself states
    nothing, never as an override, and the verdict says so in its reason with
    ``license_raw`` left None.

    - unknown ``use`` (not in ``INTENTS``) → raises ``ValueError`` (caller error, fail loud).
    - licence absent → ``REVIEW`` (spdx_id None), reason "licence not stated".
    - stated but unrecognized → ``REVIEW`` (spdx_id None), reason quoting the stated value,
      because a caller can act on a string it can go read and cannot act on "not stated".
    - all required permissions present → ``ALLOW`` (downgraded to ``REVIEW`` when a copyleft
      ``same-license``/``disclose-source`` condition applies to a redistribute/ml-training intent).
    - any required permission missing → ``DENY``, reason naming the missing permission(s).
    """
    if use not in INTENTS:
        raise ValueError(f"unknown use intent {use!r}; supported: {', '.join(sorted(INTENTS))}")

    if not license_str and source_default:
        # The record states nothing, but the source dedicates its whole archive under a
        # published blanket licence. Assess that — reporting "all-rights-reserved" for data
        # its operator has placed in the public domain is a wrong answer, not a safe one.
        # license_raw stays None: the RECORD said nothing, and the verdict must not imply
        # otherwise. A licence the record DOES state always wins; this branch never runs then.
        verdict = check(source_default, use)
        return verdict.model_copy(
            update={
                "license_raw": None,
                "reason": (
                    f"{verdict.reason} — this record states no licence; assessed from the "
                    f"source's published blanket policy ({source_policy or 'unspecified'})"
                ),
            }
        )

    spdx = normalize_spdx(license_str)
    if spdx is None:
        family = identify_cc_family(license_str)
        if family is not None:
            # Stated, and identifiable down to the element set — just not to a version,
            # which is the only thing that would pick a matrix row. Saying "not stated"
            # here contradicted license_raw sitting in the same verdict.
            return LicenseVerdict(
                use=use,
                verdict="REVIEW",
                spdx_id=None,
                license_raw=license_str,
                reason=(
                    f"licence stated as {family} but with no version; the version "
                    f"determines the terms, so no compatibility profile can be selected "
                    f"— manual review required before this use"
                ),
                disclaimer=DISCLAIMER,
            )
        # "Not stated" and "stated, but we could not read it" are different facts, and the
        # caller acts on them differently: nothing to chase versus a string worth a look.
        # They shared one message until OpenML made the cost obvious — it states `Public`
        # on every dataset, and we answered "licence not stated", so the one lead a caller
        # had was hidden behind a sentence saying there was no lead. This is the same
        # correction already applied to the CC-family branch above, generalized.
        # The verdict does NOT move: `Public` is ambiguous ("publicly available" is not
        # "public domain"), so REVIEW remains right. Only the explanation gets accurate.
        if stated := _stated_licence_excerpt(license_str):
            return LicenseVerdict(
                use=use,
                verdict="REVIEW",
                spdx_id=None,
                license_raw=license_str,
                reason=(
                    f"licence stated as {stated} but not recognized; defaults to "
                    f"all-rights-reserved — manual review required before this use"
                ),
                disclaimer=DISCLAIMER,
            )
        return LicenseVerdict(
            use=use,
            verdict="REVIEW",
            spdx_id=None,
            license_raw=license_str,
            reason=(
                "licence not stated; defaults to all-rights-reserved — "
                "manual review required before this use"
            ),
            disclaimer=DISCLAIMER,
        )
    if spdx not in LICENSE_MATRIX:
        # Identified, but we hold no compatibility flags for it. Report the id —
        # "we know it is CC-BY-SA-3.0 but cannot assess it" is materially more
        # actionable than "unrecognized", and inventing flags for an unbundled
        # licence is exactly the fabrication this module refuses to do.
        return LicenseVerdict(
            use=use,
            verdict="REVIEW",
            spdx_id=spdx,
            license_raw=license_str,
            reason=(
                f"licence identified as {spdx}, but no compatibility profile is bundled "
                f"for it; manual review required before this use"
            ),
            disclaimer=DISCLAIMER,
        )

    profile = LICENSE_MATRIX[spdx]
    required = INTENTS[use]
    missing = [p for p in required if p not in profile.permissions]

    if missing:
        clauses = ", ".join(f"{p} not granted ({_PERMISSION_LABELS.get(p, p)})" for p in missing)
        return LicenseVerdict(
            use=use,
            verdict="DENY",
            spdx_id=spdx,
            license_raw=license_str,
            reason=f"{spdx} does not grant the permission(s) required for {use}: {clauses}",
            disclaimer=DISCLAIMER,
        )

    # NonCommercial honesty note: a licence that grants the requested permission but
    # withholds commercial-use still binds that use to non-commercial terms (e.g.
    # redistributing a CC-BY-NC dataset is allowed, but the redistribution must itself be
    # non-commercial). commercial/ml-training never reach here for NC licences (they DENY).
    nc_note = (
        " — note: NonCommercial licence, the use must itself remain non-commercial"
        if "commercial-use" not in profile.permissions
        else ""
    )

    # All required permissions present. Copyleft downgrade for redistribute/ml-training.
    if use in _COPYLEFT_SENSITIVE_INTENTS:
        copyleft = [c for c in _COPYLEFT_CONDITIONS if c in profile.conditions]
        if copyleft:
            return LicenseVerdict(
                use=use,
                verdict="REVIEW",
                spdx_id=spdx,
                license_raw=license_str,
                reason=(
                    f"{spdx} grants {use} but carries copyleft obligation(s) "
                    f"({', '.join(copyleft)}) you must honour — review before relying on it"
                    f"{nc_note}"
                ),
                disclaimer=DISCLAIMER,
            )

    return LicenseVerdict(
        use=use,
        verdict="ALLOW",
        spdx_id=spdx,
        license_raw=license_str,
        reason=f"{spdx} grants the permission(s) required for {use}: {', '.join(required)}{nc_note}",
        disclaimer=DISCLAIMER,
    )
