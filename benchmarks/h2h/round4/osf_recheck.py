"""Re-read the OSF projects a round-4 key names, now that OSF is back (2026-10-18).

    python3 benchmarks/h2h/round4/osf_recheck.py benchmarks/h2h/round4/keys/U5.yaml \
        > benchmarks/h2h/round4/osf_recheck_U5_2026-10-18.json

The keys were built while OSF was down for maintenance, so their OSF studies rest on
DataCite metadata or a paper's data statement. For every OSF GUID in a key (studies and
near misses) this reads the OSF API v2 directly: the node (public or not), its
components, and the full file tree of every storage provider of the node and each
component. It records what is there; the judgement of each study is made from this
listing and written into the key by hand. Standard library only (yaml via a regex:
an OSF id is ``osf.io/<5 chars>`` or ``10.17605/osf.io/<5 chars>``).
"""

from __future__ import annotations

import json
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.osf.io/v2"
GUID = re.compile(r"osf\.io/([a-z0-9]{5})(?![a-z0-9])", re.IGNORECASE)
MAX_FILES = 400


def get(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "dam-h2h-recheck/1"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310 - fixed https host
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            return e.code, {}
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5 * (attempt + 1))
    return -1, {}


def pages(url: str):
    while url:
        status, d = get(url)
        if status != 200:
            yield {"_status": status}
            return
        yield from d.get("data", [])
        url = (d.get("links") or {}).get("next")
        time.sleep(0.5)


def tree(node: str) -> list[dict]:
    files: list[dict] = []
    for prov in pages(f"{API}/nodes/{node}/files/"):
        if "_status" in prov:
            files.append({"error": prov["_status"]})
            continue
        # An add-on provider (GitHub, Dropbox...) may carry no name, or no file link.
        name = prov["attributes"].get("name") or prov["attributes"].get("provider")
        href = ((prov.get("relationships") or {}).get("files") or {}).get("links", {})
        href = (href.get("related") or {}).get("href")
        if not href:
            files.append({"path": f"{name}:", "error": "no file listing"})
            continue
        stack = [(f"{name}:", href)]
        while stack and len(files) < MAX_FILES:
            prefix, url = stack.pop()
            for f in pages(url):
                if "_status" in f:
                    files.append({"path": prefix, "error": f["_status"]})
                    continue
                a = f["attributes"]
                path = prefix + "/" + a["name"]
                if a["kind"] == "folder":
                    stack.append((path, f["relationships"]["files"]["links"]["related"]["href"]))
                else:
                    files.append({"path": path, "size": a.get("size")})
    return files


def node(guid: str, depth: int = 0) -> dict:
    status, d = get(f"{API}/nodes/{guid}/")
    if status != 200:
        # Not a node: a registration, a preprint or a file has its own endpoint.
        g_status, g = get(f"{API}/guids/{guid}/")
        kind = (g.get("data") or {}).get("type") if g_status == 200 else None
        return {"guid": guid, "status": status, "guid_status": g_status, "guid_type": kind}
    a = d["data"]["attributes"]
    out = {
        "guid": guid,
        "status": status,
        "title": a.get("title"),
        "public": a.get("public"),
        "category": a.get("category"),
        "date_created": a.get("date_created"),
        "description": (a.get("description") or "")[:600],
        "files": tree(guid),
    }
    if depth < 2:
        out["components"] = [
            node(c["id"], depth + 1) for c in pages(f"{API}/nodes/{guid}/children/") if "id" in c
        ]
    return out


def main() -> None:
    signal.signal(signal.SIGALRM, lambda *_: (sys.stderr.write("walltime guard\n"), sys.exit(2)))
    signal.alarm(3600)
    text = Path(sys.argv[1]).read_text()
    guids = list(dict.fromkeys(g.lower() for g in GUID.findall(text)))
    report = {"checked": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "api": API, "nodes": []}
    for g in guids:
        sys.stderr.write(f"{g}\n")
        report["nodes"].append(node(g))
    json.dump(report, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
