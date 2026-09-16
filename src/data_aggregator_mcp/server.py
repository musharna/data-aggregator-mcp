"""MCP server — exposes search/resolve/fetch/list_sources over stdio or HTTP.

Transport is chosen at the CLI: bare invocation serves stdio (unchanged),
``--transport http`` serves streamable HTTP via ``http_transport``. Both share
this module's ``server`` object and every handler below; only the stream pair
differs. See ``http_transport`` for the HTTP security posture and for how
``fetch`` semantics change when the server is not a local child of the client.

search/resolve fan out through the multi-source router (Zenodo + DataCite +
NCBI omics + literature: PubMed/OpenAIRE). fetch streams files for Zenodo,
SRA (ENA FASTQ, md5-verified), GEO supplementary records, and DataCite-discovered
Figshare/Dataverse/OSF deposits (md5-verified), and open-access literature full text
(EuropePMC XML / Unpaywall PDF, unverified); a DataCite Dryad id is manifest-only
(resolve lists files, fetch fails loud), and other DataCite repos plus paywalled/non-OA
literature ids fail loud.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import jsonschema
from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

from data_aggregator_mcp import (
    __version__,
    citation,
    egress,
    elicitation,
    operate,
    router,
    run_crate,
    sources,
    tool_specs,
    zenodo,
)
from data_aggregator_mcp import croissant as croissant_mod
from data_aggregator_mcp import dossier as dossier_mod
from data_aggregator_mcp import embeddings as embeddings_mod
from data_aggregator_mcp import fair as fair_mod
from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp import health as health_mod
from data_aggregator_mcp import license_compat as license_mod
from data_aggregator_mcp import resources as resources_mod
from data_aggregator_mcp import ro_crate as ro_crate_mod
from data_aggregator_mcp import trust as trust_mod
from data_aggregator_mcp.errors import FetchNotSupportedError, ValidationError
from data_aggregator_mcp.models import DataResource

logger = logging.getLogger(__name__)

# id prefixes with a working fetch backend — derived from the central registry (which
# also feeds router's adapter map + discovery-only set), so the gate can't drift from it.
_FETCHABLE_SOURCES = sources.FETCHABLE_PREFIXES


def _is_fetchable(fid: str) -> bool:
    """True if ``fid`` has a wired fetch backend (allowlisted prefix or bare Zenodo id)."""
    return fid.startswith(_FETCHABLE_SOURCES) or fid.isdigit()


# DataCite ids all share the `datacite:` prefix, so fetchability is decided
# post-resolve from the detected host repo. Dryad is manifest-only (downloads are
# token/bot-challenge gated), so it is NOT here.
_DATACITE_FETCHABLE = ("figshare", "dataverse", "osf", "zenodo", "openneuro")


def _ensure_repo_fetchable(fid: str, resource: DataResource) -> None:
    """Fail loud when a datacite: id resolves to a host repo we can't stream."""
    if fid.startswith("datacite:") and resource.source not in _DATACITE_FETCHABLE:
        hint = (
            " Dryad downloads are token/bot-challenge gated." if resource.source == "dryad" else ""
        )
        raise FetchNotSupportedError(
            f"'{fid}' (repo: {resource.source}) is discovery-only for fetch — its file "
            f"manifest is available via resolve, but no adapter streams its bytes.{hint}"
        )


def _ensure_gbif_fetchable(fid: str, resource: DataResource) -> None:
    """Fail loud when a gbif: id resolved to no Darwin Core Archive — a metadata-only
    (or feed-only) dataset is discovery-only; occurrence/checklist/sampling-event
    datasets carry a DWC_ARCHIVE and pass."""
    if fid.startswith("gbif:") and not resource.files:
        raise FetchNotSupportedError(
            f"'{fid}' is discovery-only for fetch — this GBIF dataset publishes no "
            "Darwin Core Archive (metadata-only). Resolve it for the DOI / landing page instead."
        )


def _ensure_datagov_fetchable(fid: str, resource: DataResource) -> None:
    """Fail loud when a datagov: id resolved to no downloadable resource — a metadata-
    only / link-only package is discovery-only; packages with CKAN resources pass."""
    if fid.startswith("datagov:") and not resource.files:
        raise FetchNotSupportedError(
            f"'{fid}' is discovery-only for fetch — this data.gov package publishes no "
            "downloadable resource. Resolve it for the landing page / metadata instead."
        )


