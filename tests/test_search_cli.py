import json

from data_aggregator_mcp import server


def test_search_cli_emits_results_array(monkeypatch, capsys):
    async def fake_dispatch(name, args):
        assert name == "search"
        assert args["query"] == "rice tapetum"
        assert args["size"] == 3
        assert args["sources"] == ["zenodo", "datacite"]
        return {
            "query": "rice tapetum",
            "total": 1,
            "count": 1,
            "results": [
                {
                    "id": "zenodo:1",
                    "source": "zenodo",
                    "kind": "dataset",
                    "title": "Rice tapetum atlas",
                    "doi": "10.5281/zenodo.1",
                    "year": 2023,
                    "creators": [{"name": "A. Lee"}],
                    "description": "single-cell rice",
                }
            ],
            "errors": {},
        }

    monkeypatch.setattr(server, "_dispatch", fake_dispatch)
    server.main(["search", "--json", "--size", "3", "--sources", "zenodo,datacite", "rice tapetum"])
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["title"] == "Rice tapetum atlas"
    assert out[0]["doi"] == "10.5281/zenodo.1"


def test_bare_main_starts_server(monkeypatch):
    recorded = {}

    def fake_run(coro):
        recorded["coro_name"] = getattr(coro, "__qualname__", "") or coro.cr_code.co_name
        coro.close()  # we are not actually awaiting it

    monkeypatch.setattr(server.asyncio, "run", fake_run)
    server.main([])
    assert "_serve" in recorded["coro_name"]
