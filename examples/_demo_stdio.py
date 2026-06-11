"""Paced demo of the stdio MCP transport — used to record examples/assets/demo.svg.

Network-free: it only exercises `initialize`, `tools/list`, and a `list_sources`
tool call (which is served from a static table), so the recording is
deterministic and never flakes on an upstream API.

Re-record with:
    PATH=$HOME/.local/bin:$PATH \
      asciinema rec --overwrite -c "uv run python examples/_demo_stdio.py" /tmp/demo.cast
    svg-term --in /tmp/demo.cast --out examples/assets/demo.svg \
      --window --width 92 --height 34
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

C_DIM = "\033[2m"
C_RST = "\033[0m"
C_GRN = "\033[32m"
C_CYA = "\033[36m"
C_YEL = "\033[33m"
C_BOLD = "\033[1m"


def banner(line: str) -> None:
    print(f"{C_BOLD}{C_GRN}{line}{C_RST}", flush=True)


def prompt(line: str) -> None:
    print(f"{C_DIM}$ {C_RST}{line}", flush=True)


def send_arrow(label: str) -> None:
    print(f"{C_CYA}→{C_RST} {label}", flush=True)


def recv_arrow(label: str) -> None:
    print(f"{C_GRN}←{C_RST} {label}", flush=True)


async def main() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "data_aggregator_mcp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=8 * 1024 * 1024,
    )
    assert proc.stdin is not None and proc.stdout is not None

    async def rpc(req: dict) -> dict | None:
        proc.stdin.write((json.dumps(req) + "\n").encode())
        await proc.stdin.drain()
        if "id" not in req:
            return None
        line = await proc.stdout.readline()
        return json.loads(line)

    banner("🔎 data-aggregator-mcp — stdio MCP demo")
    print(flush=True)
    time.sleep(0.8)

    prompt("uvx data-aggregator-mcp  # boot stdio server")
    time.sleep(0.6)

    send_arrow("initialize")
    await rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "demo", "version": "0"},
            },
        }
    )
    from data_aggregator_mcp import __version__ as pkg_ver

    recv_arrow(f"serverInfo: data-aggregator-mcp v{pkg_ver}")
    await rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    time.sleep(0.7)
    print(flush=True)

    send_arrow("tools/list")
    resp = await rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = resp["result"]["tools"]
    recv_arrow(f"{len(tools)} tools registered:")
    time.sleep(0.4)
    blurb = {
        "search": "fan out across every source, deduped + taxa-normalized",
        "resolve": "full record: files, links, citation, access, full text",
        "fetch": "stream to disk, md5-verified, optional archive extract",
        "operate": "schema / preview / SQL over a remote table, no download",
        "relate": "how a set of records connect — shared id, link, lineage",
        "list_sources": "wired sources + capabilities",
    }
    by_name = {t["name"]: t for t in tools}
    for name in ["search", "resolve", "fetch", "operate", "relate", "list_sources"]:
        if name in by_name:
            print(
                f"  {C_YEL}•{C_RST} {C_BOLD}{name}{C_RST} {C_DIM}— {blurb[name]}{C_RST}", flush=True
            )
            time.sleep(0.22)
    time.sleep(0.7)
    print(flush=True)

    prompt("# tools/call list_sources")
    time.sleep(0.5)
    send_arrow('tools/call  {"name": "list_sources"}')
    resp = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_sources", "arguments": {}},
        }
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    sources = payload["sources"]
    recv_arrow(f"{len(sources)} sources:")
    time.sleep(0.3)
    for s in sources:
        fetch = s.get("fetchable")
        tag = (
            f"{C_GRN}fetch{C_RST}"
            if fetch is True
            else f"{C_YEL}{fetch}{C_RST}"
            if fetch
            else f"{C_DIM}discover{C_RST}"
        )
        print(
            f"  {C_CYA}{s['name']:<11}{C_RST} {C_DIM}{s['layer']:<10}{C_RST} {tag}  "
            f"{C_DIM}{s.get('id_example', '')}{C_RST}",
            flush=True,
        )
        time.sleep(0.2)
    time.sleep(1.2)
    print(flush=True)
    banner("done.")
    time.sleep(0.5)

    proc.stdin.close()
    await proc.wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