def _ensure_omicsdi_fetchable(fid: str, resource: DataResource) -> None:
    """Fail loud when an omicsdi: id resolved to no files — its repo (MassIVE,
    Metabolomics Workbench, GNPS, PeptideAtlas) is discovery-only this wave.
    PRIDE/MetaboLights populate files[] at resolve and pass."""
    if fid.startswith("omicsdi:") and not resource.files:
        landing = next((lnk.target_id for lnk in resource.links if lnk.rel == "landing_page"), None)
        where = f" Fetch from the source repo directly: {landing}" if landing else ""
        raise FetchNotSupportedError(
            f"'{fid}' is discovery-only for fetch — only PRIDE and MetaboLights records "
            f"are streamable; this repo exposes no wired fetch backend.{where}"
        )


_LITERATURE_PREFIXES = ("pubmed:", "openaire:")


def _ensure_fulltext_available(fid: str, resource: DataResource) -> None:
    """Fail loud when a literature id has no open-access full text to fetch
    (paywalled, or not in EuropePMC/Unpaywall) — don't return a silently empty
    result (spec §8)."""
    if fid.startswith(_LITERATURE_PREFIXES) and not resource.files:
        raise FetchNotSupportedError(
            f"no open-access full text was found for '{fid}' — it may be paywalled, absent "
            "from EuropePMC/Unpaywall, or the lookup itself may have failed. Resolve it for "
            "the landing page / DOI instead."
        )


# The catalog advertised by list_sources. Defined once, per-source, in sources.py —
# alongside the routing/fetch data it must stay consistent with.
_SOURCES: list[dict[str, Any]] = sources.CATALOG

# Static wire specs live in tool_specs; re-exported here under their historical names.
TOOLS: list[types.Tool] = tool_specs.TOOLS

# httpx pools connections per client, so a client per dispatch re-does DNS + TLS to
# every host on every call — a cost sequential calls (paging, resolve-after-search)
# pay again and again. A serve session owns one client for its whole lifetime; see
# `_http_client`.
_SHARED_CLIENT: httpx.AsyncClient | None = None


@contextlib.asynccontextmanager
async def shared_http_client() -> AsyncGenerator[None]:
    """Install one HTTP client for the enclosing serve session (both transports).

    Not reentrant: the client is bound to the running event loop, and a nested
    session would close a client the outer one is still handing out.
    """
    global _SHARED_CLIENT
    if _SHARED_CLIENT is not None:
        raise RuntimeError("shared_http_client is already active")
    async with httpx.AsyncClient(
        follow_redirects=True,
        # Every hop is validated, redirects included: checking only the URL a caller
        # hands us is defeated by a public URL that 302s into private space.
        event_hooks={"request": [egress.enforce_on_request]},
    ) as client:
        _SHARED_CLIENT = client
        try:
            yield
        finally:
            _SHARED_CLIENT = None


@contextlib.asynccontextmanager
async def _http_client() -> AsyncGenerator[httpx.AsyncClient]:
    """The client for one dispatch: the serve session's, left open for the next call,
    or — with no session (unit tests, the search CLI) — a fresh one that closes here."""
    if _SHARED_CLIENT is not None:
        yield _SHARED_CLIENT
        return
    async with httpx.AsyncClient(
        follow_redirects=True,
        # Every hop is validated, redirects included: checking only the URL a caller
        # hands us is defeated by a public URL that 302s into private space.
        event_hooks={"request": [egress.enforce_on_request]},
    ) as client:
        yield client


# mcp 2.x handlers: each takes the per-request context and the typed request
# params and returns the typed result; nothing is auto-wrapped any more.
_TOOLS_BY_NAME: dict[str, types.Tool] = {t.name: t for t in TOOLS}


async def _list_tools(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=TOOLS)


_PROMPTS: list[types.Prompt] = tool_specs.PROMPTS


async def _list_prompts(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
) -> types.ListPromptsResult:
    return types.ListPromptsResult(prompts=_PROMPTS)


_prompt_text = tool_specs.prompt_text


async def _get_prompt(
    ctx: ServerRequestContext, params: types.GetPromptRequestParams
) -> types.GetPromptResult:
    name = params.name
    args = params.arguments or {}
    text = _prompt_text(name, args)
    return types.GetPromptResult(
        description=next((p.description for p in _PROMPTS if p.name == name), None),
        messages=[
            types.PromptMessage(role="user", content=types.TextContent(type="text", text=text)),
        ],
    )


