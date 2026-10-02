"""What a client can observe of ``tool_specs``: each prompt renders, or is refused,
exactly; each advertised default and limit is the one the handler applies.

``TOOLS`` and ``PROMPTS`` are module constants, which mutmut does not mutate, so the
schema tests here are what fails when an advertised value drifts from the code that
enforces it.
"""

from __future__ import annotations

import pytest
from mcp import MCPError, types
from mcp.types import INVALID_PARAMS

from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp import (
    health,
    license_compat,
    operate,
    router,
    server,
    sources,
    tool_specs,
    zenodo,
)
from data_aggregator_mcp.models import FetchResult, SearchResult
from tests.test_server_observed import _record

# --- prompts ---------------------------------------------------------------------------

_FIND_DATA_TAIL = (
    " Review the compact results, then `resolve` the most relevant id for its full "
    "files[] manifest, and `fetch` to download."
)


@pytest.mark.parametrize(
    ("name", "args", "text"),
    [
        (
            "find_data",
            {"topic": "maize drought"},
            "Use the data-aggregator `search` tool to find datasets about: maize drought."
            + _FIND_DATA_TAIL,
        ),
        (
            "find_data",
            {"topic": "maize drought", "organism": "Zea mays"},
            "Use the data-aggregator `search` tool to find datasets about: maize drought. "
            "Pass organism='Zea mays' to expand the query with NCBI-Taxonomy synonyms."
            + _FIND_DATA_TAIL,
        ),
        (
            "find_data",
            {"topic": "maize drought", "organism": ""},
            "Use the data-aggregator `search` tool to find datasets about: maize drought."
            + _FIND_DATA_TAIL,
        ),
        (
            "data_behind_paper",
            {"paper": "10.1000/xyz"},
            "Find the data behind '10.1000/xyz'. If it is a DOI/PMID, `resolve` it — "
            "publication resolve attaches links[] to datasets/accessions and normalized "
            "identifiers. Then `resolve`/`fetch` each linked dataset. Otherwise `search` for "
            "the paper first.",
        ),
        (
            "search_resolve_fetch",
            {"need": "rice RNA-seq"},
            "Goal: rice RNA-seq. 1) `search` (add organism= to expand taxonomy synonyms). "
            "2) `resolve` a chosen id for the full record + files[]. 3) `fetch` to download. "
            "Use `list_sources` to see which sources are fetchable.",
        ),
    ],
    ids=["find_data", "find_data+organism", "find_data+blank-organism", "paper", "need"],
)
def test_each_prompt_renders_exactly(name, args, text) -> None:
    assert tool_specs.prompt_text(name, args) == text


_REQUIRED = [(p.name, a.name) for p in tool_specs.PROMPTS for a in p.arguments or [] if a.required]


def test_every_prompt_has_a_required_argument_tested_here() -> None:
    assert {name for name, _ in _REQUIRED} == {p.name for p in tool_specs.PROMPTS}


@pytest.mark.parametrize(("name", "arg"), _REQUIRED, ids=lambda x: str(x))
@pytest.mark.parametrize("given", [None, "", "  "], ids=["absent", "empty", "blank"])
def test_a_missing_required_prompt_argument_is_invalid_params(name, arg, given) -> None:
    """MCP spec, prompts/get: a missing required argument is -32602 Invalid params. It
    rendered a prompt with a hole in it ("find datasets about: .")."""
    assert tool_specs.prompt_text(name, {arg: "x"})  # positive control
    args = {} if given is None else {arg: given}
    with pytest.raises(MCPError, match=rf"^prompt '{name}' requires \['{arg}'\]$") as refused:
        tool_specs.prompt_text(name, args)
    assert refused.value.code == INVALID_PARAMS


def test_an_undeclared_prompt_argument_is_invalid_params() -> None:
    """A misspelt optional argument was dropped without a word, so the prompt silently
    lost the taxonomy expansion it was asked for."""
    good = tool_specs.prompt_text("find_data", {"topic": "t", "organism": "rice"})
    assert "organism='rice'" in good  # positive control: the declared spelling works
    with pytest.raises(
        MCPError, match=r"^prompt 'find_data' takes \['topic', 'organism'\], not \['organsim'\]$"
    ) as refused:
        tool_specs.prompt_text("find_data", {"topic": "t", "organsim": "rice"})
    assert refused.value.code == INVALID_PARAMS


async def test_get_prompt_refuses_a_missing_argument_as_invalid_params() -> None:
    ok = await server._get_prompt(
        None, types.GetPromptRequestParams(name="search_resolve_fetch", arguments={"need": "x"})
    )
    assert ok.messages[0].content.text.startswith("Goal: x. ")
    for params in (
        types.GetPromptRequestParams(name="search_resolve_fetch"),
        types.GetPromptRequestParams(name="search_resolve_fetch", arguments={}),
    ):
        with pytest.raises(MCPError, match=r"requires \['need'\]$") as refused:
            await server._get_prompt(None, params)
        assert refused.value.code == INVALID_PARAMS


# --- advertised defaults and limits are the ones the handler applies ------------------


def _props(name: str) -> dict:
    return next(t for t in tool_specs.TOOLS if t.name == name).input_schema["properties"]


