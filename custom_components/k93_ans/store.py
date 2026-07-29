"""Storage layer for K93 ANS notification history."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_KEY, STORAGE_VERSION
from .models import NotificationRecord


class NotificationStore:
    """Wraps HA's Store helper for the notification history."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._notifications: list[NotificationRecord] = []

    async def async_load(self) -> None:
        """Load persisted notifications from disk."""
        data = await self._store.async_load()
        self._notifications = (data or {}).get("notifications", [])

    async def async_save(self) -> None:
        """Persist notifications to disk."""
        await self._store.async_save({"notifications": self._notifications})

    async def async_add(self, record: NotificationRecord) -> None:
        """Add a new notification record, replacing one with the same id if it already exists.

        The replace path is what makes "live" notifications (see live_id) work: repeated
        send_notification calls for the same live_id reuse the same underlying id, so this
        refreshes that one history entry instead of piling up a new row per update. The updated
        record is (re)inserted at the front rather than left at its old position, so an actively
        updating live notification keeps sorting as the most recent thing that happened instead
        of sitting wherever it was first created - which matters because `async_list`'s `limit`
        would otherwise let a still-live notification silently scroll out of a bounded fetch.
        """
        self._notifications = [r for r in self._notifications if r["id"] != record["id"]]
        self._notifications.insert(0, record)
        await self.async_save()

    def async_get(self, notification_id: str) -> NotificationRecord | None:
        """Return a single notification by id, if present."""
        for record in self._notifications:
            if record["id"] == notification_id:
                return record
        return None

    def async_get_by_live_id(self, live_id: str) -> NotificationRecord | None:
        """Return the active (unacknowledged) live notification for live_id, if any."""
        for record in self._notifications:
            if record.get("live_id") == live_id and not record["acknowledged"]:
                return record
        return None

    def async_list(
        self, include_acknowledged: bool = True, limit: int | None = None
    ) -> list[NotificationRecord]:
        """Return notifications, newest first."""
        records = self._notifications
        if not include_acknowledged:
            records = [r for r in records if not r["acknowledged"]]
        if limit is not None:
            records = records[:limit]
        return records

    async def async_acknowledge(
        self, notification_id: str, via: str
    ) -> NotificationRecord | None:
        """Mark a notification as acknowledged.

        Returns the updated record, or None if it doesn't exist.
        """
        record = self.async_get(notification_id)
        if record is None:
            return None
        record["acknowledged"] = True
        record["acknowledged_at"] = dt_util.utcnow().isoformat()
        record["acknowledged_via"] = via
        await self.async_save()
        return record

    async def async_prune(self, max_records: int, retention_days: int) -> None:
        """Drop notifications older than retention_days, then cap to max_records."""
        cutoff = dt_util.utcnow() - timedelta(days=retention_days)
        kept = [r for r in self._notifications if dt_util.parse_datetime(r["created"]) >= cutoff]
        kept = kept[:max_records]
        if len(kept) != len(self._notifications):
            self._notifications = kept
            await self.async_save()