async def _list_resources(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
) -> types.ListResourcesResult:
    return types.ListResourcesResult(resources=resources_mod.static_resources())


async def _list_resource_templates(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
) -> types.ListResourceTemplatesResult:
    return types.ListResourceTemplatesResult(resource_templates=resources_mod.templates())


async def _read_resource(
    ctx: ServerRequestContext, params: types.ReadResourceRequestParams
) -> types.ReadResourceResult:
    uri = params.uri
    if resources_mod.is_catalog(uri):
        payload = json.dumps({"sources": _SOURCES})
    else:
        rid = resources_mod.parse_record_id(uri)
        if rid is None:
            raise ValueError(f"not a readable data-aggregator resource: {uri}")
        async with _http_client() as client:
            resource = await router.resolve(client, rid)
        payload = resource.model_dump_json()
    return types.ReadResourceResult(
        contents=[
            types.TextResourceContents(uri=str(uri), text=payload, mime_type="application/json")
        ]
    )


async def _dispatch(
    name: str, args: dict[str, Any], ctx: ServerRequestContext | None = None
) -> Any:
    """Run one tool. ``ctx`` is the MCP request context (session, request id, meta)
    when called from the wire; None from unit tests and the search CLI."""
    if name == "list_sources":
        semantic_available = embeddings_mod.is_configured()
        if not args.get("check_health"):
            return {"sources": _SOURCES, "semantic_rank_available": semantic_available}
        async with _http_client() as client:
            probed = {h["name"]: h for h in await health_mod.probe_sources(client)}
        return {
            "sources": [{**s, "health": probed.get(s["name"])} for s in _SOURCES],
            "semantic_rank_available": semantic_available,
        }
    async with _http_client() as client:
        match name:
            case "search":
                ontology_args = {
                    f: args.get(f) for f in ("organism", "disease", "tissue", "chemical", "assay")
                }
                # S1.6: give a capable client the chance to fix an ontology term that
                # matches nothing BEFORE the fan-out, rather than silently dropping the
                # filter. Skipped on a cursor continuation — those params are replayed
                # from the cursor, already prompted for on page 1. No-ops (returns {})
                # for every client that can't or won't answer; never raises.
                if not args.get("cursor") and any(ontology_args.values()):
                    corrections = await elicitation.correct_unresolved(
                        client,
                        getattr(ctx, "session", None),
                        ontology_args,
                        related_request_id=getattr(ctx, "request_id", None),
                    )
                    ontology_args.update(corrections)
                result = await router.search_page(
                    client,
                    query=args.get("query"),
                    size=args.get("size", zenodo.DEFAULT_SIZE),
                    sources=args.get("sources"),
                    organism=ontology_args["organism"],
                    disease=ontology_args["disease"],
                    tissue=ontology_args["tissue"],
                    chemical=ontology_args["chemical"],
                    assay=ontology_args["assay"],
                    published_after=args.get("published_after"),
                    published_before=args.get("published_before"),
                    kind=args.get("kind"),
                    cursor=args.get("cursor"),
                    rank=args.get("rank", "relevance"),
                    collapse_mirrors=args.get("collapse_mirrors", False),
                    understand=args.get("understand", False),
                    multi_query=args.get("multi_query", False),
                )
                if args.get("provenance"):
                    result = result.model_copy(
                        update={"provenance_crate": run_crate.render(result)}
                    )
                return result.model_dump()
            case "resolve":
                resource = await router.resolve(client, args["id"])
                cite = args.get("cite")
                if cite:
                    rendered = await citation.render(client, resource, cite)
                    resource = resource.model_copy(update={"citation": rendered})
                fmt = args.get("format")
                if fmt == "croissant":
                    resource = resource.model_copy(
                        update={"croissant": croissant_mod.render(resource)}
                    )
                elif fmt == "ro-crate":
                    resource = resource.model_copy(
                        update={"ro_crate": ro_crate_mod.render(resource)}
                    )
                if args.get("trust"):
                    signals = await trust_mod.annotate(client, resource)
                    resource = resource.model_copy(update={"trust": signals})
                if args.get("fair"):
                    resource = resource.model_copy(update={"fair": fair_mod.assess(resource)})
                if use := args.get("use"):
                    default_lic, lic_policy = sources.default_license_for(resource.source)
                    resource = resource.model_copy(
                        update={
                            "license_compat": license_mod.check(
                                resource.license,
                                use,
                                source_default=default_lic,
                                source_policy=lic_policy,
                            )
                        }
                    )
                if fmt == "provenance":
                    if resource.fair is None:
                        resource = resource.model_copy(update={"fair": fair_mod.assess(resource)})
                    if resource.trust is None:
                        resource = resource.model_copy(
                            update={"trust": await trust_mod.annotate(client, resource)}
                        )
                    resource = resource.model_copy(
                        update={"provenance": dossier_mod.render(resource)}
                    )
                return resource.model_dump()
            case "fetch":
                fid = args["id"].strip()
                if not _is_fetchable(fid):
                    raise FetchNotSupportedError(
                        f"'{fid}' has no wired fetch backend. Fetchable id prefixes: "
                        f"{', '.join(_FETCHABLE_SOURCES)} (and bare Zenodo ids). "
                        "Resolve it for the landing page / DOI instead."
                    )
                resource = await router.resolve(client, fid)
                _ensure_repo_fetchable(fid, resource)
                _ensure_fulltext_available(fid, resource)
                _ensure_omicsdi_fetchable(fid, resource)
                _ensure_gbif_fetchable(fid, resource)
                _ensure_datagov_fetchable(fid, resource)
                # Wire MCP progress notifications when the caller supplied a
                # progressToken (in the request meta). The notification is
                # auxiliary telemetry: a send failure is logged and swallowed so
                # it can NEVER abort or mask the actual download. This is the one
                # sanctioned fail-soft spot — the core fetch still succeeds.
                # 2.x: ctx.meta is the request's `_meta` mapping (snake_case keys).
                meta = ctx.meta if ctx is not None else None
                token = meta.get("progress_token") if meta else None
                on_progress = None
                if token is not None and ctx is not None:
                    session = ctx.session

                    async def _on_progress(done: int, total: int, name: str) -> None:
                        try:
                            await session.send_progress_notification(
                                token, progress=done, total=total
                            )
                        except Exception as exc:  # noqa: BLE001 - auxiliary telemetry
                            logger.warning("progress notification failed: %r", exc)

                    on_progress = _on_progress
                out = await fetch_mod.fetch_files(
                    client,
                    resource,
                    dest=args.get("dest"),
                    files=args.get("files"),
                    max_bytes=args.get("max_bytes", fetch_mod.DEFAULT_MAX_BYTES),
                    force=args.get("force", False),
                    extract=args.get("extract", False),
                    on_progress=on_progress,
                )
                return out.model_dump()
            case "operate":
                return await operate.run(
                    client,
                    args["id"],
                    args["op"],
                    file=args.get("file"),
                    query=args.get("query"),
                    n=args.get("n", 20),
                    columns=args.get("columns"),
                )
            case "relate":
                rel = await router.relate(client, args["ids"])
                return rel.model_dump()
            case _:
                raise ValueError(f"unknown tool: {name}")


