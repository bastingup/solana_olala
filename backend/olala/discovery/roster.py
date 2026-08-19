"""Seat competition for the followed-trader roster.

Both discovery streams end here, and both compete on the same terms: a
nominee takes a free seat, or it must beat the weakest incumbent by
``discovery.replace_margin``. Keeping this in one place is what stops
the two streams from developing different ideas about who deserves a
seat.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..domain.models import TraderProfile, TraderStatus
from ..services.traders import TraderRegistry

logger = logging.getLogger(__name__)


class Roster:
    def __init__(self, registry: TraderRegistry,
                 assign_wallet: Callable[[], str],
                 counters: dict[str, int], db: Any = None) -> None:
        self._registry = registry
        self._assign_wallet = assign_wallet
        self._counters = counters
        self._db = db

    def followed(self) -> list[TraderProfile]:
        return self._registry.followed()

    def weakest(self) -> TraderProfile | None:
        followed = self.followed()
        return min(followed, key=lambda p: p.score) if followed else None

    def claim_seat(self, config, address: str, score: float) -> bool:
        """Is there room for this score — evicting the weakest if not?

        Returns True when a seat is available (already freed if an
        eviction was required). Callers must be ready to USE the seat:
        an eviction has already happened by the time this returns True.
        """
        followed = self.followed()
        if len(followed) < config.discovery.max_followed_traders:
            return True
        worst = self.weakest()
        if worst is None:
            # A roster capped at zero has no seats and nothing to evict;
            # min() over an empty list would raise.
            return False
        margin = config.discovery.replace_margin
        if score <= worst.score + margin:
            return False
        self.retire_for(worst, address, score)
        return True

    def retire_for(self, worst: TraderProfile, replacement: str,
                   score: float) -> None:
        """Evict the weakest followed trader for a stronger find.

        Open positions stay with their wallet, protected by the panic
        stop, and close on their own exits — mirroring manual unfollow.
        """
        worst.status = TraderStatus.RETIRED
        worst.rejection_reason = (
            f"replaced by {replacement[:6]}… "
            f"(score {score:.3f} vs {worst.score:.3f})")
        self._registry.update(worst, event="trader_retired")
        logger.info("retired trader %s: %s", worst.address[:8],
                    worst.rejection_reason)

    def follow(self, profile: TraderProfile, score: float,
               stats: Any = None,
               watermark: tuple[int, str] | None = None) -> None:
        """Seat a trader and hand it a wallet to trade through.

        ``watermark`` arms tracking at the trader's CURRENT newest
        signature. Without it the tracker would arm itself on its next
        sweep — which is also safe, but a moment later, so a trade landing
        in between would be missed.
        """
        profile.status = TraderStatus.FOLLOWED
        profile.rejection_reason = ""
        profile.score = score
        if stats is not None:
            profile.stats = stats
        profile.assigned_wallet_id = self._assign_wallet()
        if not profile.assigned_wallet_id:
            # No wallet to trade through: the seat would be occupied by a
            # trader that can never produce a fill. Say so loudly rather
            # than let the roster look full while nothing happens.
            logger.warning("trader %s… followed but no wallet was "
                           "available to assign — it cannot trade until "
                           "a wallet exists", profile.address[:8])
        self._registry.update(profile, event="trader_admitted")
        if watermark is not None and self._db is not None:
            slot, signature = watermark
            if signature:
                self._db.update_watermarks(
                    [(profile.address, slot, signature)])
        self._counters["admitted"] += 1

    def reject(self, profile: TraderProfile, reason: str) -> None:
        profile.status = TraderStatus.REJECTED
        profile.rejection_reason = reason or "did not meet the bar"
        self._registry.update(profile, event="trader_rejected")
        self._counters["rejected"] += 1
        logger.info("rejected trader %s: %s", profile.address[:8],
                    profile.rejection_reason)
