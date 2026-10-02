"""operate must not follow a redirect into private address space, on any read path.

operate checked the record URL and then handed it to fsspec (schema/preview, and the size
probe) and DuckDB's httpfs (head/sql/peek). Both follow redirects by themselves, so a
record URL on a public host that answers 302 to 127.0.0.1 had the private body read and
returned as rows: the guard only ever saw the entry URL.

Real listeners on 127.0.0.1 stand in for the hosts. ``egress.assert_public_url`` is
wrapped to count the two "public" listeners as public; every other address, the private
listener included, goes through the real guard. The private listener records each
request, so "refused" is asserted as zero requests, not inferred from an error.
"""

from __future__ import annotations

import io
import re
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("fsspec")

import pyarrow as pa
import pyarrow.parquet as pq

from data_aggregator_mcp import duckquery, egress, operate, router
from data_aggregator_mcp.errors import ValidationError
from data_aggregator_mcp.models import DataResource, FileEntry
from tests._listeners import listener, redirect, serve

pytestmark = pytest.mark.asyncio


def _parquet(column: str, value: str) -> bytes:
    buf = io.BytesIO()
    pq.write_table(pa.table({column: [value]}), buf)
    return buf.getvalue()


# The private file has its own column name, so a leak shows even in a schema.
_LEGIT = {"csv": b"col\nlegit\n", "parquet": _parquet("col", "legit")}
_SECRET = {"csv": b"secret\nhunter2\n", "parquet": _parquet("secret", "hunter2")}


def _refusal(url: str) -> str:
    return (
        rf"^\[ValidationError\] request: '127\.0\.0\.1' resolves to the non-public address "
        rf"127\.0\.0\.1; refusing to fetch {re.escape(url)}\. "
    )


def _legit(op: str, out: dict) -> bool:
    if op == "schema":
        return [c["name"] for c in out["columns"]] == ["col"]
    if op == "peek":
        return out["row_count"] == 1 and [c["column_name"] for c in out["columns"]] == ["col"]
    return out["rows"] == [{"col": "legit"}]


@pytest.fixture
def hosts(monkeypatch):
    """Two "public" listeners and a private one, with the guard ON for everything else."""
    monkeypatch.delenv(egress.ALLOW_PRIVATE_ENV, raising=False)
    egress._clear_cache()
    with listener() as public, listener() as public2, listener() as private:
        for fmt in ("csv", "parquet"):
            secret = private.url(f"/secret.{fmt}")
            private.routes[f"/secret.{fmt}"] = serve(_SECRET[fmt])
            for host in (public, public2):
                host.routes[f"/data.{fmt}"] = serve(_LEGIT[fmt])
                host.routes[f"/to-private.{fmt}"] = redirect(secret)
            public.routes[f"/chain.{fmt}"] = redirect(public2.url(f"/to-private.{fmt}"))
            public.routes[f"/get-to-private.{fmt}"] = redirect(secret, head=serve(_LEGIT[fmt]))
            public.routes[f"/to-public.{fmt}"] = redirect(public2.url(f"/data.{fmt}"))

        real = egress.assert_public_url
        allowed = (public.url("/"), public2.url("/"))

        async def treat_listeners_as_public(url: str, *, what: str) -> None:
            if url.startswith(allowed):
                return
            await real(url, what=what)

        monkeypatch.setattr(egress, "assert_public_url", treat_listeners_as_public)
        yield SimpleNamespace(public=public, public2=public2, private=private)


async def _operate(monkeypatch, url: str, fmt: str, op: str) -> dict | Exception:
    record = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        files=[FileEntry(name=f"data.{fmt}", url=url)],
    )

    async def resolve(client, rid):
        return record

    monkeypatch.setattr(router, "resolve", resolve)
    query = "SELECT * FROM data" if op == "sql" else None
    # A plain client on purpose: the guard must not depend on the caller hooking it.
    async with httpx.AsyncClient() as client:
        try:
            return await operate.run(client, "zenodo:1", op, query=query, n=5)
        except Exception as exc:  # recorded; the test asserts its exact type and message
            return exc