def _error_result(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


async def _call_tool(
    ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    """The tool-layer contract, kept exactly as mcp 1.x's ``@call_tool`` gave it.

    1.x validated arguments against ``inputSchema``, wrapped a dict return into
    ``structuredContent`` plus its JSON as text, validated that against
    ``outputSchema``, and turned ANY handler exception into
    ``CallToolResult(isError=True, content=[str(exc)])``. 2.x does none of that:
    an exception leaving this function becomes a JSON-RPC *error*, and on a
    modern-era connection the SDK replaces its text with a generic "Internal
    server error" (``mcp/server/runner.py::modern_error_data``). Every refusal
    this server raises (``FetchNotSupportedError``, ``ValidationError``, ...) is
    written for the model to read, so it must travel as an error *result* with
    its text intact — which is what this wrapper guarantees on both transports.
    """
    name = params.name
    arguments = params.arguments or {}
    tool = _TOOLS_BY_NAME.get(name)
    try:
        if tool is not None:
            try:
                jsonschema.validate(instance=arguments, schema=tool.input_schema)
            except jsonschema.ValidationError as e:
                return _error_result(f"Input validation error: {e.message}")
        result = await _dispatch(name, arguments, ctx)
        if not isinstance(result, dict):
            return _error_result(f"Unexpected return type from tool: {type(result).__name__}")
        if tool is not None and tool.output_schema is not None:
            try:
                jsonschema.validate(instance=result, schema=tool.output_schema)
            except jsonschema.ValidationError as e:
                return _error_result(f"Output validation error: {e.message}")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, indent=2))],
            structured_content=result,
            is_error=False,
        )
    except Exception as exc:  # noqa: BLE001 - the wire contract: every failure reaches the model as text
        logger.warning("tool %s failed: %s: %s", name, type(exc).__name__, exc)
        return _error_result(str(exc))


