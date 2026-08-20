"""The slot watermark: never skip, never replay.

The bug being fixed had teeth. The old cursor was a bare signature, and
when the poll window did not contain it, every entry in that window was
treated as fresh — so a lagging node or a burst longer than the window
made the follower re-execute trades it had already copied. That is a
duplicate buy with real money.
"""

import pytest

from olala.chain.errors import SourceIncomplete
from olala.chain.signature_walk import (Watermark, collect_fresh,
                                        select_fresh)


def sig(name, slot, err=None):
    return {"signature": name, "slot": slot, "err": err,
            "blockTime": 1_755_000_000 + slot}


def names(entries):
    return [e["signature"] for e in entries]


# -- arming ----------------------------------------------------------------

def test_first_contact_arms_without_replaying_history():
    """Otherwise the first tick buys into positions exited weeks ago."""
    page = [sig("c", 300), sig("b", 200), sig("a", 100)]
    result = select_fresh(page, Watermark())
    assert result.fresh == []
    assert result.complete
    assert result.newest["signature"] == "c"


def test_empty_page_is_complete_and_empty():
    result = select_fresh([], Watermark(slot=100, signature="a"))
    assert result.fresh == [] and result.complete


# -- the ordinary case -----------------------------------------------------

def test_fresh_entries_come_back_oldest_first():
    """Execution order is chronological: a BUY must precede its SELL."""
    page = [sig("d", 400), sig("c", 300), sig("b", 200), sig("a", 100)]
    result = select_fresh(page, Watermark(slot=100, signature="a"))
    assert names(result.fresh) == ["b", "c", "d"]
    assert result.complete


def test_nothing_new_yields_nothing():
    page = [sig("a", 100)]
    result = select_fresh(page, Watermark(slot=100, signature="a"))
    assert result.fresh == [] and result.complete


# -- the replay bug --------------------------------------------------------

def test_window_that_misses_the_watermark_is_incomplete_not_all_fresh():
    """THE regression. The old code saw no cursor in the window and
    treated all 30 entries as fresh, re-copying every one."""
    page = [sig(f"s{i}", 900 + i) for i in range(5)]      # all above 100
    result = select_fresh(page, Watermark(slot=100, signature="old"))
    assert result.complete is False
    assert len(result.fresh) == 5      # they ARE fresh, but the gap is open


def test_entries_below_the_watermark_slot_are_never_fresh():
    page = [sig("new", 500), sig("older", 50), sig("oldest", 40)]
    result = select_fresh(page, Watermark(slot=100, signature="gone"))
    # 'older'/'oldest' are beneath the watermark: already handled.
    assert names(result.fresh) == ["new"]
    # Walking clean past the watermark slot proves the window covers it,
    # even though the marker signature itself has aged out of view.
    assert result.complete


def test_already_processed_signatures_are_not_re_emitted():
    page = [sig("c", 300), sig("b", 200), sig("a", 100)]
    result = select_fresh(page, Watermark(slot=100, signature="a"),
                          processed={"b": 200})
    assert names(result.fresh) == ["c"]


def test_several_transactions_in_one_slot_are_all_seen():
    """Slots hold many transactions; a slot-only comparison would drop
    every sibling of the watermark."""
    page = [sig("x2", 100), sig("x1", 100), sig("mark", 100),
            sig("older", 90)]
    result = select_fresh(page, Watermark(slot=100, signature="mark"))
    assert names(result.fresh) == ["x1", "x2"]
    assert result.complete


def test_processed_set_disambiguates_siblings_in_the_watermark_slot():
    page = [sig("x2", 100), sig("x1", 100), sig("mark", 100)]
    result = select_fresh(page, Watermark(slot=100, signature="mark"),
                          processed={"x1": 100})
    assert names(result.fresh) == ["x2"]


# -- paging ----------------------------------------------------------------

class Pager:
    """Serves a fixed history newest-first, honouring `before`."""

    def __init__(self, entries):
        self.entries = entries          # newest first
        self.calls = []

    def __call__(self, address, limit, before=None):
        self.calls.append((address, limit, before))
        start = 0
        if before:
            index = names(self.entries).index(before)
            start = index + 1
        return self.entries[start:start + limit]


def test_paging_walks_back_until_the_watermark_is_reached():
    history = [sig(f"s{i:02d}", 100 + i) for i in range(20, 0, -1)]
    history.append(sig("mark", 100))
    pager = Pager(history)

    result = collect_fresh(pager, "W", Watermark(slot=100, signature="mark"),
                           page_size=5, max_pages=10)

    assert result.complete
    assert names(result.fresh) == [f"s{i:02d}" for i in range(1, 21)]
    assert result.pages > 1                       # it really did page


