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
    """A backend is unreachable or 5xx past its retries, or a checksum failed."""


class FetchTooLargeError(DataAggregatorError):
    """Selected files exceed ``max_bytes`` — caller must pass ``force=True``."""


class AuthRequiredError(DataAggregatorError):
    """A source needs a credential that is not configured."""


class FetchNotSupportedError(DataAggregatorError):
    """The resolved resource's source has no fetch adapter yet (discovery-only)."""


class ValidationError(DataAggregatorError):
    """Caller supplied invalid input (bad cursor, unknown filter value, ...)."""
