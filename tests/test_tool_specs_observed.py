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

from data_aggregator_mcp import server, tool_specs

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
