"""Paced demo of real tool calls — used to record examples/assets/demo.svg.

Live: every call goes to the real upstream APIs (NCBI, EBI, OpenAIRE, DataCite,
Zenodo, OpenML, Hugging Face), so a re-recording shows whatever they answer that
day. It shows, in order: a search whose organism is expanded to its current name
(Orobanche aegyptiaca → Phelipanche aegyptiaca), the file manifest of one hit with
its checksums, a checksum-verified download (✓ only when the record declares a
checksum and fetch reports it verified), and SQL over a remote CSV without
downloading it.

Re-record with (needs the [operate] extra):
    PATH=$HOME/.local/bin:$PATH asciinema rec --overwrite --cols 92 --rows 30 \
      -c "uv run python examples/_demo_search.py" /tmp/demo.cast
    npx svg-term-cli --in /tmp/demo.cast --out examples/assets/demo.svg \
      --window --width 92 --height 30
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time

DIM, RST, GRN, CYA, YEL, BOLD = "\033[2m", "\033[0m", "\033[32m", "\033[36m", "\033[33m", "\033[1m"


def say(line: str = "", pause: float = 0.0) -> None:
    print(line, flush=True)
    time.sleep(pause)


def call(name: str, args: str, more: bool = False) -> None:
    say(f"{CYA}→{RST} {BOLD}{name}{RST}({args}{'' if more else ')'}", 0.3)


def short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


async def main() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "data_aggregator_mcp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=16 * 1024 * 1024,
    )
    assert proc.stdin is not None and proc.stdout is not None
    ids = iter(range(1, 100))

    async def rpc(method: str, params: dict | None = None, notify: bool = False) -> dict:
        req: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            req["id"] = next(ids)
        proc.stdin.write((json.dumps(req) + "\n").encode())
        await proc.stdin.drain()
        return {} if notify else json.loads(await proc.stdout.readline())

    async def tool(name: str, arguments: dict) -> dict:
        resp = await rpc("tools/call", {"name": name, "arguments": arguments})
        return json.loads(resp["result"]["content"][0]["text"])

    await rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "demo", "version": "0"},
        },
    )
    await rpc("notifications/initialized", notify=True)
    say(f"{BOLD}{GRN}data-aggregator-mcp{RST} {DIM}— live calls over stdio{RST}", 0.8)
    say()

    # 1. search, with the organism expanded to its current name
    call("search", '"transcriptome", organism="Orobanche aegyptiaca", size=5,', more=True)
    say(f'{" " * 9}sources=["omics", "literature", "datacite", "zenodo"])', 0.3)
    found = await tool(
        "search",
        {
            "query": "transcriptome",
            "organism": "Orobanche aegyptiaca",
            "sources": ["omics", "literature", "datacite", "zenodo"],
            "size": 5,
        },
    )
    tx = found["taxon_expansion"]
    say(
        f"  {DIM}organism →{RST} {YEL}{tx['canonical_name']}{RST} "
        f"{DIM}(NCBI taxid {tx['taxid']}; also matches {', '.join(tx['synonyms'])}){RST}",
        0.4,
    )
    for hit in found["results"]:
        prefix = hit["id"].split(":", 1)[0]
        kind = hit.get("kind") or ""
        say(f"  {CYA}{prefix:<10}{RST} {DIM}{kind:<14}{RST} {short(hit['title'] or '', 62)}", 0.18)
    say(f"  {DIM}{found['total']} matches upstream, top 5 shown; DOI duplicates removed{RST}", 0.3)
    for source, message in found.get("errors", {}).items():
        say(f"  {YEL}errors[{source}]{RST} {DIM}{short(message, 70)}{RST}", 0.3)
    time.sleep(0.7)
    say()

    # 2. resolve one hit: its files and their checksums
    run = next(h["id"] for h in found["results"] if h["source"] == "sra")
    call("resolve", f'"{run}"')
    record = await tool("resolve", {"id": run})
    for f in record["files"][:2]:
        say(
            f"  {f['name']:<24} {f['size'] / 1e9:4.1f} GB  {DIM}{f['checksum'][:20]}…{RST}",
            0.2,
        )
    say(f"  {DIM}linked: {', '.join(record['accessions'][1:3])}{RST}", 1.0)
    say()

    # 3. fetch a small dataset, verified against the checksum the source publishes.
    # fetch raises on a mismatch, and lists in `unverified` only files whose declared
    # checksum it could not compute, so a file is verified when its record declares a
    # checksum and it is not in `unverified`. The declared checksums come from resolve.
    call("fetch", '"openml:61", files="*.arff"')
    declared = {
        f["name"]: f["checksum"] for f in (await tool("resolve", {"id": "openml:61"}))["files"]
    }
    with tempfile.TemporaryDirectory() as dest:
        got = await tool("fetch", {"id": "openml:61", "dest": dest, "files": "*.arff"})
    for path in got["paths"]:
        name = path.rsplit("/", 1)[-1]
        checksum = declared.get(name)
        if checksum and name not in got["unverified"]:
            mark = f"{GRN}{checksum.split(':', 1)[0]} ✓{RST}"
        else:
            mark = f"{YEL}not verified{RST}"
        say(f"  {name}  {got['bytes']:,} bytes  {mark}", 1.0)
    say()

    # 4. SQL over a remote CSV, without downloading it
    select = "SELECT Species, count(*) n, round(avg(PetalLengthCm), 2) petal"
    rest = "FROM data GROUP BY 1 ORDER BY 1"
    sql = f"{select} {rest}"
    call("operate", '"sql", "hf:scikit-learn/iris", file="Iris.csv",', more=True)
    say(f'{" " * 10}query="{select}', 0.1)
    say(f'{" " * 17}{rest}")', 0.3)
    table = await tool(
        "operate",
        {
            "op": "sql",
            "id": "hf:scikit-learn/iris",
            "file": "Iris.csv",
            "query": sql,
        },
    )
    for row in table["rows"]:
        say(f"  {row['Species']:<16} n={row['n']}  petal={row['petal']} cm", 0.2)
    say(f"  {DIM}queried in place over HTTP; nothing saved to disk{RST}", 1.6)

    proc.stdin.close()
    await proc.wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