# Pass version explicitly: the SDK falls back to reporting ITS OWN version in
# initialize().serverInfo when this is None, so clients were told the server was
# "1.28.1" (the mcp SDK) rather than the package version. Affects both transports.
server: Server = Server(
    "data-aggregator-mcp",
    version=__version__,
    on_list_tools=_list_tools,
    on_call_tool=_call_tool,
    on_list_prompts=_list_prompts,
    on_get_prompt=_get_prompt,
    on_list_resources=_list_resources,
    on_list_resource_templates=_list_resource_templates,
    on_read_resource=_read_resource,
)


async def _serve() -> None:
    async with shared_http_client(), stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _run_search_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="data-aggregator-mcp search")
    parser.add_argument("query")
    parser.add_argument("--json", action="store_true", help="emit JSON (the only format)")
    parser.add_argument("--size", type=int, default=zenodo.DEFAULT_SIZE)
    parser.add_argument("--sources", default=None, help="comma-separated sources; default: all")
    ns = parser.parse_args(argv)
    sources = [s.strip() for s in ns.sources.split(",") if s.strip()] if ns.sources else None
    try:
        page = asyncio.run(
            _dispatch("search", {"query": ns.query, "size": ns.size, "sources": sources})
        )
    except Exception as exc:  # fail loud on stderr, non-zero exit
        print(f"data-aggregator-mcp search: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    results = page.get("results", [])
    errors = page.get("errors", {}) or {}
    # The router tolerates a failing source so the OTHER sources can still
    # answer, and reports it in ``errors``. Dropping that dict here turned an
    # upstream outage into ``[]`` with exit 0 - indistinguishable from "no
    # hits", and a vacuous pass for anything asserting over the list.
    for name, msg in errors.items():
        print(f"data-aggregator-mcp search: {name}: {msg}", file=sys.stderr)
    if errors and not results:
        print("data-aggregator-mcp search: every source failed", file=sys.stderr)
        raise SystemExit(1)
    json.dump(results, sys.stdout)
    sys.stdout.write("\n")


def _run_serve_cli(argv: list[str]) -> None:
    """Parse transport flags and serve. Bare invocation keeps serving stdio."""
    from data_aggregator_mcp import http_transport

    parser = argparse.ArgumentParser(prog="data-aggregator-mcp")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio (default) or streamable HTTP",
    )
    parser.add_argument(
        "--host",
        default=http_transport.DEFAULT_HOST,
        help=(
            "http only; bind address (default %(default)s = this machine only). "
            "A non-loopback value requires --allow-host."
        ),
    )
    parser.add_argument("--port", type=int, default=http_transport.DEFAULT_PORT, help="http only")
    parser.add_argument(
        "--allow-host",
        action="append",
        default=None,
        metavar="HOST:PORT",
        help="http only; permitted Host header, repeatable. Required off loopback.",
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=None,
        metavar="ORIGIN",
        help="http only; permitted browser Origin header, repeatable.",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        help="http only; fresh transport per request, no session affinity",
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        help="http only; plain JSON responses instead of SSE streams",
    )
    ns = parser.parse_args(argv)

    if ns.transport == "stdio":
        asyncio.run(_serve())
        return

    # fetch(dest=...) writes to THIS process's filesystem. Over stdio that is the
    # caller's own disk; over HTTP it may not be. Say so rather than surprise them.
    print(
        "data-aggregator-mcp: serving over HTTP — note that fetch() writes to this "
        "server's filesystem, not the client's.",
        file=sys.stderr,
    )
    try:
        http_transport.serve_http(
            host=ns.host,
            port=ns.port,
            allow_hosts=ns.allow_host,
            allow_origins=ns.allow_origin,
            stateless=ns.stateless,
            json_response=ns.json_response,
        )
    except ValidationError as exc:
        print(f"data-aggregator-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "search":
        _run_search_cli(args[1:])
        return
    _run_serve_cli(args)


if __name__ == "__main__":
    main()
