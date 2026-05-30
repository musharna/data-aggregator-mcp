from __future__ import annotations

from data_aggregator_mcp.errors import (
    DataAggregatorError,
    FetchNotSupportedError,
    FetchTooLargeError,
    NotFoundError,
)


def test_subclass_str_prepends_class_name() -> None:
    assert str(NotFoundError("no record")) == "[NotFoundError] no record"


def test_base_str_has_no_prefix() -> None:
    assert str(DataAggregatorError("raw")) == "raw"


def test_fetch_too_large_is_subclass() -> None:
    assert issubclass(FetchTooLargeError, DataAggregatorError)


def test_fetch_not_supported_is_data_aggregator_error() -> None:
    err = FetchNotSupportedError("fetch is Zenodo-only in Phase 2")
    assert isinstance(err, DataAggregatorError)
    assert str(err) == "[FetchNotSupportedError] fetch is Zenodo-only in Phase 2"
