"""The chain error taxonomy — what makes routing possible at all.

Every failure used to arrive as a bare ``ChainError``: a 429, a dead
node, a malformed parameter and a node whose history did not reach far
enough were indistinguishable. Nothing could route around a rate limit,
because nothing could tell one had happened.

Every class here still subclasses ``ChainError``, so existing
``except ChainError`` sites keep working exactly as before while gaining
the ability to discriminate. The distinctions that matter:

``SourceRejected``
    Our request was wrong. Every other source will reject it too, so
    failing over is pointless and merely multiplies the damage.

``SourceIncomplete``
    The source answered honestly but its history does not reach as far
    back as we need. A cursor MUST NOT advance over this — treating a
    short answer as a complete one is how a tracker skips trades.

``SourceUnsupported``
    The method is not available here at all (public nodes disable
    ``getTokenLargestAccounts``). Permanent for this source+method pair,
    so it is worth remembering rather than rediscovering every call.
"""

from __future__ import annotations


class ChainError(RuntimeError):
    """A chain request failed."""


class SourceError(ChainError):
    """A failure attributable to one source.

    ``source`` names it so logs and metrics can say who, and
    ``retry_after`` carries the server's own guidance when it gave any.
    """

    #: Whether trying the next source in the policy could plausibly help.
    failover = True

    def __init__(self, message: str, *, source: str = "",
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.source = source
        self.retry_after = retry_after


class SourceRateLimited(SourceError):
    """The source is throttling us. Back off here, ask someone else."""


class SourceUnavailable(SourceError):
    """Transport failure, 5xx, timeout, or a node that is behind."""


class SourceRejected(SourceError):
    """The request itself is invalid — do NOT fail over."""

    failover = False


class SourceIncomplete(SourceError):
    """The source cannot see far enough back to answer completely.

    A cursor or watermark must never advance across one of these.
    """


class SourceUnsupported(SourceError):
    """This source does not implement the method (JSON-RPC -32601)."""


class SourceDataError(SourceError):
    """The response was well-formed HTTP but not the shape we expect."""


# Kept for callers written before the taxonomy existed.
RateLimited = SourceRateLimited


# -- JSON-RPC error code mapping -------------------------------------------

# Solana's server errors are documented per method; these are the codes
# that change what a caller should DO. Anything unrecognised is treated
# as "this source is having a problem", which fails over safely.
_INCOMPLETE_CODES = frozenset({
    -32004,   # block not available for slot
    -32007,   # slot skipped, or missing due to ledger jump
    -32009,   # slot was skipped / cleaned up
    -32019,   # failed to query long-term storage
})
_REJECTED_CODES = frozenset({
    -32600,   # invalid request
    -32602,   # invalid params
    -32700,   # parse error
})


def classify_rpc_error(error: dict, *, source: str = "",
                       method: str = "") -> SourceError:
    """Turn a JSON-RPC ``error`` object into the right exception type."""
    code = error.get("code")
    message = str(error.get("message") or "").strip()
    detail = f"{method}: {message or error}" if method else (message or error)

    if code == -32601:
        return SourceUnsupported(f"{detail}", source=source)
    if code in _REJECTED_CODES:
        return SourceRejected(f"{detail}", source=source)
    if code in _INCOMPLETE_CODES:
        return SourceIncomplete(f"{detail}", source=source)
    if code == -32005:
        # Overloaded: "Node is behind by N slots" AND "Too many requests".
        # Only the latter should slow our own bucket down.
        lowered = message.lower()
        if "too many" in lowered or "rate" in lowered:
            return SourceRateLimited(f"{detail}", source=source)
        return SourceUnavailable(f"{detail}", source=source)
    return SourceUnavailable(f"{detail}", source=source)
