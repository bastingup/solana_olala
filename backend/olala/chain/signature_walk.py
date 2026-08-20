"""The "never skip, never replay" signature walk, written once.

This logic existed twice — in the discovery scanner and in the follow
daemon — with different bugs in each. The follower's was the dangerous
one: if the poll window did not contain the cursor, it treated **every**
entry as fresh. A node lagging behind, or a burst longer than the window,
therefore made it re-execute trades it had already copied. With real
money that is a duplicate buy.

The fix is to stop using a bare signature as a position marker. A
signature alone cannot be compared; a **slot** can. The watermark is
``(slot, signature)`` of the newest entry fully handled, and:

* an entry is fresh only if it is above the watermark slot, or shares
  that slot and is not already in ``processed``;
* the watermark advances only across a walk that reached the previous
  watermark — a window that did not reach it is INCOMPLETE, and pages
  back rather than guessing;
* if paging runs out before reaching it, that is
  :class:`~olala.chain.errors.SourceIncomplete`, and the watermark stays
  where it is. Losing sight of some trades is recoverable. Copying them
  twice is not.

Several transactions can share one slot, which is why ``processed``
exists as well as the slot: the slot bounds how much of it must be kept.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .errors import SourceIncomplete

logger = logging.getLogger(__name__)

#: Pages of history to walk back before declaring a gap unbridgeable.
DEFAULT_MAX_PAGES = 5
#: Entries per catch-up page. ``getSignaturesForAddress`` caps at 1000
#: and costs the same as asking for one, so walking back shallowly is
#: pure downside.
DEFAULT_CATCHUP_PAGE_SIZE = 1000


@dataclass(frozen=True)
class Watermark:
    """Newest signature fully handled for one address.

    ``slot == 0`` means "not armed yet": nothing has been handled, and
    the first observation arms it rather than replaying all history as
    live trades.
    """

    slot: int = 0
    signature: str = ""

    @property
    def armed(self) -> bool:
        return self.slot > 0 or bool(self.signature)

    def advanced_to(self, entry: Mapping[str, Any]) -> "Watermark":
        return Watermark(slot=int(entry.get("slot") or 0),
                         signature=str(entry.get("signature") or ""))


@dataclass
class WalkResult:
    """Outcome of one walk over an address's recent signatures."""

    #: Entries newer than the watermark, OLDEST FIRST — the order they
    #: must be executed in.
    fresh: list[dict[str, Any]]
    #: Whether the walk reached back to the watermark. False means a gap
    #: remains and the watermark MUST NOT advance past it.
    complete: bool
    #: Newest entry seen, for arming an unarmed watermark.
    newest: dict[str, Any] | None = None
    #: The entry that IS the watermark, when it was found in the window.
    #: Lets a pre-slot cursor learn its own slot without losing history.
    matched: dict[str, Any] | None = None
    #: Pages of history consumed.
    pages: int = 1

    @property
    def empty(self) -> bool:
        return not self.fresh


def entry_slot(entry: Mapping[str, Any]) -> int:
    return int(entry.get("slot") or 0)


def select_fresh(entries: Iterable[Mapping[str, Any]],
                 watermark: Watermark,
                 processed: Mapping[str, int] | None = None) -> WalkResult:
    """Split one page of signatures (NEWEST FIRST) against the watermark.

    Pure: no I/O, so the awkward cases are cheap to test.
    """
    page = [dict(e) for e in entries if e.get("signature")]
    if not page:
        return WalkResult(fresh=[], complete=True, newest=None)

    newest = page[0]
    if not watermark.armed:
        # First contact: arm at the newest entry. Everything before it is
        # history, and replaying history as live trades would buy into
        # positions the trader exited weeks ago.
        return WalkResult(fresh=[], complete=True, newest=newest)

    seen = processed or {}
    fresh: list[dict[str, Any]] = []
    matched: dict[str, Any] | None = None
    complete = False
    for item in page:                       # newest -> oldest
        signature = item["signature"]
        slot = entry_slot(item)
        if signature == watermark.signature:
            complete = True
            matched = item
            break
        if slot and slot < watermark.slot:
            # We walked clean past the watermark slot without meeting the
            # marker signature — it has aged out of this node's view, but
            # the window demonstrably covers the gap.
            complete = True
            break
        if signature in seen:
            continue
        if slot and slot == watermark.slot:
            # Same slot as the marker but not the marker and not seen:
            # genuinely new, several transactions share a slot.
            fresh.append(item)
            continue
        fresh.append(item)

    fresh.reverse()                          # oldest first
    return WalkResult(fresh=fresh, complete=complete, newest=newest,
                      matched=matched)


def collect_fresh(fetch: Callable[..., list[dict[str, Any]]],
                  address: str, watermark: Watermark, *,
                  processed: Mapping[str, int] | None = None,
                  page_size: int = 30,
                  catchup_page_size: int = DEFAULT_CATCHUP_PAGE_SIZE,
                  max_pages: int = DEFAULT_MAX_PAGES,
                  first_page: list[dict[str, Any]] | None = None
                  ) -> WalkResult:
    """Walk back until the watermark is reached, or admit the gap.

    ``fetch(address, limit, before)`` supplies pages. Pass ``first_page``
    when the caller already has it — a batched sweep fetches everyone's
    first page in one request, and only the few addresses that need
    paging come back here.

    Raises :class:`SourceIncomplete` if the watermark is not reached
    within ``max_pages``. That is the whole point: an unreachable
    watermark must stop the caller, never silently reset it.
    """
    page = (first_page if first_page is not None
            else fetch(address, page_size, None))
    result = select_fresh(page, watermark, processed)
    if result.complete:
        return result
    # NOT `or not result.fresh`. An empty result means there is no WORK
    # here, which says nothing about whether we reached the watermark —
    # every entry may simply have been handled already. Stopping on it
    # left the watermark frozen while reality moved on, so the gap grew
    # every cycle until it passed the lookback and the wallet was wedged
    # for good. Measured live: watermarks 87,000 slots adrift, three
    # wallets permanently blind, 858 unbridgeable-gap errors.

    collected = list(result.fresh)
    newest = result.newest
    oldest_signature = page[-1]["signature"] if page else None
    pages = 1

    while pages < max_pages and oldest_signature:
        pages += 1
        # Catch-up pages ask for far more than the first one. A public
        # node charges per CALL, not per signature, and returns up to a
        # thousand entries for the same price — so a shallow walk back
        # buys nothing and risks exactly the wedge described above.
        page = fetch(address, catchup_page_size, oldest_signature)
        if not page:
            # Nothing older, and we still never met the watermark. The
            # marker is a transaction of THIS address, so its absence
            # means the node's history is pruned or partial — we cannot
            # prove what lies between, and dispatching across an
            # unproven span is how a trade gets copied twice.
            raise SourceIncomplete(
                f"{address}: history ends before watermark slot "
                f"{watermark.slot} — refusing to advance the cursor over "
                f"an unproven span")
        older = select_fresh(page, watermark, processed)
        collected = older.fresh + collected      # older entries go first
        if older.complete:
            return WalkResult(fresh=collected, complete=True,
                              newest=newest, pages=pages,
                              matched=older.matched)
        oldest_signature = page[-1]["signature"]

    raise SourceIncomplete(
        f"{address}: walked {pages} pages of {page_size} signatures without "
        f"reaching watermark slot {watermark.slot}; refusing to advance the "
        f"cursor over an unknown gap")
