"""A deterministic fake of every upstream the perf workloads reach.

It replaces ``httpx.AsyncHTTPTransport.handle_async_request``: below the server's
client, its event hooks and ``_http``'s retry loop, so every request the server code
issues is answered here and counted per host, and nothing in ``src/`` is edited. A
request no route below knows raises :class:`Unstubbed`, which fails the workload: a
silently empty answer would make the workload cheaper and the gate would read that as
a speed-up.

Every body is a pure function of the request URL, so the same request gets the same
answer in every run and on every Python version.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

_ZENODO_FIXTURE = Path(__file__).parents[2] / "tests" / "fixtures" / "zenodo_record_21887007.json"

# Records each distinct search query holds, so a cursor walk spans several pages.
POOL = 12
FETCH_RECORD = 900002
_CSV_ROWS = 400


class Unstubbed(AssertionError):
    """The server sent a request this fake has no answer for."""


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


def _window(params: dict[str, str], size_key: str, page_key: str) -> range:
    size = int(params.get(size_key, "10"))
    page = int(params.get(page_key, "1"))
    return range((page - 1) * size, min(page * size, POOL))


def csv_bytes(name: str) -> bytes:
    rows = [f"{name},{i},{(i * 7919) % 1000 / 10:.1f}" for i in range(_CSV_ROWS)]
    return ("file,row,value\n" + "\n".join(rows) + "\n").encode()


_FILES = ("heat_trials.csv", "symbiont_counts.csv")


class Upstream:
    def __init__(self) -> None:
        self.requests: Counter[str] = Counter()
        self.unstubbed: list[str] = []
        self._zenodo = json.loads(_ZENODO_FIXTURE.read_text())

    # --- Zenodo -------------------------------------------------------------------
    def _zenodo_record(self, rid: int, title: str, with_files: bool) -> dict[str, Any]:
        r = copy.deepcopy(self._zenodo)
        doi = f"10.5281/zenodo.{rid}"
        r.update(id=rid, recid=str(rid), doi=doi, doi_url=f"https://doi.org/{doi}", title=title)
        r["conceptrecid"] = str(rid - 1)
        r["conceptdoi"] = f"10.5281/zenodo.{rid - 1}"
        r["metadata"].update(title=title, doi=doi)
        r["links"] = {"self": f"https://zenodo.org/api/records/{rid}"}
        r["files"] = []
        if with_files:
            for name in _FILES:
                body = csv_bytes(name)
                r["files"].append(
                    {
                        "id": f"file-{rid}-{name}",
                        "key": name,
                        "size": len(body),
                        "checksum": "md5:" + hashlib.md5(body, usedforsecurity=False).hexdigest(),
                        "links": {
                            "self": f"https://zenodo.org/api/records/{rid}/files/{name}/content"
                        },
                    }
                )
        return r

    def zenodo(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, dict(request.url.params)
        if path == "/api/records":
            base = 100_000 + (_seed(params["q"]) % 8000) * 100
            hits = [
                self._zenodo_record(base + i, f"Coral heat stress survey {base + i}", False)
                for i in _window(params, "size", "page")
            ]
            return httpx.Response(200, json={"hits": {"total": POOL, "hits": hits}})
        if path.startswith("/api/records/") and path.endswith("/content"):
            return httpx.Response(200, content=csv_bytes(path.split("/")[-2]))
        if path.startswith("/api/records/"):
            rid = int(path.rsplit("/", 1)[1])
            title = f"Coral heat stress record {rid}"
            return httpx.Response(200, json=self._zenodo_record(rid, title, rid == FETCH_RECORD))
        raise Unstubbed(str(request.url))

    # --- DataCite -----------------------------------------------------------------
    @staticmethod
    def _datacite_item(doi: str, title: str, client: str) -> dict[str, Any]:
        return {
            "id": doi,
            "type": "dois",
            "attributes": {
                "doi": doi,
                "titles": [{"title": title}],
                "creators": [{"name": "Doe, J."}],
                "publicationYear": 2024,
                "descriptions": [{"description": "Thermal stress assay of reef corals."}],
                "subjects": [{"subject": "coral"}],
                "rightsList": [{"rights": "CC0 1.0", "rightsIdentifier": "cc0-1.0"}],
                "types": {"resourceTypeGeneral": "Dataset"},
                "url": f"https://example.org/{doi}",
                "publisher": "Dryad" if client.startswith("dryad") else "PANGAEA",
            },
            "relationships": {"client": {"data": {"id": client, "type": "clients"}}},
        }

    def datacite(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, dict(request.url.params)
        if path == "/dois":
            base = _seed(params["query"]) % 9000
            data = [
                self._datacite_item(
                    f"10.1594/PANGAEA.{base}{i:02d}",
                    f"Coral heat stress measurements {base}-{i}",
                    "pangaea.repository",
                )
                for i in _window(params, "page[size]", "page[number]")
            ]
            return httpx.Response(200, json={"data": data, "meta": {"total": POOL}})
        if path.startswith("/dois/"):
            doi = path.removeprefix("/dois/")
            client = "dryad.dryad" if "dryad" in doi else "pangaea.repository"
            item = self._datacite_item(doi, f"Coral heat stress deposit {doi}", client)
            return httpx.Response(200, json={"data": item})
        raise Unstubbed(str(request.url))

    # --- Dryad (files of a DataCite-resolved Dryad DOI) ---------------------------
    def dryad(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v2/datasets/"):
            return httpx.Response(
                200, json={"_links": {"stash:version": {"href": "/api/v2/versions/4242"}}}
            )
        if path == "/api/v2/versions/4242/files":
            files = [
                {
                    "path": name,
                    "size": len(csv_bytes(name)),
                    "digest": hashlib.sha256(csv_bytes(name)).hexdigest(),
                    "digestType": "sha-256",
                    "_links": {"self": {"href": f"/api/v2/files/{7000 + n}"}},
                }
                for n, name in enumerate(_FILES)
            ]
            return httpx.Response(
                200,
                json={"_embedded": {"stash:files": files}, "total": len(files), "_links": {}},
            )
        raise Unstubbed(str(request.url))

    # --- Hugging Face -------------------------------------------------------------
    @staticmethod
    def _hf_dataset(ds_id: str) -> dict[str, Any]:
        return {
            "id": ds_id,
            "author": ds_id.split("/")[0],
            "tags": ["coral", "license:cc-by-4.0"],
            "siblings": [{"rfilename": "data/train.csv"}, {"rfilename": "README.md"}],
            "createdAt": "2024-05-01T00:00:00.000Z",
            "lastModified": "2024-06-01T00:00:00.000Z",
            "downloads": 10,
            "likes": 1,
        }

    def huggingface(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, dict(request.url.params)
        if path == "/api/datasets":
            n = int(params.get("limit", "10"))
            base = _seed(params["search"]) % 1000
            return httpx.Response(
                200, json=[self._hf_dataset(f"reef{base}/coral-heat-{i}") for i in range(min(n, 6))]
            )
        if path.startswith("/api/datasets/"):
            return httpx.Response(200, json=self._hf_dataset(path.removeprefix("/api/datasets/")))
        raise Unstubbed(str(request.url))

    @staticmethod
    def hf_parquet(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/parquet":
            raise Unstubbed(str(request.url))
        ds = request.url.params["dataset"]
        branch = f"https://huggingface.co/datasets/{ds}/resolve/refs%2Fconvert%2Fparquet"
        files = [{"url": f"{branch}/default/train/0000.parquet", "size": 4096}]
        return httpx.Response(200, json={"parquet_files": files})

    # --- dispatch -----------------------------------------------------------------
    def answer(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.requests[host] += 1
        route = {
            "zenodo.org": self.zenodo,
            "api.datacite.org": self.datacite,
            "datadryad.org": self.dryad,
            "huggingface.co": self.huggingface,
            "datasets-server.huggingface.co": self.hf_parquet,
        }.get(host)
        try:
            if route is None:
                raise Unstubbed(str(request.url))
            return route(request)
        except Unstubbed:
            # Kept as well as raised: enrichment steps catch every exception, so a
            # raise alone can be swallowed and the workload would finish cheaper.
            self.unstubbed.append(str(request.url))
            raise

    def install(self) -> None:
        """Answer every request any httpx async client sends, from here."""
        upstream = self

        async def handle_async_request(
            self: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            return upstream.answer(request)

        httpx.AsyncHTTPTransport.handle_async_request = handle_async_request  # type: ignore[method-assign]
