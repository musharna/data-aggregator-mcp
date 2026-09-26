from __future__ import annotations

import ast
from pathlib import Path

import pytest

from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp import server, tool_specs, zenodo


def test_server_reexports_the_specs_it_serves():
    """server keeps the historical names, but they ARE the tool_specs objects — no copy
    that could drift."""
    assert server.TOOLS is tool_specs.TOOLS
    assert server._PROMPTS is tool_specs.PROMPTS
    assert server._prompt_text is tool_specs.prompt_text


@pytest.mark.asyncio
async def test_mcp_handlers_serve_the_specs():
    """Go through the registered MCP handlers, not the module constants."""
    from mcp import types

    # 2.x results are pydantic models that copy the list on construction, so identity
    # is checked at the module level above; here the served content must be equal.
    assert (await server._list_tools(None, None)).tools == tool_specs.TOOLS
    assert (await server._list_prompts(None, None)).prompts == tool_specs.PROMPTS
    result = await server._get_prompt(
        None, types.GetPromptRequestParams(name="find_data", arguments={"topic": "maize drought"})
    )
    assert "maize drought" in result.messages[0].content.text


def test_the_advertised_schema_defaults_come_from_the_modules_that_enforce_them():
    """A hand-copied default would silently promise a limit the code does not apply."""
    search = next(t for t in tool_specs.TOOLS if t.name == "search")
    size = search.input_schema["properties"]["size"]
    assert size["default"] == zenodo.DEFAULT_SIZE
    assert size["maximum"] == zenodo.MAX_SIZE
    fetch_tool = next(t for t in tool_specs.TOOLS if t.name == "fetch")
    max_bytes = fetch_tool.input_schema["properties"]["max_bytes"]
    assert max_bytes["default"] == fetch_mod.DEFAULT_MAX_BYTES


def test_every_prompt_is_renderable_and_unknown_ones_fail_loud():
    for prompt in tool_specs.PROMPTS:
        required = {a.name: "x" for a in (prompt.arguments or []) if a.required}
        assert tool_specs.prompt_text(prompt.name, required).strip()
    with pytest.raises(ValueError, match="unknown prompt"):
        tool_specs.prompt_text("no_such_prompt", {})


def test_tool_specs_stays_free_of_request_handling_imports():
    """The point of the split: specs are declarative. Importing the router or an HTTP client
    here would put request handling back into the static-data module."""
    tree = ast.parse(Path(tool_specs.__file__ or "").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {m for m in imported if m.endswith("router") or m == "httpx"}, sorted(imported)


# Each fetchable id prefix -> the name the `fetch` description must use for it. A new
# fetchable prefix without an entry here fails the test, so the list cannot go stale again.
_FETCH_LABELS = {
    "zenodo": "Zenodo",
    "dataone": "DataONE",
    "gbif": "GBIF",
    "datagov": "data.gov",
    "cellxgene": "CELLxGENE",
    "datacite": "DataCite",
    "dandi": "DANDI",
    "geo": "GEO",
    "sra": "SRA",
    "openaire": "OpenAIRE",
    "pubmed": "PubMed",
    "hf": "HuggingFace",
    "omicsdi": "OmicsDI",
    "openml": "OpenML",
    "pdb": "PDB",
    "uniprot": "UniProt",
    "biostudies": "BioStudies",
}


def test_fetch_description_names_every_fetchable_source():
    from data_aggregator_mcp import sources

    desc = next(t for t in tool_specs.TOOLS if t.name == "fetch").description
    prefixes = {p.rstrip(":") for p in sources.FETCHABLE_PREFIXES}
    assert "zenodo" in prefixes and "Zenodo" in desc  # positive control: a known pair matches
    assert prefixes - _FETCH_LABELS.keys() == set(), "fetchable prefix with no label here"
    missing = sorted(p for p in prefixes if _FETCH_LABELS[p] not in desc)
    assert missing == [], f"fetch description omits: {missing}"
