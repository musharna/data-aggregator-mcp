"""ENA filereport client — direct FASTQ file manifests for SRA accessions.

ENA mirrors most INSDC reads and serves them over HTTPS at ftp.sra.ebi.ac.uk.
``fastq_ftp``/``fastq_bytes``/``fastq_md5`` are ``;``-separated parallel lists;
paths are scheme-less, so we prepend ``https://``. Returns [] when an accession
is not (yet) mirrored.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

BASE_URL = "https://www.ebi.ac.uk/ena/portal/api"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
_FIELDS = (
    "run_accession,experiment_accession,study_accession,sample_accession,"
    "scientific_name,fastq_ftp,fastq_bytes,fastq_md5"
)


def _entries_from_record(rec: dict) -> list[FileEntry]:
    ftp = (rec.get("fastq_ftp") or "").strip()
    if not ftp:
        return []
    paths = ftp.split(";")
    sizes = (rec.get("fastq_bytes") or "").split(";")
    md5s = (rec.get("fastq_md5") or "").split(";")
    out: list[FileEntry] = []
    for i, path in enumerate(paths):
        if not path:
            continue
        size = sizes[i] if i < len(sizes) else ""
        md5 = md5s[i] if i < len(md5s) else ""
        out.append(
            FileEntry(
                name=path.rsplit("/", 1)[-1],
                size=int(size) if size.isdigit() else None,
                url=f"https://{path}",
                checksum=f"md5:{md5}" if md5 else None,
            )
        )
    return out


async def filereport(client: httpx.AsyncClient, accession: str) -> list[FileEntry]:
    """Return FASTQ FileEntries for an SRA accession (SRX/SRR/SRP/PRJ…). [] if unmirrored."""
    params = {"accession": accession, "result": "read_run", "fields": _FIELDS, "format": "json"}
    records = (
        await _http.request_json(
            client,
            "GET",
            f"{BASE_URL}/filereport",
            service="ENA filereport",
            params=params,
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        or []
    )
    files: list[FileEntry] = []
    for rec in records:
        files.extend(_entries_from_record(rec))
    return files
