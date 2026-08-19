"""Trader registry: thread-safe in-memory roster of traders, backed by SQLite.

The discovery daemon writes candidates and admissions here; the follower
daemon reads the followed set and its polling cursors. All mutations are
persisted immediately and broadcast on the event bus.
"""

from __future__ import annotations

import threading

from ..domain.models import TraderProfile, TraderStatus
from ..events import EventBus
from ..persistence.database import Database


class TraderRegistry:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self._db = db
        self._bus = bus
        self._lock = threading.RLock()
        self._profiles: dict[str, TraderProfile] = {}
        self._history_cursors: dict[str, str] = {}
        self._history_complete: dict[str, bool] = {}
        for row in db.load_traders():
            profile = Database.trader_from_row(row)
            self._profiles[profile.address] = profile
            self._history_cursors[profile.address] = row["history_cursor"]
            self._history_complete[profile.address] = bool(row["history_complete"])

    # -- queries -----------------------------------------------------------

    def get(self, address: str) -> TraderProfile | None:
        with self._lock:
            return self._profiles.get(address)

    def all(self) -> list[TraderProfile]:
        with self._lock:
            return list(self._profiles.values())

    def by_status(self, status: TraderStatus) -> list[TraderProfile]:
        with self._lock:
            return [p for p in self._profiles.values() if p.status is status]

    def followed(self) -> list[TraderProfile]:
        return self.by_status(TraderStatus.FOLLOWED)

    def history_cursor(self, address: str) -> tuple[str, bool]:
        with self._lock:
            return (self._history_cursors.get(address, ""),
                    self._history_complete.get(address, False))

    # -- mutations ---------------------------------------------------------

    def add_candidate(self, address: str) -> bool:
        with self._lock:
            if address in self._profiles:
                return False
            profile = TraderProfile(address=address)
            self._profiles[address] = profile
            self._persist(profile)
        self._bus.publish("trader_candidate", profile.to_dict())
        return True

    def update(self, profile: TraderProfile, history_cursor: str | None = None,
               history_complete: bool | None = None,
               event: str = "") -> None:
        """Persist a profile change.

        The tracking watermark is NOT part of this. It belongs to the
        tracker, which is its sole writer — a registry that also wrote it
        wiped the tracker's progress on every score update.
        """
        with self._lock:
            self._profiles[profile.address] = profile
            if history_cursor is not None:
                self._history_cursors[profile.address] = history_cursor
            if history_complete is not None:
                self._history_complete[profile.address] = history_complete
            self._persist(profile)
        if event:
            self._bus.publish(event, profile.to_dict())

    def _persist(self, profile: TraderProfile) -> None:
        self._db.upsert_trader(
            profile,
            history_cursor=self._history_cursors.get(profile.address, ""),
            history_complete=self._history_complete.get(profile.address, False))
