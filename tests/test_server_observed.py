"""Behaviour of ``server`` that the nightly mutation run (#88) showed no test observed.

Each test pins what a caller sees: the exact flags the CLI hands the HTTP transport, the
defaults each tool passes on, which client object reaches every upstream call, and the
exact text of every refusal, log line and help entry.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sys

import pytest
from mcp import types

from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp import fetch_gate, http_transport, server, tool_specs, zenodo
from data_aggregator_mcp import license_compat as license_mod
from data_aggregator_mcp import resources as resources_mod
from data_aggregator_mcp.errors import ValidationError
from data_aggregator_mcp.models import (
    DataResource,
    FetchResult,
    FileEntry,
    RelateResult,
    SearchResult,
    TrustSignals,
)


def _words(text: str) -> str:
    """Help output with argparse's line wrapping folded away."""
    return " ".join(text.split())


# --- the serve CLI ------------------------------------------------------------------


@pytest.fixture
def served(monkeypatch):
    """Record what ``main`` hands the HTTP transport instead of binding a port."""
    calls: list[dict] = []

    def fake_serve_http(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(http_transport, "serve_http", fake_serve_http)
    return calls


def test_every_http_flag_reaches_the_transport(served, capsys) -> None:
    server.main(
        [
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--port",
            "9001",
            "--allow-host",
            "a.example:9001",
            "--allow-host",
            "b.example:9001",
            "--allow-origin",
            "https://a.example",
            "--allow-origin",
            "https://b.example",
            "--stateless",
            "--json-response",
        ]
    )
    assert served == [
        {
            "host": "0.0.0.0",
            "port": 9001,
            "allow_hosts": ["a.example:9001", "b.example:9001"],
            "allow_origins": ["https://a.example", "https://b.example"],
            "stateless": True,
            "json_response": True,
        }
    ]
    assert capsys.readouterr().err == (
        "data-aggregator-mcp: serving over HTTP — note that fetch() writes to this "
        "server's filesystem, not the client's.\n"
    )


def test_http_flags_default_to_this_machine_only(served) -> None:
    server.main(["--transport", "http"])
    assert served == [
        {
            "host": http_transport.DEFAULT_HOST,
            "port": http_transport.DEFAULT_PORT,
            "allow_hosts": None,
            "allow_origins": None,
            "stateless": False,
            "json_response": False,
        }
    ]
    assert isinstance(served[0]["port"], int)


def test_transport_accepts_stdio_and_http_only(served, monkeypatch, capsys) -> None:
    started: list[str] = []

    async def fake_serve() -> None:
        started.append("stdio")

    monkeypatch.setattr(server, "_serve", fake_serve)
    server.main(["--transport", "stdio"])
    server.main([])
    assert started == ["stdio", "stdio"]
    assert served == []

    with pytest.raises(SystemExit) as exc:
        server.main(["--transport", "ftp"])
    assert exc.value.code == 2
    # argparse quotes the choices on some Python versions and not others.
    assert re.search(
        r"argument --transport: invalid choice: 'ftp' \(choose from '?stdio'?, '?http'?\)$",
        _words(capsys.readouterr().err),
    )


def test_a_refused_http_setting_exits_2_with_its_message(monkeypatch, capsys) -> None:
    refusal = ValidationError("--host 0.0.0.0 needs --allow-host")

    def refuse(**kwargs):
        raise refusal

    monkeypatch.setattr(http_transport, "serve_http", refuse)
    with pytest.raises(SystemExit) as exc:
        server.main(["--transport", "http", "--host", "0.0.0.0"])
    assert exc.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == f"data-aggregator-mcp: {refusal}"


def test_serve_help_documents_every_flag(capsys, monkeypatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exc:
        server.main(["--help"])
    assert exc.value.code == 0
    out = _words(capsys.readouterr().out)
    assert out.startswith("usage: data-aggregator-mcp [-h] [--transport {stdio,http}]")
    for entry in (
        "--transport {stdio,http} stdio (default) or streamable HTTP",
        "--host HOST http only; bind address (default 127.0.0.1 = this machine only). "
        "A non-loopback value requires --allow-host.",
        "--port PORT http only",
        "--allow-host HOST:PORT http only; permitted Host header, repeatable. "
        "Required off loopback.",
        "--allow-origin ORIGIN http only; permitted browser Origin header, repeatable.",
        "--stateless http only; fresh transport per request, no session affinity",
        "--json-response http only; plain JSON responses instead of SSE streams",
    ):
        assert entry in out, entry


# --- the search CLI -----------------------------------------------------------------


def _page(results: list, errors: dict) -> dict:
    return SearchResult(
        query="q", total=len(results), count=len(results), results=results, errors=errors
    ).model_dump()


@pytest.fixture
def searched(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_dispatch(name, args):
        calls.append((name, args))
        return _page([], {})

    monkeypatch.setattr(server, "_dispatch", fake_dispatch)
    return calls


def test_search_cli_defaults_to_every_source_at_the_default_size(searched, capsys) -> None:
    server.main(["search", "rice"])
    assert searched == [("search", {"query": "rice", "size": zenodo.DEFAULT_SIZE, "sources": None})]
    assert capsys.readouterr().out == "[]\n"


def test_search_cli_reads_sys_argv_when_given_no_argv(searched, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["data-aggregator-mcp", "search", "--size", "4", "rice"])
    server.main()
    assert searched == [("search", {"query": "rice", "size": 4, "sources": None})]


def test_search_cli_drops_blank_source_names(searched) -> None:
    server.main(["search", "--sources", " zenodo , ,datacite ", "rice"])
    assert searched[0][1]["sources"] == ["zenodo", "datacite"]


def test_search_cli_reports_a_failed_dispatch_and_exits_1(monkeypatch, capsys) -> None:
    async def boom(name, args):
        raise RuntimeError("router exploded")

    monkeypatch.setattr(server, "_dispatch", boom)
    with pytest.raises(SystemExit) as exc:
        server.main(["search", "rice"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "data-aggregator-mcp search: router exploded\n"


def test_search_cli_names_each_failed_source_then_the_total_failure(monkeypatch, capsys) -> None:
    async def all_failed(name, args):
        return _page([], {"zenodo": "down", "datacite": "slow"})

    monkeypatch.setattr(server, "_dispatch", all_failed)
    with pytest.raises(SystemExit) as exc:
        server.main(["search", "rice"])
    assert exc.value.code == 1
    assert capsys.readouterr().err.splitlines() == [
        "data-aggregator-mcp search: zenodo: down",
        "data-aggregator-mcp search: datacite: slow",
        "data-aggregator-mcp search: every source failed",
    ]


def test_search_help_documents_every_flag(capsys, monkeypatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exc:
        server.main(["search", "--help"])
    assert exc.value.code == 0
    out = _words(capsys.readouterr().out)
    assert out.startswith("usage: data-aggregator-mcp search [-h] [--json] [--size SIZE]")
    assert "--json emit JSON (the only format)" in out
    assert "--sources SOURCES comma-separated sources; default: all" in out


# --- _serve -------------------------------------------------------------------------


async def test_serve_runs_the_server_on_the_stdio_streams_inside_a_session(monkeypatch) -> None:
    ran: list[tuple] = []

    @contextlib.asynccontextmanager
    async def fake_stdio():
        yield ("read-stream", "write-stream")

    async def fake_run(read, write, options):
        ran.append((read, write, options, server._SHARED_CLIENT is not None))

    monkeypatch.setattr(server, "stdio_server", fake_stdio)
    monkeypatch.setattr(server.server, "run", fake_run)
    monkeypatch.setattr(server.server, "create_initialization_options", lambda: "init-options")
    await server._serve()
    assert ran == [("read-stream", "write-stream", "init-options", True)]
    assert server._SHARED_CLIENT is None


# --- _dispatch: which client and which arguments reach each handler -----------------


class _Ctx:
    def __init__(self, *, session=None, request_id=None, meta=None) -> None:
        self.session = session
        self.request_id = request_id
        self.meta = meta


class _Session:
    def __init__(self, fail: Exception | None = None) -> None:
        self.progress: list[tuple] = []
        self.fail = fail

    async def send_progress_notification(self, token, progress, total=None, **kw):
        if self.fail is not None:
            raise self.fail
        self.progress.append((token, progress, total))


def _record(
    id_: str = "zenodo:1", source: str = "zenodo", license_: str | None = None
) -> DataResource:
    return DataResource(
        id=id_,
        source=source,
        kind="dataset",
        title="t",
        license=license_,
        files=[FileEntry(name="a.txt", url="https://x/a.txt", size=1)],
    )


async def test_list_sources_health_check_uses_the_session_client(monkeypatch) -> None:
    seen: list = []

    async def fake_probe(client):
        seen.append(client)
        return [{"name": "zenodo", "status": "up", "latency_ms": 1, "detail": None}]

    monkeypatch.setattr(server.health_mod, "probe_sources", fake_probe)
    monkeypatch.setattr(server.embeddings_mod, "is_configured", lambda: True)
    async with server.shared_http_client():
        out = await server._dispatch("list_sources", {"check_health": True})
        assert seen == [server._SHARED_CLIENT]
    assert set(out) == {"sources", "semantic_rank_available"}
    assert out["semantic_rank_available"] is True
    health = {s["name"]: s["health"] for s in out["sources"]}
    assert health["zenodo"]["status"] == "up"
    assert health["datacite"] is None  # a source the probe did not report on


@pytest.fixture
def search_calls(monkeypatch):
    calls: list[dict] = []

    async def fake_search_page(client, **kwargs):
        calls.append({"client": client, **kwargs})
        return SearchResult(query="q", total=0, count=0, results=[], errors={})

    monkeypatch.setattr("data_aggregator_mcp.router.search_page", fake_search_page)
    return calls


async def test_search_passes_its_documented_defaults(search_calls) -> None:
    async with server.shared_http_client():
        await server._dispatch("search", {"query": "rice"})
        client = server._SHARED_CLIENT
    assert search_calls == [
        {
            "client": client,
            "query": "rice",
            "size": zenodo.DEFAULT_SIZE,
            "sources": None,
            "organism": None,
            "disease": None,
            "tissue": None,
            "chemical": None,
            "assay": None,
            "published_after": None,
            "published_before": None,
            "kind": None,
            "cursor": None,
            "rank": "relevance",
            "collapse_mirrors": False,
            "understand": False,
            "multi_query": False,
        }
    ]


async def test_search_passes_an_explicit_size(search_calls) -> None:
    await server._dispatch("search", {"query": "rice", "size": 3})
    assert search_calls[0]["size"] == 3


async def test_ontology_elicitation_runs_only_on_a_first_page_with_a_filter(
    monkeypatch, search_calls
) -> None:
    asked: list[dict] = []

    async def fake_correct(client, session, params, *, related_request_id=None):
        asked.append(
            {
                "client": client,
                "session": session,
                "params": dict(params),
                "rid": related_request_id,
            }
        )
        return {"organism": "Oryza sativa"}

    monkeypatch.setattr(server.elicitation, "correct_unresolved", fake_correct)
    session = object()
    ctx = _Ctx(session=session, request_id=7)
    async with server.shared_http_client():
        client = server._SHARED_CLIENT
        await server._dispatch("search", {"query": "rice", "organism": "rise"}, ctx)
        await server._dispatch("search", {"query": "rice"}, ctx)
        await server._dispatch("search", {"cursor": "tok", "organism": "rise"}, ctx)
    assert asked == [
        {
            "client": client,
            "session": session,
            "params": {
                "organism": "rise",
                "disease": None,
                "tissue": None,
                "chemical": None,
                "assay": None,
            },
            "rid": 7,
        }
    ]
    # The correction reached the router; the other two calls kept what they were given.
    assert [c["organism"] for c in search_calls] == ["Oryza sativa", None, "rise"]


async def test_resolve_attaches_a_licence_verdict_using_the_source_default(monkeypatch) -> None:
    async def fake_resolve(client, rid):
        return _record("uniprot:P1", "uniprot")

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    out = await server._dispatch("resolve", {"id": "uniprot:P1", "use": "commercial"})
    default_lic, policy = server.sources.default_license_for("uniprot")
    assert default_lic is not None and policy is not None
    expected = license_mod.check(
        None, "commercial", source_default=default_lic, source_policy=policy
    ).model_dump()
    assert out["license_compat"] == expected
    # Positive control: without the source default the verdict differs, so the
    # comparison above can tell a dropped default from a passed one.
    assert license_mod.check(None, "commercial").model_dump() != expected

    plain = await server._dispatch("resolve", {"id": "uniprot:P1"})
    assert plain["license_compat"] is None


async def test_resolve_passes_the_record_licence_to_the_verdict(monkeypatch) -> None:
    async def fake_resolve(client, rid):
        return _record("zenodo:1", "zenodo", "CC-BY-NC-4.0")

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    out = await server._dispatch("resolve", {"id": "zenodo:1", "use": "commercial"})
    assert out["license_compat"] == license_mod.check("CC-BY-NC-4.0", "commercial").model_dump()
    assert out["license_compat"]["verdict"] == "DENY"


async def test_trust_is_computed_with_the_session_client_on_the_resolved_record(
    monkeypatch,
) -> None:
    annotated: list[tuple] = []

    async def fake_resolve(client, rid):
        return _record()

    async def fake_annotate(client, resource):
        annotated.append((client, resource.id))
        return TrustSignals()

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    monkeypatch.setattr(server.trust_mod, "annotate", fake_annotate)
    async with server.shared_http_client():
        client = server._SHARED_CLIENT
        await server._dispatch("resolve", {"id": "zenodo:1", "trust": True})
        await server._dispatch("resolve", {"id": "zenodo:1", "format": "provenance"})
    assert annotated == [(client, "zenodo:1"), (client, "zenodo:1")]


@pytest.fixture
def fetch_calls(monkeypatch):
    resolved: list[tuple] = []
    fetched: list[dict] = []

    async def fake_resolve(client, rid):
        resolved.append((client, rid))
        return _record()

    async def fake_fetch_files(client, resource, **kwargs):
        on_progress = kwargs.pop("on_progress")
        fetched.append({"client": client, "id": resource.id, **kwargs})
        if on_progress is not None:
            await on_progress(1, 2, "a.txt")
            await on_progress(2, 2, "b.txt")
        return FetchResult(paths=["/tmp/a.txt"], bytes=1)

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    monkeypatch.setattr(fetch_mod, "fetch_files", fake_fetch_files)
    return resolved, fetched


async def test_fetch_passes_its_documented_defaults(fetch_calls) -> None:
    resolved, fetched = fetch_calls
    async with server.shared_http_client():
        client = server._SHARED_CLIENT
        await server._dispatch("fetch", {"id": " zenodo:1 "})
    assert resolved == [(client, "zenodo:1")]
    assert fetched == [
        {
            "client": client,
            "id": "zenodo:1",
            "dest": None,
            "files": None,
            "max_bytes": fetch_mod.DEFAULT_MAX_BYTES,
            "force": False,
            "extract": False,
        }
    ]


async def test_fetch_passes_every_explicit_argument(fetch_calls) -> None:
    _, fetched = fetch_calls
    await server._dispatch(
        "fetch",
        {
            "id": "zenodo:1",
            "dest": "/tmp/d",
            "files": "*.csv",
            "max_bytes": 5,
            "force": True,
            "extract": True,
        },
    )
    assert {k: v for k, v in fetched[0].items() if k != "client"} == {
        "id": "zenodo:1",
        "dest": "/tmp/d",
        "files": "*.csv",
        "max_bytes": 5,
        "force": True,
        "extract": True,
    }


async def test_fetch_reports_each_progress_step(fetch_calls) -> None:
    session = _Session()
    ctx = _Ctx(session=session, meta={"progress_token": "tok"})
    await server._dispatch("fetch", {"id": "zenodo:1"}, ctx)
    assert session.progress == [("tok", 1, 2), ("tok", 2, 2)]


async def test_a_failed_progress_notification_is_logged_and_the_fetch_completes(
    fetch_calls, caplog
) -> None:
    session = _Session(fail=RuntimeError("stream closed"))
    ctx = _Ctx(session=session, meta={"progress_token": "tok"})
    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        out = await server._dispatch("fetch", {"id": "zenodo:1"}, ctx)
    assert out["paths"] == ["/tmp/a.txt"]
    assert [r.getMessage() for r in caplog.records] == [
        "progress notification failed: RuntimeError('stream closed')"
    ] * 2


async def test_fetch_refuses_an_id_with_no_backend_by_name() -> None:
    fid = "bioproject:PRJNA111"
    with pytest.raises(server.FetchNotSupportedError) as exc:
        await server._dispatch("fetch", {"id": fid})
    assert str(exc.value) == str(server.FetchNotSupportedError(fetch_gate.no_backend_message(fid)))


@pytest.fixture
def operate_calls(monkeypatch):
    calls: list[dict] = []

    async def fake_run(client, rid, op, **kwargs):
        calls.append({"client": client, "id": rid, "op": op, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(server.operate, "run", fake_run)
    return calls


async def test_operate_passes_its_documented_defaults(operate_calls) -> None:
    async with server.shared_http_client():
        client = server._SHARED_CLIENT
        await server._dispatch("operate", {"id": "zenodo:1", "op": "schema"})
    assert operate_calls == [
        {
            "client": client,
            "id": "zenodo:1",
            "op": "schema",
            "file": None,
            "query": None,
            "n": 20,
            "columns": None,
        }
    ]


async def test_operate_passes_every_explicit_argument(operate_calls) -> None:
    await server._dispatch(
        "operate",
        {
            "id": "zenodo:1",
            "op": "sql",
            "file": "a.csv",
            "query": "select 1",
            "n": 5,
            "columns": ["x"],
        },
    )
    assert {k: v for k, v in operate_calls[0].items() if k != "client"} == {
        "id": "zenodo:1",
        "op": "sql",
        "file": "a.csv",
        "query": "select 1",
        "n": 5,
        "columns": ["x"],
    }


async def test_relate_uses_the_session_client(monkeypatch) -> None:
    seen: list[tuple] = []

    async def fake_relate(client, ids):
        seen.append((client, ids))
        return RelateResult(input_ids=ids, resolved=ids)

    monkeypatch.setattr("data_aggregator_mcp.router.relate", fake_relate)
    async with server.shared_http_client():
        client = server._SHARED_CLIENT
        await server._dispatch("relate", {"ids": ["zenodo:1", "zenodo:2"]})
    assert seen == [(client, ["zenodo:1", "zenodo:2"])]


async def test_dispatch_names_an_unknown_tool() -> None:
    with pytest.raises(ValueError) as exc:
        await server._dispatch("nope", {})
    assert str(exc.value) == "unknown tool: nope"


# --- _call_tool: the wire contract ---------------------------------------------------


def _call(name: str, arguments: dict) -> types.CallToolRequestParams:
    return types.CallToolRequestParams(name=name, arguments=arguments)


async def test_a_result_travels_as_indented_json_and_structured_content(monkeypatch) -> None:
    result = {"sources": [{"name": "zenodo"}], "semantic_rank_available": False}

    async def fake_dispatch(name, args, ctx=None):
        return result

    monkeypatch.setattr(server, "_dispatch", fake_dispatch)
    out = await server._call_tool(None, _call("list_sources", {}))
    assert out.is_error is False
    assert out.structured_content == result
    assert [(c.type, c.text) for c in out.content] == [("text", json.dumps(result, indent=2))]


async def test_a_result_that_breaks_the_output_schema_is_an_error(monkeypatch) -> None:
    async def fake_dispatch(name, args, ctx=None):
        return {"sources": "not a list"}

    monkeypatch.setattr(server, "_dispatch", fake_dispatch)
    out = await server._call_tool(None, _call("list_sources", {}))
    assert out.is_error is True
    assert out.content[0].type == "text"
    assert out.content[0].text.startswith("Output validation error: ")
    assert "'not a list' is not of type 'array'" in out.content[0].text


async def test_a_tool_without_an_output_schema_returns_its_result(monkeypatch) -> None:
    assert server._TOOLS_BY_NAME["operate"].output_schema is None

    async def fake_dispatch(name, args, ctx=None):
        return {"rows": [1]}

    monkeypatch.setattr(server, "_dispatch", fake_dispatch)
    out = await server._call_tool(None, _call("operate", {"id": "zenodo:1", "op": "head"}))
    assert out.is_error is False
    assert out.structured_content == {"rows": [1]}


async def test_a_non_dict_result_is_named_by_type(monkeypatch) -> None:
    async def fake_dispatch(name, args, ctx=None):
        return ["a"]

    monkeypatch.setattr(server, "_dispatch", fake_dispatch)
    out = await server._call_tool(None, _call("list_sources", {}))
    assert out.is_error is True
    assert out.content[0].text == "Unexpected return type from tool: list"


async def test_a_raised_refusal_reaches_the_caller_verbatim_and_is_logged(caplog) -> None:
    fid = "bioproject:PRJNA111"
    refusal = server.FetchNotSupportedError(fetch_gate.no_backend_message(fid))
    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        out = await server._call_tool(None, _call("fetch", {"id": fid}))
        unknown = await server._call_tool(None, _call("nope", {}))
    assert out.is_error is True
    assert out.content[0].type == "text"
    assert out.content[0].text == str(refusal)
    assert unknown.is_error is True
    assert unknown.content[0].text == "unknown tool: nope"
    assert [r.getMessage() for r in caplog.records] == [
        f"tool fetch failed: FetchNotSupportedError: {refusal}",
        "tool nope failed: ValueError: unknown tool: nope",
    ]


# --- resources and prompts ----------------------------------------------------------


async def test_reading_an_unknown_uri_names_it() -> None:
    uri = "dataresource://nope/x"
    with pytest.raises(ValueError) as exc:
        await server._read_resource(None, types.ReadResourceRequestParams(uri=uri))
    assert str(exc.value) == f"not a readable data-aggregator resource: {uri}"


async def test_reading_a_record_resolves_it_with_the_session_client(monkeypatch) -> None:
    seen: list[tuple] = []

    async def fake_resolve(client, rid):
        seen.append((client, rid))
        return _record(rid)

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    uri = resources_mod.record_uri("zenodo:9")
    async with server.shared_http_client():
        client = server._SHARED_CLIENT
        await server._read_resource(None, types.ReadResourceRequestParams(uri=uri))
    assert seen == [(client, "zenodo:9")]


@pytest.mark.parametrize("prompt", tool_specs.PROMPTS, ids=lambda p: p.name)
async def test_each_prompt_carries_its_own_description(prompt) -> None:
    args = {a.name: "x" for a in prompt.arguments or []}
    out = await server._get_prompt(
        None, types.GetPromptRequestParams(name=prompt.name, arguments=args)
    )
    assert out.description == prompt.description
    assert [(m.role, m.content.type) for m in out.messages] == [("user", "text")]
    assert out.messages[0].content.text == tool_specs.prompt_text(prompt.name, args)
