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


# A URL-ish token: optional scheme, a dotted host, optional path. Used to pull the
# HOST out of a licence string so domain checks cannot be satisfied by a substring
# sitting in someone else's path.
_URLISH_RE = re.compile(
    r"(?:https?://)?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?:/[^\s,;)\]]*)?", re.I
)


def url_hosts(text: str) -> list[str]:
    """Lowercased hostnames of every URL-ish token in *text*.

    A licence field is free-form: it may be a bare URL, a bare host+path with no
    scheme (``creativecommons.org/publicdomain/zero/1.0``), or prose that merely
    mentions one. All three need to work, which is why this scans for tokens rather
    than parsing the whole string as a single URL.
    """
    hosts: list[str] = []
    for token in _URLISH_RE.findall(text):
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

    # 4. Prose / spaced forms via the alias table (normalize internal whitespace).
    collapsed = re.sub(r"\s+", " ", low).strip(" .")
    if collapsed in _PROSE_ALIASES:
        return _PROSE_ALIASES[collapsed]

    # 5. Spaced/cased CC prose, e.g. "CC BY 4.0", "CC BY-NC 4.0", "Creative Commons Attribution 4.0".
    if (
        collapsed.startswith("cc ")
        or collapsed.startswith("cc-")
        or "creative commons" in collapsed
    ):
        ver = _CC_VERSION_RE.search(collapsed)
        if ver:
            version = ver.group(1)
            tokens = re.split(r"[\s-]+", collapsed)
            cc_elements: list[str] = []
            phrase = collapsed
            if "attribution" in phrase or "by" in tokens:
                cc_elements.append("by")
            if "noncommercial" in phrase or "non-commercial" in phrase or "nc" in tokens:
                cc_elements.append("nc")
            if "noderivatives" in phrase or "noderiv" in phrase or "nd" in tokens:
                cc_elements.append("nd")
            if "sharealike" in phrase or "share-alike" in phrase or "sa" in tokens:
                cc_elements.append("sa")
            spdx = _canonical_spdx_for_cc(cc_elements, version)
            if spdx:
                return spdx

    return None


def check(license_str: str | None, use: str) -> LicenseVerdict:
    """Return a licence-compatibility verdict for ``use`` against ``license_str``. PURE:
    no I/O, deterministic.

    - unknown ``use`` (not in ``INTENTS``) → raises ``ValueError`` (caller error, fail loud).
    - licence absent / unrecognized / not in the matrix → ``REVIEW`` (spdx_id None),
      reason naming "licence not stated / not recognized; defaults to all-rights-reserved".
    - all required permissions present → ``ALLOW`` (downgraded to ``REVIEW`` when a copyleft
      ``same-license``/``disclose-source`` condition applies to a redistribute/ml-training intent).
    - any required permission missing → ``DENY``, reason naming the missing permission(s).
    """
    if use not in INTENTS:
        raise ValueError(f"unknown use intent {use!r}; supported: {', '.join(sorted(INTENTS))}")

    spdx = normalize_spdx(license_str)
    if spdx is None:
        return LicenseVerdict(
            use=use,
            verdict="REVIEW",
            spdx_id=None,
            license_raw=license_str,
            reason=(
                "licence not stated / not recognized; defaults to all-rights-reserved — "
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