@pytest.mark.parametrize("fmt", ["csv", "parquet"])
@pytest.mark.parametrize("op", ["schema", "preview", "head", "sql", "peek"])
async def test_operate_refuses_a_redirect_into_private_space(hosts, monkeypatch, op, fmt):
    """schema/preview were read by fsspec and head/sql/peek by DuckDB's httpfs (after an
    fsspec size probe); each followed the 302 itself."""
    secret = hosts.private.url(f"/secret.{fmt}")
    attacks = {
        "public -> private": hosts.public.url(f"/to-private.{fmt}"),
        "public -> public -> private": hosts.public.url(f"/chain.{fmt}"),
        "HEAD answered, GET redirected": hosts.public.url(f"/get-to-private.{fmt}"),
    }
    reached, answers = [], {}
    for route, entry in attacks.items():
        hosts.private.hits.clear()
        answers[route] = await _operate(monkeypatch, entry, fmt, op)
        if hosts.private.hits:
            reached.append(route)
    assert reached == [], f"the private listener was reached via: {reached}"
    for route, got in answers.items():
        assert isinstance(got, ValidationError), f"{route}: returned {got!r}"
        assert re.match(_refusal(secret), str(got)), f"{route}: {got}"
    # Positive control, same op: a redirect to an allowed host is still followed.
    out = await _operate(monkeypatch, hosts.public.url(f"/to-public.{fmt}"), fmt, op)
    assert not isinstance(out, ValidationError), out
    assert _legit(op, out), out
    assert any(path == f"/data.{fmt}" for _, path, _ in hosts.public2.hits)
    assert hosts.private.hits == []


@pytest.mark.parametrize("op", ["head", "sql", "peek"])
async def test_the_engine_reads_the_guarded_copy_not_the_url(hosts, monkeypatch, op):
    """A host can answer the first requests honestly and redirect every later one. The URL
    is requested twice, by the size probe and the guarded download; httpfs requested it
    again itself and followed the later 302 unchecked."""
    seen = []
    honest = serve(_LEGIT["parquet"])
    then_private = redirect(hosts.private.url("/secret.parquet"))

    def honest_twice(h):
        seen.append(h.command)
        return (honest if len(seen) <= 2 else then_private)(h)

    hosts.public.routes["/twice.parquet"] = honest_twice
    out = await _operate(monkeypatch, hosts.public.url("/twice.parquet"), "parquet", op)
    assert hosts.private.hits == [], f"a later read of the URL followed the redirect: {seen}"
    assert not isinstance(out, Exception), out
    assert _legit(op, out), out  # positive control: the honest answers are the data
    assert seen == ["HEAD", "GET"]


async def test_duckdb_is_never_handed_a_remote_filesystem_url(hosts):
    """The engine reads a local copy, so the schemes httpfs would open itself (S3-family,
    hf://) are refused before DuckDB sees them. The endpoint is steered at the private
    listener, so on the old code the egress shows up as a request there."""
    target = (
        f"s3://bucket/secret.csv?s3_endpoint=127.0.0.1:{hosts.private.port}"
        "&s3_url_style=path&s3_use_ssl=false"
    )
    try:
        got: object = await duckquery.run_sql(target, "secret.csv", "SELECT * FROM data")
    except Exception as exc:  # recorded, then asserted by exact type and message below
        got = exc
    assert hosts.private.hits == [], f"DuckDB reached the private listener: {got!r}"
    assert isinstance(got, ValidationError), repr(got)
    assert re.match(
        r"^\[ValidationError\] operate source: scheme 's3' cannot be read \(only "
        rf"http/https or a local file\): {re.escape(target)}$",
        str(got),
    ), got
    # Positive control: an allowed remote source is read, through the guarded download.
    ok = await duckquery.run_sql(hosts.public.url("/data.csv"), "data.csv", "SELECT * FROM data")
    assert ok["rows"] == [{"col": "legit"}]