def test_paging_preserves_chronological_order_across_pages():
    history = [sig(f"s{i:02d}", 100 + i) for i in range(6, 0, -1)]
    history.append(sig("mark", 100))
    result = collect_fresh(Pager(history), "W",
                           Watermark(slot=100, signature="mark"),
                           page_size=2, max_pages=10)
    assert names(result.fresh) == ["s01", "s02", "s03", "s04", "s05", "s06"]


def test_an_unbridgeable_gap_raises_instead_of_advancing():
    """Losing sight of trades is recoverable; copying them twice is not."""
    history = [sig(f"s{i:03d}", 1000 + i) for i in range(60, 0, -1)]
    with pytest.raises(SourceIncomplete, match="refusing to advance"):
        collect_fresh(Pager(history), "W",
                      Watermark(slot=100, signature="long-gone"),
                      page_size=5, max_pages=3)


def test_history_ending_before_the_watermark_raises():
    """The marker is a transaction of this very address, so its absence
    from an exhausted history means the node's view is pruned. We cannot
    prove what lies between, and guessing means copying trades twice."""
    history = [sig("b", 300), sig("a", 200)]
    with pytest.raises(SourceIncomplete, match="unproven span"):
        collect_fresh(Pager(history), "W",
                      Watermark(slot=100, signature="never-seen"),
                      page_size=2, max_pages=5)


def test_caller_supplied_first_page_avoids_a_duplicate_fetch():
    """A batched sweep already holds everyone's first page; refetching it
    would double the cost of the cheap path."""
    page = [sig("b", 200), sig("a", 100)]
    pager = Pager(page)
    result = collect_fresh(pager, "W", Watermark(slot=100, signature="a"),
                           page_size=2, first_page=page)
    assert names(result.fresh) == ["b"]
    assert pager.calls == []           # no fetch at all


# -- watermark value semantics --------------------------------------------

def test_watermark_arming_and_advance():
    assert Watermark().armed is False
    assert Watermark(slot=5).armed is True
    advanced = Watermark().advanced_to(sig("z", 900))
    assert advanced.slot == 900 and advanced.signature == "z"


# -- the frozen-watermark bug ---------------------------------------------
#
# Found in production after an overnight run: watermarks 87,000 slots
# adrift, three wallets permanently blind, 858 unbridgeable-gap errors,
# and one copied trade in ten hours.
#
# The walk stopped paging when a page contained no FRESH work. But "no
# work here" says nothing about whether the watermark was reached — every
# entry may simply have been handled already. So the marker froze while
# the chain moved on, the gap grew every cycle, and once it passed the
# lookback the wallet was wedged for good.

def test_a_page_of_already_processed_entries_still_pages_to_the_watermark():
    history = [sig(f"s{i:02d}", 100 + i) for i in range(20, 0, -1)]
    history.append(sig("mark", 100))
    pager = Pager(history)
    # Everything in the first page has already been handled.
    processed = {f"s{i:02d}": 100 + i for i in range(20, 15, -1)}

    result = collect_fresh(pager, "W", Watermark(slot=100, signature="mark"),
                           processed=processed, page_size=5, max_pages=10)

    # It must reach the watermark, so the caller can advance it.
    assert result.complete
    # And still surface the entries that were NOT already handled.
    assert names(result.fresh) == [f"s{i:02d}" for i in range(1, 16)]


def test_an_entirely_processed_history_still_reaches_the_watermark():
    """The exact production shape: every recent signature above the
    watermark had been handled, so the walk returned nothing and the
    marker never moved again."""
    history = [sig(f"s{i:02d}", 100 + i) for i in range(20, 0, -1)]
    history.append(sig("mark", 100))
    processed = {f"s{i:02d}": 100 + i for i in range(20, 0, -1)}

    result = collect_fresh(Pager(history), "W",
                           Watermark(slot=100, signature="mark"),
                           processed=processed, page_size=5, max_pages=10)

    assert result.complete
    assert result.fresh == []
    assert result.newest["signature"] == "s20"   # so the caller can advance


def test_catch_up_pages_ask_for_far_more_than_the_first_page():
    """A public node charges per CALL, not per signature, and returns up
    to 1000 for the same price — a shallow walk back buys nothing."""
    history = [sig(f"s{i:03d}", 100 + i) for i in range(300, 0, -1)]
    history.append(sig("mark", 100))
    pager = Pager(history)

    collect_fresh(pager, "W", Watermark(slot=100, signature="mark"),
                  page_size=30, catchup_page_size=1000, max_pages=5)

    limits = [limit for _, limit, _ in pager.calls]
    assert limits[0] == 30            # the cheap first look
    assert all(limit == 1000 for limit in limits[1:])
    # 300 signatures of lookback used to need 10 pages of 30 and failed
    # at 5; one deep page covers it.
    assert len(pager.calls) == 2
