"""Storage layer for K93 ANS notification history (SQLite-backed)."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CUSTOM_STORAGE_FILENAME,
    DEFAULT_STORAGE_DIR_NAME,
    IMPORTANCE_LEVELS,
    LEGACY_JSON_FILENAME,
    LEGACY_JSON_FILENAME_ALT,
    LEGACY_STORAGE_KEY,
)
from .models import NotificationRecord

_LOGGER = logging.getLogger(__name__)

_LEGACY_JSON_FILENAMES = (LEGACY_JSON_FILENAME, LEGACY_JSON_FILENAME_ALT)


def _importance_rank(level: str) -> int:
    try:
        return IMPORTANCE_LEVELS.index(level)
    except ValueError:
        return IMPORTANCE_LEVELS.index("normal")


def _resolve_storage_dir(hass: HomeAssistant, storage_path: str | None) -> Path:
    """The effective storage directory for `storage_path` (blank = the dedicated default).

    Falls back to the default directory if the configured one already exists as a *file* rather
    than a directory (e.g. storage_path was set to the old .storage/k93_ans_notifications path
    itself, from before this was a folder) - trying to mkdir over an existing file would raise and
    leave the store unable to load or save anything, silently "losing" all history until fixed.
    """
    default_dir = Path(hass.config.path(DEFAULT_STORAGE_DIR_NAME))
    if not storage_path:
        return default_dir

    directory = Path(storage_path)
    if not directory.is_absolute():
        directory = Path(hass.config.path(storage_path))

    if directory.exists() and not directory.is_dir():
        _LOGGER.error(
            "K93 ANS: storage_path '%s' resolves to %s, which already exists as a file (not a "
            "directory) - falling back to %s. Change storage_path under Advanced to a plain "
            "folder path.",
            storage_path,
            directory,
            default_dir,
        )
        return default_dir

    return directory


def _migrate_storage_dir(hass: HomeAssistant, effective_dir: Path) -> None:
    """Move the previous default folder's contents into effective_dir if storage_path was just
    pointed somewhere else and effective_dir doesn't have its own database yet.

    This only handles the "storage_path changed" case - relocating an entire folder's worth of
    files (not just history) from the default dedicated folder to a newly-chosen custom one.
    Moving directly between two different custom paths isn't migrated automatically - there's no
    reliable way to know what the previous custom path even was.

    Recovering the pre-dedicated-folder, pre-SQLite `.storage/k93_ans_notifications` file (from
    HA's own Store helper) is handled separately by _import_legacy_json, not here - deliberately
    not gated on whether database.db already exists, since an earlier version of this migration
    logic could leave a *blank* database.db behind (e.g. it failed to unwrap Store's wrapper
    format and imported zero records) which would otherwise permanently block recovery on every
    later load. See _import_legacy_json's docstring.
    """
    db_file = effective_dir / CUSTOM_STORAGE_FILENAME
    if db_file.exists() or any((effective_dir / name).exists() for name in _LEGACY_JSON_FILENAMES):
        return

    default_dir = Path(hass.config.path(DEFAULT_STORAGE_DIR_NAME))
    if effective_dir == default_dir:
        return
    default_db = default_dir / CUSTOM_STORAGE_FILENAME
    default_has_json = any((default_dir / name).exists() for name in _LEGACY_JSON_FILENAMES)
    if not (default_db.exists() or default_has_json):
        return

    try:
        effective_dir.mkdir(parents=True, exist_ok=True)
        for item in default_dir.iterdir():
            item.rename(effective_dir / item.name)
        try:
            default_dir.rmdir()
        except OSError:
            pass
        _LOGGER.warning(
            "K93 ANS migrated notification history from %s to %s", default_dir, effective_dir
        )
    except OSError:
        _LOGGER.exception("K93 ANS failed migrating notification history to %s", effective_dir)


def _extract_notifications(data: Any) -> list[NotificationRecord]:
    """Pull the notification list out of a legacy JSON file's parsed content.

    Two shapes are recognized: this integration's own flat format, `{"notifications": [...]}`
    (used by both legacy JSON filenames), and homeassistant.helpers.storage.Store's own wrapper,
    `{"version": ..., "key": ..., "data": {"notifications": [...]}}` - the very first version of
    K93 ANS stored history via that helper, so `.storage/k93_ans_notifications` still has this
    wrapper around it, not the flat shape.
    """
    if not isinstance(data, dict):
        return []
    if "notifications" in data:
        return data.get("notifications") or []
    inner = data.get("data")
    if isinstance(inner, dict):
        return inner.get("notifications") or []
    return []


def _candidate_legacy_paths(
    hass: HomeAssistant, effective_dir: Path, *, include_migrated_backups: bool
) -> list[Path]:
    """Every location plain-JSON (or the original Store-wrapped file) history has ever lived at,
    in the order they should be tried.

    Deliberately *not* gated on any "has this already been relocated/renamed" bookkeeping - only
    on whether the file itself still exists under a not-yet-".migrated" name (which is the actual
    signal that it hasn't been successfully consumed yet). This integration has been through
    several storage-format changes (raw Store file -> flat JSON in a dedicated folder, under two
    different filenames -> SQLite), so a real instance can have leftover files in almost any
    combination.

    include_migrated_backups additionally re-checks the already-renamed "<name>.migrated" copies -
    a past version of this migration could rename a source away without actually importing
    anything usable from it (e.g. it didn't unwrap Store's own wrapper format), which would
    otherwise strand that history forever. The caller only passes this when the notifications
    table is still completely empty, so a record deliberately deleted by the user can't keep
    reappearing on every restart just because its old backup file is still sitting there.
    """
    dirs = [effective_dir]
    default_dir = Path(hass.config.path(DEFAULT_STORAGE_DIR_NAME))
    if default_dir != effective_dir:
        dirs.append(default_dir)

    paths: list[Path] = []
    for directory in dirs:
        for name in _LEGACY_JSON_FILENAMES:
            paths.append(directory / name)
            if include_migrated_backups:
                paths.append(directory / f"{name}.migrated")
    ancient_file = Path(hass.config.path(".storage", LEGACY_STORAGE_KEY))
    paths.append(ancient_file)
    if include_migrated_backups:
        paths.append(ancient_file.with_name(ancient_file.name + ".migrated"))
    return paths


def _import_legacy_json(
    hass: HomeAssistant, effective_dir: Path, *, include_migrated_backups: bool = True
) -> list[NotificationRecord]:
    """Recover pre-SQLite history from every known legacy location that actually has it.

    Checks every filename/location this history has ever lived under - see
    _candidate_legacy_paths - and accumulates records from *all* of them that yield at least one
    (not just the first hit), deduped by id, since more than one can genuinely hold different real
    history at once (this integration has changed storage format several times over its life). A
    candidate that exists but is empty, unreadable, or in an unrecognized shape is skipped rather
    than treated as final, so it can't hide real history sitting at another candidate.

    Renames whichever file(s) it successfully imports from to "<name>.migrated" in place
    afterward, kept as a backup rather than deleted - re-reading/rewriting that whole file on
    every event was the exact bottleneck this migration exists to get away from for ongoing use,
    but there's no reason to touch it more than this one time.
    """
    collected: dict[str, NotificationRecord] = {}
    for legacy_path in _candidate_legacy_paths(
        hass, effective_dir, include_migrated_backups=include_migrated_backups
    ):
        if not legacy_path.exists():
            continue

        try:
            with legacy_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            records = _extract_notifications(data)
        except (OSError, ValueError):
            _LOGGER.exception("K93 ANS failed reading legacy history from %s", legacy_path)
            continue
        if not records:
            continue

        for record in records:
            collected.setdefault(record["id"], record)

        if not legacy_path.name.endswith(".migrated"):
            try:
                legacy_path.rename(legacy_path.with_name(legacy_path.name + ".migrated"))
            except OSError:
                _LOGGER.exception(
                    "K93 ANS failed renaming legacy history file %s after migrating it",
                    legacy_path,
                )

        _LOGGER.warning(
            "K93 ANS migrated %d notification(s) from %s into the new SQLite database",
            len(records),
            legacy_path,
        )

    return list(collected.values())


class NotificationStore:
    """SQLite-backed notification history, at "<storage directory>/database.db".

    The full history is also kept in memory (self._notifications, newest first) - every read
    method (async_get, async_list, etc.) is synchronous and reads straight from that list, exactly
    like before this was SQLite-backed, so none of the many callers throughout the integration
    (dispatch.py, services.py, sensor.py, websocket_api.py - several of which call these
    synchronously, without `await`) needed to change. What changed is persistence: each write
    (add/acknowledge/delete/prune) now does a small, targeted SQLite statement instead of
    rewriting the *entire* history to disk on every single notification event - the previous
    JSON-file design did a full rewrite every time, which scales badly once history grows into the
    tens of thousands of records (see the README's Storage & history section).

    The storage directory defaults to a dedicated folder in the HA config directory (kept apart
    from HA's own `.storage/` and every other integration's data, in case K93 ANS ever needs more
    than just this one file down the line) - overridable via the `storage_path` option, which
    only takes effect on the next integration setup (see __init__.py's options-change reload
    listener - saving Advanced settings already triggers that automatically). Whichever directory
    turns out to be effective, existing history is migrated into it automatically - see
    _migrate_storage_dir and _import_legacy_json.
    """

    def __init__(self, hass: HomeAssistant, storage_path: str | None = None) -> None:
        self._hass = hass
        self._dir = _resolve_storage_dir(hass, storage_path)
        self._db_path = self._dir / CUSTOM_STORAGE_FILENAME
        self._notifications: list[NotificationRecord] = []
        self._pending_live: dict[str, tuple[str, str]] = {}


    def _connect(self) -> sqlite3.Connection:
        """A fresh connection per call, not a shared one - sqlite3 connections aren't safe to use
        across threads, and each of these calls runs in a different executor thread. Opening one
        is fast; the CREATE-IF-NOT-EXISTS statements are cheap idempotent no-ops once the schema
        already exists.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                live_id TEXT,
                channel TEXT NOT NULL,
                created TEXT NOT NULL,
                acknowledged INTEGER NOT NULL,
                record_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_k93_created ON notifications(created)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_k93_live_id ON notifications(live_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_k93_channel ON notifications(channel)")
        return conn

    def _load_all(self) -> list[NotificationRecord]:
        """Blocking full read, newest first - only called once, at startup (async_load)."""
        _migrate_storage_dir(self._hass, self._dir)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT record_json FROM notifications ORDER BY created DESC"
            ).fetchall()
        finally:
            conn.close()
        records: list[NotificationRecord] = [json.loads(row[0]) for row in rows]

        imported = _import_legacy_json(self._hass, self._dir, include_migrated_backups=not records)
        if imported:
            for record in imported:
                self._upsert(record)
            existing_ids = {record["id"] for record in records}
            records = records + [record for record in imported if record["id"] not in existing_ids]
            records.sort(key=lambda r: r["created"], reverse=True)

        return records

    def _upsert(self, record: NotificationRecord) -> None:
        """Blocking single-row insert-or-replace."""
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO notifications (id, live_id, channel, created, acknowledged, record_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    live_id = excluded.live_id,
                    channel = excluded.channel,
                    created = excluded.created,
                    acknowledged = excluded.acknowledged,
                    record_json = excluded.record_json
                """,
                (
                    record["id"],
                    record.get("live_id"),
                    record["channel"],
                    record["created"],
                    int(bool(record.get("acknowledged"))),
                    json.dumps(record),
                ),
            )
            conn.commit()
        except sqlite3.Error:
            _LOGGER.exception(
                "K93 ANS failed writing notification %s to the database", record.get("id")
            )
        finally:
            conn.close()

    def _delete_ids(self, ids: list[str]) -> None:
        """Blocking batch delete."""
        if not ids:
            return
        conn = self._connect()
        try:
            conn.executemany("DELETE FROM notifications WHERE id = ?", [(i,) for i in ids])
            conn.commit()
        except sqlite3.Error:
            _LOGGER.exception("K93 ANS failed deleting notifications from the database")
        finally:
            conn.close()


    async def async_load(self) -> None:
        """Load persisted notifications from disk."""
        self._notifications = await self._hass.async_add_executor_job(self._load_all)

    def file_size_bytes(self) -> int:
        """Blocking Path.stat() - call via hass.async_add_executor_job (see sensor.py)."""
        try:
            return self._db_path.stat().st_size
        except OSError:
            return 0

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
        await self._hass.async_add_executor_job(self._upsert, record)

    async def async_update_record(self, record: NotificationRecord) -> None:
        """Persist a record already mutated in place by the caller (its position in the
        in-memory list, and the list itself, are untouched - only the on-disk copy is refreshed).
        Used where a record's own dict is mutated directly rather than going through async_add/
        async_acknowledge (see dispatch.py's async_clear_inactive_live_recipients).
        """
        await self._hass.async_add_executor_job(self._upsert, record)

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
        await self._hass.async_add_executor_job(self._upsert, record)
        return record

    async def async_delete(self, notification_ids: list[str]) -> list[str]:
        """Delete notifications by id. Returns the ids that actually existed and were removed."""
        id_set = set(notification_ids)
        deleted = [r["id"] for r in self._notifications if r["id"] in id_set]
        if deleted:
            self._notifications = [r for r in self._notifications if r["id"] not in id_set]
            await self._hass.async_add_executor_job(self._delete_ids, deleted)
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

    async def async_prune(
        self,
        channel_defs: list[dict[str, Any]],
        default_max_records: int,
        default_retention_days: int,
    ) -> None:
        """Drop notifications older than their retention, then cap each channel's own count.

        Both limits default to the global Advanced settings, but a channel can override either
        (see config_flow.py's edit_channel step) - a noisy channel sending 100+ notifications
        every few days can be capped tightly without starving a rarer, more important channel's
        history of its own (larger) budget, and channels are pruned by age independently too, so
        one being cleared out early doesn't need the same short retention forced onto another.
        A record's *primary* channel (record["channel"], first of its "channels" if it has
        several) decides which override applies - the same channel this integration already uses
        for anything else that can only carry one value (Android's own notification channel, the
        card's icon color).
        """
        channel_lookup = {c["key"]: c for c in channel_defs}
        now = dt_util.utcnow()

        def _retention_days(key: str) -> int:
            channel = channel_lookup.get(key)
            override = channel.get("retention_days") if channel else None
            return override if override else default_retention_days

        def _max_records(key: str) -> int:
            channel = channel_lookup.get(key)
            override = channel.get("max_records") if channel else None
            return override if override else default_max_records

        kept_by_age = [
            r
            for r in self._notifications
            if dt_util.parse_datetime(r["created"]) >= now - timedelta(days=_retention_days(r["channel"]))
        ]

        counts: dict[str, int] = {}
        kept: list[NotificationRecord] = []
        for record in kept_by_age:
            key = record["channel"]
            if counts.get(key, 0) >= _max_records(key):
                continue
            counts[key] = counts.get(key, 0) + 1
            kept.append(record)

        if len(kept) != len(self._notifications):
            kept_ids = {r["id"] for r in kept}
            removed_ids = [r["id"] for r in self._notifications if r["id"] not in kept_ids]
            self._notifications = kept
            await self._hass.async_add_executor_job(self._delete_ids, removed_ids)