@pytest.fixture
def downstream(monkeypatch):
    """Record the arguments each tool hands the code that does the work."""
    seen: list[tuple[str, dict]] = []

    async def fake_search_page(client, **kwargs):
        seen.append(("search", kwargs))
        return SearchResult(query="q", total=0, count=0, results=[], errors={})

    async def fake_resolve(client, rid):
        return _record()

    async def fake_fetch_files(client, resource, **kwargs):
        seen.append(("fetch", kwargs))
        return FetchResult(paths=["/tmp/a.txt"], bytes=1)

    async def fake_operate(client, resource_id, op, **kwargs):
        seen.append(("operate", {"resource_id": resource_id, "op": op, **kwargs}))
        return {}

    monkeypatch.setattr("data_aggregator_mcp.router.search_page", fake_search_page)
    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    monkeypatch.setattr(fetch_mod, "fetch_files", fake_fetch_files)
    monkeypatch.setattr(operate, "run", fake_operate)
    return seen


_MINIMAL_ARGS = {
    "search": {"query": "rice"},
    "fetch": {"id": "zenodo:1"},
    "operate": {"op": "head", "id": "zenodo:1"},
    "list_sources": {},
}

_DEFAULTED = [
    (tool.name, prop)
    for tool in tool_specs.TOOLS
    for prop, spec in tool.input_schema["properties"].items()
    if "default" in spec
]


def test_every_tool_with_a_defaulted_argument_has_a_minimal_call_here() -> None:
    assert {name for name, _ in _DEFAULTED} == set(_MINIMAL_ARGS)
    assert ("operate", "n") in _DEFAULTED and ("search", "size") in _DEFAULTED
    assert len(_DEFAULTED) == 11


@pytest.mark.parametrize(("tool", "prop"), _DEFAULTED, ids=lambda x: str(x))
async def test_omitting_an_argument_is_passing_its_advertised_default(
    downstream, tool, prop
) -> None:
    """The schema's default is what a client is told happens when it leaves the argument
    out; the server restates each one in an ``args.get(name, default)``."""
    default = _props(tool)[prop]["default"]
    omitted = await server._dispatch(tool, dict(_MINIMAL_ARGS[tool]))
    explicit = await server._dispatch(tool, {**_MINIMAL_ARGS[tool], prop: default})
    assert omitted == explicit
    if tool == "list_sources":
        return  # nothing downstream: the static catalog is the whole answer
    assert len(downstream) == 2
    assert downstream[0] == downstream[1]
    # Positive control: another value of the same argument does reach the handler, so
    # the equality above is not two calls that both ignore it.
    other = {"rank": "semantic"}.get(prop) or (
        not default if isinstance(default, bool) else default + 1
    )
    downstream.clear()
    changed = await server._dispatch(tool, {**_MINIMAL_ARGS[tool], prop: other})
    if prop == "provenance":  # read by the server after the search, not passed down
        assert changed != omitted
    else:
        assert downstream[0][1][prop] == other


def test_the_advertised_limits_are_the_enforcing_modules_values() -> None:
    search, operate_props = _props("search"), _props("operate")
    assert search["size"] == {
        "type": "integer",
        "description": "Max results (1-50, default 10)",
        "default": zenodo.DEFAULT_SIZE,
        "minimum": 1,
        "maximum": tool_specs.SEARCH_MAX_SIZE,
    }
    assert (zenodo.DEFAULT_SIZE, tool_specs.SEARCH_MAX_SIZE) == (10, 50)
    # The server always passes n (test_omitting_an_argument_... pins its default), so
    # operate.run's own default is never reached from the tool; mutmut also wraps it.
    assert operate_props["n"]["default"] == 20
    assert operate_props["n"]["minimum"] == 1
    assert operate_props["op"]["enum"] == list(operate.OPERATE_MODES)
    ids = _props("relate")["ids"]
    assert (ids["minItems"], ids["maxItems"]) == (2, router.RELATE_MAX_IDS)
    kinds = ["dataset", "sequencing_run", "study", "publication", "software"]
    assert search["kind"]["enum"] == kinds and set(kinds) == router._VALID_KINDS
    assert search["rank"]["enum"] == ["relevance", "semantic"]
    assert _props("resolve")["format"]["enum"] == ["croissant", "ro-crate", "provenance"]


def test_the_prose_names_the_values_the_code_uses() -> None:
    """A description that states a number or a list must state the enforced one."""
    advertised = _props("search")["sources"]["description"].split("Available: ")[1]
    assert sorted(advertised.split(", ")) == sorted(s.name for s in sources.SOURCES)
    targets = list(health._PROBE_TARGETS)
    probe = _props("list_sources")["check_health"]["description"]
    assert f"probe {len(targets)} sources ({', '.join(targets)})" in probe
    relate = next(t for t in tool_specs.TOOLS if t.name == "relate")
    assert relate.description.startswith(f"Given 2-{router.RELATE_MAX_IDS} resource ids")
    assert _props("relate")["ids"]["description"].startswith(f"2-{router.RELATE_MAX_IDS} ")
    use = _props("resolve")["use"]["description"]
    assert [i for i in license_compat.INTENTS if f"'{i}'" not in use] == []
    assert len(license_compat.INTENTS) == 4  # positive control: the list is not empty
