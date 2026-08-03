"""Storage layer for K93 ANS notification history."""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CUSTOM_STORAGE_FILENAME,
    IMPORTANCE_LEVELS,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .models import NotificationRecord

_LOGGER = logging.getLogger(__name__)


def _importance_rank(level: str) -> int:
    try:
        return IMPORTANCE_LEVELS.index(level)
    except ValueError:
        return IMPORTANCE_LEVELS.index("normal")


class NotificationStore:
    """Wraps HA's Store helper for the notification history - or, if `storage_path` is set, a
    plain JSON file written directly at `<storage_path>/k93_ans_notifications.json` instead.

    The custom-path mode intentionally forgoes everything homeassistant.helpers.storage.Store
    normally gives you for free (debounced writes, atomic replace, storage-version migration) in
    exchange for landing the file wherever the user actually asked for it - `Store` itself has no
    way to do that, it always writes under `.storage/` in the HA config directory. Changing
    `storage_path` only takes effect on the next integration setup (HA restart or reload), and
    nothing here migrates an existing file from the old location to the new one automatically.
    """

    def __init__(self, hass: HomeAssistant, storage_path: str | None = None) -> None:
        self._hass = hass
        self._custom_path: Path | None = None
        self._store: Store | None = None
        if storage_path:
            directory = Path(storage_path)
            if not directory.is_absolute():
                directory = Path(hass.config.path(storage_path))
            self._custom_path = directory / CUSTOM_STORAGE_FILENAME
        else:
            self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._notifications: list[NotificationRecord] = []
        self._pending_live: dict[str, tuple[str, str]] = {}

    def _read_custom_file(self) -> dict[str, Any] | None:
        """Blocking read of the custom-path JSON file - call via hass.async_add_executor_job."""
        assert self._custom_path is not None
        if not self._custom_path.exists():
            default_path = Path(self._hass.config.path(".storage", STORAGE_KEY))
            if default_path.exists():
                _LOGGER.warning(
                    "K93 ANS: %s doesn't exist yet, but %s does - starting fresh at the new "
                    "location; this isn't migrated automatically, copy it over yourself if you "
                    "want to keep that history",
                    self._custom_path,
                    default_path,
                )
            return None
        try:
            with self._custom_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            _LOGGER.exception(
                "K93 ANS failed reading notification history from %s", self._custom_path
            )
            return None

    def _write_custom_file(self, data: dict[str, Any]) -> None:
        """Blocking write of the custom-path JSON file - call via hass.async_add_executor_job.

        Writes to a temp file and renames over the target so a crash mid-write can't leave a
        truncated/corrupt history file behind - the same reason HA's own Store offers
        atomic_writes.
        """
        assert self._custom_path is not None
        try:
            self._custom_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._custom_path.with_name(self._custom_path.name + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle)
            tmp_path.replace(self._custom_path)
        except OSError:
            _LOGGER.exception(
                "K93 ANS failed writing notification history to %s", self._custom_path
            )

    async def async_load(self) -> None:
        """Load persisted notifications from disk."""
        if self._custom_path is not None:
            data = await self._hass.async_add_executor_job(self._read_custom_file)
        else:
            data = await self._store.async_load()
        self._notifications = (data or {}).get("notifications", [])

    async def async_save(self) -> None:
        """Persist notifications to disk."""
        data = {"notifications": self._notifications}
        if self._custom_path is not None:
            await self._hass.async_add_executor_job(self._write_custom_file, data)
        else:
            await self._store.async_save(data)

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
        live_id = record.get("live_id")
        if live_id and self._pending_live.get(live_id, (None, None))[0] == record["id"]:
            del self._pending_live[live_id]
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

    def async_resolve_live_notification(self, live_id: str) -> dict[str, str] | None:
        """Return the {id, created} to reuse for this live_id, or None for a genuinely new session.

        Checks an already-stored active record first, then falls back to a reservation made by a
        send_notification call that's still in flight through the event pipeline (see
        async_reserve_live_id). Without that second check, two send_notification calls for the
        same live_id in quick succession - e.g. a burst of trigger firings right after an HA
        restart - could both see "nothing stored yet" and each mint their own id: two distinct
        notifications/phone pushes for what should be one live session, with only the second ever
        receiving further updates (the first is silently orphaned).
        """
        existing = self.async_get_by_live_id(live_id)
        if existing:
            return {"id": existing["id"], "created": existing["created"]}
        pending = self._pending_live.get(live_id)
        if pending:
            return {"id": pending[0], "created": pending[1]}
        return None

    def async_reserve_live_id(self, live_id: str, notification_id: str, created: str) -> None:
        """Record that `notification_id` is the id in use for `live_id`, before it's stored.

        Cleared automatically once that id is actually stored (see async_add).
        """
        self._pending_live[live_id] = (notification_id, created)

    def async_list(
        self,
        include_acknowledged: bool = True,
        limit: int | None = None,
        channels: list[str] | None = None,
        channel_mode: str = "include",
        min_importance: str | None = None,
    ) -> list[NotificationRecord]:
        """Return notifications, newest first.

        channels/channel_mode/min_importance let a caller (the card's History/ticker fetch) ask
        for the most recent `limit` records that already match a filter, instead of the most
        recent `limit` records overall filtered afterwards - the latter lets a handful of noisy
        channels crowd a rarer one out of the fetch window entirely, even though it has plenty of
        history further back. Compared case-insensitively since a channel key is always
        lowercased when saved (see config_flow.py) but older stored records may not be (fixed
        going forward in services.py).
        """
        records = self._notifications
        if not include_acknowledged:
            records = [r for r in records if not r["acknowledged"]]
        if channels:
            normalized = {c.strip().lower() for c in channels}

            def _record_channels(r: NotificationRecord) -> set[str]:
                return {c.lower() for c in (r.get("channels") or [r["channel"]])}

            if channel_mode == "exclude":
                records = [r for r in records if not (_record_channels(r) & normalized)]
            else:
                records = [r for r in records if _record_channels(r) & normalized]
        if min_importance:
            min_rank = _importance_rank(min_importance)
            records = [r for r in records if _importance_rank(r["importance"]) >= min_rank]
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

    async def async_delete(self, notification_ids: list[str]) -> list[str]:
        """Delete notifications by id. Returns the ids that actually existed and were removed."""
        id_set = set(notification_ids)
        deleted = [r["id"] for r in self._notifications if r["id"] in id_set]
        if deleted:
            self._notifications = [r for r in self._notifications if r["id"] not in id_set]
            await self.async_save()
        return deleted

    def async_history_ids(self, include_unacknowledged: bool = False) -> list[str]:
        """Return ids eligible for a "clear history" action.

        By default only ids of notifications that aren't still actively pending acknowledgement -
        the same (not requires_ack) or acknowledged rule the History section itself uses - so
        clearing history never silently makes a still-outstanding notification disappear.
        Pass include_unacknowledged=True to include everything instead.
        """
        if include_unacknowledged:
            return [r["id"] for r in self._notifications]
        return [r["id"] for r in self._notifications if not r["requires_ack"] or r["acknowledged"]]

    async def async_prune(self, max_records: int, retention_days: int) -> None:
        """Drop notifications older than retention_days, then cap to max_records."""
        cutoff = dt_util.utcnow() - timedelta(days=retention_days)
        kept = [r for r in self._notifications if dt_util.parse_datetime(r["created"]) >= cutoff]
        kept = kept[:max_records]
        if len(kept) != len(self._notifications):
            self._notifications = kept
            await self.async_save()
