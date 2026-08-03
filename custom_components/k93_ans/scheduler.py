"""Cron-based scheduled notifications for K93 ANS."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from croniter import croniter
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .const import CONF_SCHEDULED_NOTIFICATIONS, DEFAULT_CHANNEL
from .services import async_send_notification
from .store import NotificationStore

_LOGGER = logging.getLogger(__name__)


def _notification_data(scheduled: dict[str, Any]) -> dict[str, Any]:
    """Build send_notification-shaped field data for one scheduled notification firing.

    A scheduled notification only exposes the fields useful for a recurring reminder; anything
    else (actions, image, data, live_id, ...) gets the same default SEND_NOTIFICATION_SCHEMA would
    apply for a service call that omitted it, since _build_record expects every key to be present.
    """
    return {
        "title": scheduled["title"],
        "message": scheduled["message"],
        "icon": scheduled.get("icon"),
        "image": None,
        "channel": scheduled.get("channel") or DEFAULT_CHANNEL,
        "importance": scheduled.get("importance", "normal"),
        "actions": [],
        "persistent": scheduled.get("persistent", False),
        "data": {},
        "target_recipients": scheduled.get("target_recipients") or None,
        "home_only": scheduled.get("home_only", False),
        "live_id": None,
        "source": f"scheduled:{scheduled.get('name', scheduled.get('id'))}",
        "show_in_history": True,
        "dismiss_on_action": False,
        "clear_on_acknowledge": True,
    }


def async_setup_scheduled_notifications(
    hass: HomeAssistant, entry: ConfigEntry, store: NotificationStore
) -> Callable[[], None]:
    """Schedule every enabled ScheduledNotification, rescheduling itself after each firing.

    Returns one unsub callable that cancels every still-pending timer - call it on unload/reload.
    Adding, editing, or removing a scheduled notification saves options, which reloads the whole
    integration (see the options-change listener in __init__.py) and tears this down and rebuilds
    it from the latest config - nothing here needs to react to config changes on its own.
    """
    unsubs: list[Callable[[], None]] = []

    def _schedule(scheduled: dict[str, Any]) -> None:
        try:
            next_run = croniter(scheduled["cron"], dt_util.now()).get_next(datetime)
        except Exception:
            _LOGGER.error(
                "K93 ANS: scheduled notification '%s' has an invalid cron expression '%s' - "
                "not scheduling it",
                scheduled.get("name"),
                scheduled.get("cron"),
            )
            return

        async def _fire(_now: datetime) -> None:
            await async_send_notification(hass, entry, store, _notification_data(scheduled))
            _schedule(scheduled)

        unsubs.append(async_track_point_in_time(hass, _fire, dt_util.as_utc(next_run)))

    for scheduled in entry.options.get(CONF_SCHEDULED_NOTIFICATIONS, []):
        if scheduled.get("enabled", True):
            _schedule(scheduled)

    def _unsub_all() -> None:
        for unsub in unsubs:
            unsub()
        unsubs.clear()

    return _unsub_all
