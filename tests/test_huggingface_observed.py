"""What the Hugging Face adapter sends and how it reads what comes back, pinned exactly.

Shapes from the live Hub (2026-10-02): see ``test_huggingface_answers.py``.
"""

import logging

import httpx
import pytest

from data_aggregator_mcp import _http, hf_datasets_server, huggingface
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import FileEntry
from tests.test_huggingface_answers import _SROIE

_ID = "priyank-m/SROIE_2019_text_recognition"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _call(r: httpx.Request) -> tuple[str, str, str | None]:
    return r.method, str(r.url), r.headers.get("accept")


@pytest.mark.asyncio
async def test_each_request_is_exactly_what_the_hub_is_sent(monkeypatch):
    sent: list[httpx.Request] = []
    seen: list[tuple[object, str]] = []

    async def fake_parquet(client, ds_id):
        seen.append((client, ds_id))
        return []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=[_SROIE] if request.url.params.get("search") else _SROIE)

    monkeypatch.setattr(hf_datasets_server, "parquet_files", fake_parquet)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await huggingface.search(c, "SROIE 2019", size=99)
        await huggingface.resolve(c, f"hf:{_ID}")
    assert [_call(s) for s in sent] == [
        (
            "GET",
            "https://huggingface.co/api/datasets?search=SROIE+2019&limit=50&full=true",
            "application/json",
        ),
        ("GET", f"https://huggingface.co/api/datasets/{_ID}?full=true", "application/json"),
    ]
    # The datasets-server lookup is made on the caller's client, for the same dataset.
    assert seen == [(c, _ID)]


@pytest.mark.asyncio
async def test_an_unknown_dataset_is_not_found():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(404, text="Not Found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(
            NotFoundError, match=r"^\[NotFoundError\] HuggingFace has no dataset 'owner/missing'$"
        ):
            await huggingface.resolve(c, "hf:owner/missing")
    assert len(sent) == 1


@pytest.mark.parametrize("bad", ["hf:owner/a..b", "hf:owner/a b", "hf:../x", "hf:a/b/c"])
@pytest.mark.asyncio
async def test_a_malformed_id_is_refused_before_any_request(bad):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=_SROIE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(NotFoundError) as exc:
            await huggingface.resolve(c, bad)
        assert str(exc.value) == f"[NotFoundError] malformed HuggingFace id {bad!r}"
        assert sent == []
        # Positive control: a well-formed id with dots and dashes is requested.
        await huggingface.resolve(c, "hf:owner/a.b-c")
    assert [s.url.path for s in sent if s.url.host == "huggingface.co"] == [
        "/api/datasets/owner/a.b-c"
    ]


@pytest.mark.asyncio
async def test_a_failed_datasets_server_lookup_is_logged_by_dataset(monkeypatch, caplog):
    async def fake_parquet(client, ds_id):
        raise RuntimeError("datasets-server 503")

    monkeypatch.setattr(hf_datasets_server, "parquet_files", fake_parquet)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_SROIE))
    ) as c:
        with caplog.at_level(logging.WARNING, logger=huggingface.logger.name):
            r = await huggingface.resolve(c, f"hf:{_ID}")
    assert [m.getMessage() for m in caplog.records] == [
        f"datasets-server enrichment failed for {_ID}: RuntimeError('datasets-server 503')"
    ]
    assert r.errors == {
        "files": "HuggingFace datasets-server lookup failed: RuntimeError: datasets-server 503; "
        "converted parquet files unknown"
    }


@pytest.mark.asyncio
async def test_converted_parquet_files_follow_the_repository_files(monkeypatch):
    parquet = FileEntry(name="default/train/0000.parquet", url="https://h/0.parquet")

    async def fake_parquet(client, ds_id):
        return [parquet]

    monkeypatch.setattr(hf_datasets_server, "parquet_files", fake_parquet)
    body = {**_SROIE, "siblings": [{"rfilename": ".gitattributes"}, {"rfilename": "a.csv"}]}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as c:
        r = await huggingface.resolve(c, f"hf:{_ID}")
    assert [f.name for f in r.files] == ["a.csv", "default/train/0000.parquet"]
    assert r.errors == {}


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({"downloads": 320}, (320, None)),
        ({"likes": 14}, (None, 14)),
        ({"downloads": 0, "likes": 0}, (0, 0)),
    ],
)
def test_either_hub_count_alone_is_a_metric(counts, expected):
    """Positive control: with neither count the record has no metrics."""
    base = {k: v for k, v in _SROIE.items() if k not in ("downloads", "likes")}
    m = huggingface._normalize({**base, **counts}).metrics
    assert m is not None and (m.downloads, m.likes) == expected
    assert huggingface._normalize(base).metrics is None


@pytest.mark.parametrize(
    ("created", "year"),
    [("2022-08-27T20:56:31.000Z", 2022), (None, None), ("", None), ("n/a", None)],
)
def test_the_year_is_read_from_the_creation_date(created, year):
    assert huggingface._normalize({**_SROIE, "createdAt": created}).year == year


def test_subjects_are_the_tags_without_a_prefix():
    r = huggingface._normalize(_SROIE)
    assert r.subjects == ["text-recognition", "recognition"]
    assert r.doi is None and r.creators[0].name == "priyank-m"
