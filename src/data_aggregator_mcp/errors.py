"""Exception hierarchy for data-aggregator-mcp.

All errors inherit from ``DataAggregatorError`` so callers keep one
``except`` catch-all. The base ``__str__`` prepends the leaf class name so
the MCP SDK's default error serializer preserves the failure type on the
wire (e.g. ``[NotFoundError] Zenodo has no record id='99'``).
"""

from __future__ import annotations


class DataAggregatorError(RuntimeError):
    """Base error for any data-aggregator-mcp backend."""

    def __str__(self) -> str:
        msg = super().__str__()
        if type(self) is DataAggregatorError:
            return msg
        return f"[{type(self).__name__}] {msg}"


class RateLimitError(DataAggregatorError):
    """A backend exhausted its 429 retry budget — transient, back off."""


class NotFoundError(DataAggregatorError):
    """A record / dataset does not exist upstream — terminal for that input."""


class UpstreamUnavailableError(DataAggregatorError):
    """A backend is unreachable or 5xx past its retries, or a checksum failed.

    ``down`` says the host did not answer: no response in time, no connection, or a
    5xx on every try. It is false when the host answered with something unusable
    (another error status, a body that would not parse, a failed checksum), which is
    what an API change looks like; the nightly live suite tells the two apart by it.
    """

    def __init__(self, *args: object, down: bool = False) -> None:
        super().__init__(*args)
        self.down = down


class FetchTooLargeError(DataAggregatorError):
    """Selected files exceed ``max_bytes`` — caller must pass ``force=True``."""


class AuthRequiredError(DataAggregatorError):
    """A source needs a credential that is not configured."""


class FetchNotSupportedError(DataAggregatorError):
    """The resolved resource's source has no fetch adapter yet (discovery-only)."""


class ValidationError(DataAggregatorError):
    """Caller supplied invalid input (bad cursor, unknown filter value, ...)."""


class OperateNotSupportedError(DataAggregatorError):
    """The requested op/file is not operable (not tabular, not range-readable, or
    the [operate] extra is absent)."""
