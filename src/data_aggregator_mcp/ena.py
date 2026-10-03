"""ENA filereport client — direct FASTQ file manifests for SRA accessions.

ENA mirrors most INSDC reads and serves them over HTTPS at ftp.sra.ebi.ac.uk.
``fastq_ftp``/``fastq_bytes``/``fastq_md5`` are ``;``-separated parallel lists;
paths are scheme-less, so we prepend ``https://``. Returns [] when ENA has no run
for the accession (not mirrored, or unknown: both are ``200 []``); a run without
FASTQ lists nothing (0.6% of runs carry only the submitted files, e.g. a BAM).

All 25,514 runs of a live sample (2026-10-02) gave the three lists at equal length,
every path as ``ftp.sra.ebi.ac.uk/vol1/fastq/…``, every size as digits and every md5
as 32 lowercase hex digits. An answer that breaks this is malformed: a ragged list
cannot be paired file-to-checksum without guessing, and a path off the host must
not become a download URL. An empty slot in equal-length lists is read as absent.
"""

from __future__ import annotations

import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

BASE_URL = "https://www.ebi.ac.uk/ena/portal/api"
_FIELDS = (
    "run_accession,experiment_accession,study_accession,sample_accession,"
    "scientific_name,fastq_ftp,fastq_bytes,fastq_md5"
)
# The host, then segments of safe characters that do not start with a dot: no
# scheme, userinfo, port, query, fragment, escape, or ``.``/``..``/empty segment.
_PATH = re.compile(r"(?:ftp\.sra\.ebi\.ac\.uk(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)+)?")
_SIZE = re.compile(r"[0-9]*")
_MD5 = re.compile(r"(?:[0-9a-f]{32})?")
_COLUMNS = (("fastq_ftp", _PATH), ("fastq_bytes", _SIZE), ("fastq_md5", _MD5))


def _columns(rec: object) -> tuple[list[str], list[str], list[str]]:
    """A run's FASTQ paths, byte counts and md5s, index-aligned (an empty field is one
    empty slot). Raises ``UpstreamEnvelopeError`` naming the run and the field when the
    record breaks the contract above."""
    if not isinstance(rec, dict):
        raise _http.UpstreamEnvelopeError(f"no ENA filereport run in {rec!r:.200}")
    run = rec.get("run_accession")
    cols = []
    for field, pattern in _COLUMNS:
        value = rec.get(field)
        if not (isinstance(value, str) and all(map(pattern.fullmatch, value.split(";")))):
            raise _http.UpstreamEnvelopeError(
                f"ENA filereport run {run!r:.40} has a malformed {field}: {value!r:.200}"
            )
        cols.append(value.split(";"))
    paths, sizes, md5s = cols
    if not len(paths) == len(sizes) == len(md5s):
        raise _http.UpstreamEnvelopeError(
            f"ENA filereport run {run!r:.40} lists {len(paths)} files, "
            f"{len(sizes)} sizes and {len(md5s)} md5s"
        )
    return paths, sizes, md5s


def _check_report(body: list) -> None:
    for rec in body:
        _columns(rec)


def _entries_from_record(rec: dict) -> list[FileEntry]:
    paths, sizes, md5s = _columns(rec)
    return [
        FileEntry(
            name=path.rpartition("/")[2],
            size=int(sizes[i]) if sizes[i] else None,
            url=f"https://{path}",
            checksum=f"md5:{md5s[i]}" if md5s[i] else None,
        )
        for i, path in enumerate(paths)
        if path
    ]


async def filereport(client: httpx.AsyncClient, accession: str) -> list[FileEntry]:
    """Return FASTQ FileEntries for an SRA accession (SRX/SRR/SRP/PRJ…). [] when ENA
    has no run for it or its runs have no FASTQ; a malformed answer is retried, then
    raised as ``UpstreamUnavailableError``."""
    params = {"accession": accession, "result": "read_run", "fields": _FIELDS, "format": "json"}
    # httpx upper-cases the method, so "get" sends the same request; the method is
    # pinned by test_ena_observed.py::test_filereport_sends_exactly_the_documented_get.
    method = "GET"  # pragma: no mutate
    records = await _http.request_json(
        client,
        method,
        f"{BASE_URL}/filereport",
        service="ENA filereport",
        params=params,
        expect=list,
        check=_check_report,
    )
    files: list[FileEntry] = []
    for rec in records:
        files.extend(_entries_from_record(rec))
    return files
