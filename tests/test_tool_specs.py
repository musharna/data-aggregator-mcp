from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from mcp import MCPError
from mcp.types import INVALID_PARAMS

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
    assert size["maximum"] == tool_specs.SEARCH_MAX_SIZE == 50
    fetch_tool = next(t for t in tool_specs.TOOLS if t.name == "fetch")
    max_bytes = fetch_tool.input_schema["properties"]["max_bytes"]
    assert max_bytes["default"] == fetch_mod.DEFAULT_MAX_BYTES


def test_every_prompt_is_renderable_and_unknown_ones_fail_loud():
    for prompt in tool_specs.PROMPTS:
        required = {a.name: "x" for a in (prompt.arguments or []) if a.required}
        assert tool_specs.prompt_text(prompt.name, required).strip()
    with pytest.raises(MCPError, match=r"^unknown prompt: 'no_such_prompt'$") as refused:
        tool_specs.prompt_text("no_such_prompt", {})
    assert refused.value.code == INVALID_PARAMS


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


# Each source a default search fans out to -> the name the `search` description uses for
# it. A new source without an entry here fails the test, as _FETCH_LABELS does for fetch.
_SEARCH_LABELS = {
    "zenodo": "Zenodo",
    "dataone": "DataONE",
    "gbif": "GBIF",
    "datagov": "data.gov",
    "cellxgene": "CELLxGENE",
    "datacite": "DataCite",
    "dandi": "DANDI",
    "omics": "NCBI omics",
    "literature": "PubMed",
    "huggingface": "HuggingFace",
    "omicsdi": "OmicsDI",
    "openml": "OpenML",
    "pdb": "RCSB PDB",
    "uniprot": "UniProt",
    "gwas": "GWAS Catalog",
    "nasacmr": "NASA CMR",
    "biostudies": "BioStudies",
    "ngdc": "NGDC",
    "mwb": "Metabolomics Workbench",
    "massive": "MassIVE",
    "kaggle": "Kaggle",
}


def test_search_description_names_every_source_a_default_search_queries():
    """The description listed 12 of the 17 sources a search with no `sources` fans out to,
    so a client reading it would not know GBIF, data.gov, NASA CMR, UniProt or BioStudies
    were searched."""
    from data_aggregator_mcp import router

    desc = next(t for t in tool_specs.TOOLS if t.name == "search").description
    queried = set(router._select(None))
    assert "zenodo" in queried and "Zenodo" in desc  # positive control: a known pair matches
    assert queried - _SEARCH_LABELS.keys() == set(), "searched source with no label here"
    missing = sorted(s for s in queried if _SEARCH_LABELS[s] not in desc)
    assert missing == [], f"search description omits: {missing}"


# A quoted or backticked `prefix:id`; `<...>` placeholders (`openaire:<id>`) are skipped.
_QUOTED_ID = re.compile(r"[`'\"]([a-z]+):([^`'\"\s<>]+)[`'\"]")


def _example_ids(text: str, prefixes: set[str]) -> list[str]:
    return [f"{m[1]}:{m[2]}" for m in _QUOTED_ID.finditer(text) if m[1] in prefixes]


def _descriptions(node: object) -> list[str]:
    if isinstance(node, dict):
        own = [node["description"]] if isinstance(node.get("description"), str) else []
        return own + [s for v in node.values() for s in _descriptions(v)]
    return []


def test_every_example_id_shown_to_a_model_is_a_registry_example():
    """An example id in a tool description is the id a model tries first. Only the registry's
    id_example values are resolved live (test_live_every_advertised_id_example_resolves_to_itself);
    a hand-copied one went stale: resolve and operate kept advertising zenodo:7654321 after it
    began redirecting to a restricted, fileless record and the registry had moved on."""
    from data_aggregator_mcp import sources

    prefixes = {p for spec in sources.SOURCES for p in spec.prefixes}
    known = {p.strip() for spec in sources.SOURCES for p in spec.id_example.split("|")}
    assert _example_ids("e.g. 'zenodo:1' or `openaire:<id>`", prefixes) == ["zenodo:1"]
    texts = [
        s for t in tool_specs.TOOLS for s in [t.description or "", *_descriptions(t.input_schema)]
    ]
    texts.append((Path(__file__).parents[1] / "docs" / "reference.md").read_text())
    shown = [i for text in texts for i in _example_ids(text, prefixes)]
    assert "omicsdi:pride:PXD000001" in shown  # positive control: a registry example is found
    assert sorted({i for i in shown if i not in known}) == []


def _flagged(flag: str) -> set[str]:
    import importlib

    return {
        name
        for name in _SEARCH_LABELS
        if getattr(importlib.import_module(f"data_aggregator_mcp.{name}"), flag, False)
    }


def _labels_in(sentence: str, names: set[str]) -> set[str]:
    short = {"mwb": "Workbench"}  # the description names it "the Workbench" after first use
    return {n for n in _SEARCH_LABELS if short.get(n, _SEARCH_LABELS[n]) in sentence} & names


def test_search_description_says_which_every_word_sources_also_search_some_words():
    """0.75.0 gave MassIVE a some-words fill, but the search description kept telling models
    MassIVE had none and to keep its queries short. The two lists are derived here from the
    flags the adapters act on, so a source that gains or loses a fill fails this test until
    the description is changed with it."""
    desc = next(t for t in tool_specs.TOOLS if t.name == "search").description
    every = _flagged("REQUIRES_EVERY_WORD")
    fill = _flagged("ANY_WORD_FILL")
    assert "datacite" in fill and "omics" in every - fill  # positive control: known flags read
    assert fill <= every, "a some-words fill only exists for an every-word source"
    also = re.search(r"([^.]*) are also searched for records holding only some", desc)
    short = re.search(r"keep queries to ([^.]*) short", desc)
    assert also and short, "the description no longer has the two sentences this test reads"
    assert _labels_in(desc.split(" match only records holding every query word")[0], every) == every
    assert _labels_in(also[1], every) == fill
    assert _labels_in(short[1], every) == every - fill
