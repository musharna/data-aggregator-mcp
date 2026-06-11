import asyncio
import os
import sys

import httpx

from data_aggregator_mcp import router

# Discover live: search omics for a topic, resolve the top hits, and relate them. We
# look for a GEO/SRA pair that shares a BioProject accession OR a record that links to
# another. The exact ids are NOT hard-coded — they are discovered at runtime so the
# check stays honest (same discipline as the recall-eval anchors).
#
# Live probe result (2026-06-11, sources=["omics"], query="RNA-seq"):
#   candidate ids: ['geo:GSE335000', 'sra:SRX33847073', 'bioproject:PRJNA1477125',
#                   'geo:GSE334629', 'sra:SRX33847072', 'bioproject:PRJNA1477097']
#   resolved: ['geo:GSE335000', 'sra:SRX33847073', 'geo:GSE334629', 'sra:SRX33847072']
#   errors: bioproject:PRJNA1477125 and bioproject:PRJNA1477097 → NotFoundError
#           (BioProject resolve not yet wired; non-fatal)
#   HINT shared_accession: ['sra:SRX33847073', 'sra:SRX33847072'] key='SRP708637'
#   HINT shared_accession: ['sra:SRX33847073', 'sra:SRX33847072'] key='PRJNA1477220'
#   Two SRA runs from the same BioProject share both SRP and PRJNA accessions → joinable.


async def main() -> int:
    if os.environ.get("DATA_AGGREGATOR_MCP_LIVE") != "1":
        print("SKIP: set DATA_AGGREGATOR_MCP_LIVE=1 to run the live check.")
        return 0
    async with httpx.AsyncClient(follow_redirects=True) as client:
        res = await router.search_page(client, query="RNA-seq", size=10, sources=["omics"])
        ids = [r.id for r in res.results][:6]
        print("candidate ids:", ids)
        if len(ids) < 2:
            print("FAIL: could not find >=2 live ids")
            return 1
        out = await router.relate(client, ids)
        print("resolved:", out.resolved)
        print("errors:", out.errors)
        for h in out.hints:
            print(f"HINT {h.kind}: {h.resources} key={h.key!r} :: {h.suggestion}")
        print("note:", out.note)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
