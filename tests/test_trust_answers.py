"""Each Crossref /works/{doi} answer beside the trust verdict it must give.

Probed live 2026-10-02 (api.crossref.org):
- ``facet=update-type:*`` over all works: retraction 76,026, expression_of_concern
  4,307, withdrawal 3,397, removal 702, partial_retraction 2; also spelled
  ``Retraction`` (3) and ``expression-of-concern`` (6). No ``reinstatement``.
- A withdrawn or removed article carries its notice under ``updated-by`` exactly like a
  retraction, often pointing at itself (Elsevier re-titles the article "WITHDRAWN:" /
  "REMOVED:"). Before 2026-10-02 all of them read ``retracted=False``: clean.
- A work's answer is ``{"status": "ok", "message-type": "work", "message": {...}}``; the
  message always has a ``DOI``; ``updated-by`` is absent or a list of dicts each with a
  string ``type`` (700 sampled works, 500 of them with updates, 586 updates).
"""

import copy

import httpx
import pytest

from data_aggregator_mcp import _http, trust
from data_aggregator_mcp.models import DataResource


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _work(doi: str, *updates: dict) -> dict:
    message: dict = {"DOI": doi, "type": "journal-article"}
    if updates:
        message["updated-by"] = list(updates)
    return {"status": "ok", "message-type": "work", "message-version": "1.0.0", "message": message}


def _update(doi: str, utype: str, source: str = "publisher") -> dict:
    return {"DOI": doi, "type": utype, "label": utype.title(), "source": source}


# Verbatim updated-by lists from the live answers (``updated`` dates dropped).
_WAKEFIELD = _work(
    "10.1016/s0140-6736(97)11096-0",
    {
        **_update("10.1016/s0140-6736(04)15715-2", "correction", "retraction-watch"),
        "record-id": "17269",
    },
    {
        **_update("10.1016/s0140-6736(10)60175-4", "retraction", "retraction-watch"),
        "record-id": "4036",
    },
)
_WITHDRAWN = _work(  # "WITHDRAWN: RF - Should Striae be Considered a Predisposing F…"
    "10.1016/j.adengl.2021.11.027", _update("10.1016/j.adengl.2021.11.027", "withdrawal")
)
_WITHDRAWN_BY_NOTICE = _work(  # AIAA: a separate withdrawal notice
    "10.2514/6.2018-2123", _update("10.2514/6.2018-2123.c1", "withdrawal")
)
_REMOVED = _work(  # "REMOVED: Short-term ocean tidal parameters…", plus an erratum
    "10.1016/j.asr.2025.03.045",
    _update("10.1016/j.asr.2025.10.008", "erratum"),
    _update("10.1016/j.asr.2025.03.045", "removal"),
)
_PARTIAL = _work(
    "10.29328/journal.jcmhs.1001023",
    _update("10.29328/journal.jcmhs.1001023", "partial_retraction"),
)
_CLEAN = _work("10.1038/nature14539")
_CORRECTED = _work("10.1/c", _update("10.1/c.erratum", "correction"))


def _client(*bodies: object, status: int = 200) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """A client answering ``bodies`` in turn (the last one repeated)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=bodies[min(len(seen), len(bodies)) - 1])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def _resource(doi: str = "10.1/x") -> DataResource:
    return DataResource(id="pub:1", source="literature", kind="publication", title="t", doi=doi)


async def _verdict(body: object) -> tuple[bool | None, str | None, bool | None]:
    client, _ = _client(body)
    async with client:
        t = await trust.annotate(client, _resource())
    return t.retracted, t.retraction_doi, t.concern


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "notice"),
    [
        (_WAKEFIELD, "10.1016/s0140-6736(10)60175-4"),
        (_WITHDRAWN, "10.1016/j.adengl.2021.11.027"),
        (_WITHDRAWN_BY_NOTICE, "10.2514/6.2018-2123.c1"),
        (_REMOVED, "10.1016/j.asr.2025.03.045"),
        (_PARTIAL, "10.29328/journal.jcmhs.1001023"),
    ],
    ids=["retraction", "withdrawal", "withdrawal-notice", "removal", "partial-retraction"],
)
async def test_a_notice_that_takes_the_work_out_of_the_record_is_a_retraction(body, notice):
    """A withdrawn or removed paper read as clean: only ``type == "retraction"`` counted."""
    assert await _verdict(body) == (True, notice, False)
    # positive control: a correction alone leaves the work in the record
    assert await _verdict(_CORRECTED) == (False, None, False)


@pytest.mark.asyncio
async def test_an_update_type_is_read_in_any_case_or_separator():
    """The live index spells some types ``Retraction`` and ``expression-of-concern``."""
    upper = _work("10.1/x", _update("10.1/r", "Retraction"))
    hyphen = _work("10.1/x", _update("10.1/e", "expression-of-concern"))
    assert await _verdict(upper) == (True, "10.1/r", False)
    assert await _verdict(hyphen) == (False, None, True)
    assert await _verdict(_CLEAN) == (False, None, False)


@pytest.mark.asyncio
async def test_the_first_retraction_notice_is_reported():
    both = _work("10.1/x", _update("10.1/w", "withdrawal"), _update("10.1/r", "retraction"))
    assert await _verdict(both) == (True, "10.1/w", False)


@pytest.mark.asyncio
async def test_a_retraction_notice_without_a_doi_is_still_a_retraction():
    body = _work("10.1/x", {"type": "retraction", "source": "retraction-watch"})
    assert await _verdict(body) == (True, None, False)


def _without_message() -> dict:
    body = copy.deepcopy(_CLEAN)
    del body["message"]
    return body


def _message(**fields: object) -> dict:
    body = copy.deepcopy(_CLEAN)
    body["message"].update(fields)
    return body


_MALFORMED = {
    "no message": _without_message(),
    "message null": {"status": "ok", "message": None},
    "message a list": {"status": "ok", "message": []},
    "message without DOI": {"status": "ok", "message": {"type": "journal-article"}},
    "DOI not a string": _message(DOI=10),
    "updated-by null": _message(**{"updated-by": None}),
    "updated-by a dict": _message(**{"updated-by": {"type": "retraction"}}),
    "updated-by a string": _message(**{"updated-by": "retraction"}),
    "update not a dict": _message(**{"updated-by": ["retraction"]}),
    "update without type": _message(**{"updated-by": [{"DOI": "10.1/r"}]}),
    "update type not a string": _message(**{"updated-by": [{"type": ["retraction"]}]}),
    "one bad update among good": _message(
        **{"updated-by": [_update("10.1/c", "correction"), {"label": "Retraction"}]}
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", list(_MALFORMED.values()), ids=list(_MALFORMED))
async def test_a_malformed_answer_is_unknown_not_clean(body):
    """Each of these read as ``retracted=False``: a missing or unreadable update list is
    not an empty one. It is retried, then reported unknown."""
    client, seen = _client(body)
    async with client:
        t = await trust.annotate(client, _resource())
    assert (t.retracted, t.retraction_doi, t.concern) == (None, None, None)
    assert len(seen) == trust.MAX_RETRIES  # retried as a malformed answer
    # positive control: a retry that gets a well-formed answer reads it
    client, seen = _client(body, _CLEAN)
    async with client:
        t = await trust.annotate(client, _resource())
    assert (t.retracted, t.concern, len(seen)) == (False, False, 2)


def test_check_work_names_the_answer():
    with pytest.raises(
        _http.UpstreamEnvelopeError, match=r"^no Crossref work in \{'status': 'ok'\}$"
    ):
        trust._check_work({"status": "ok"})
    trust._check_work(_WAKEFIELD)  # positive control: a live answer passes
