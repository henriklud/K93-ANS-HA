"""Dispatch handling for incoming K93 ANS notification events."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_HOME
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    ANDROID_IMPORTANCE_MAP,
    CONF_CHANNELS,
    CONF_RECIPIENTS,
    DEFAULT_CHANNEL,
    IMPORTANCE_LEVELS,
    IOS_INTERRUPTION_MAP,
    SIGNAL_UPDATED,
)
from .models import NotificationRecord
from .store import NotificationStore

_LOGGER = logging.getLogger(__name__)

_FALLBACK_CHANNEL = {
    "key": DEFAULT_CHANNEL,
    "name": DEFAULT_CHANNEL,
    "min_importance": "low",
    "enabled": True,
}


def _importance_rank(level: str) -> int:
    try:
        return IMPORTANCE_LEVELS.index(level)
    except ValueError:
        return IMPORTANCE_LEVELS.index("normal")


def _is_mobile_app_target(notify_service: str) -> bool:
    """Heuristic: companion-app notify services are named mobile_app_<device>."""
    return notify_service.startswith("mobile_app_")


def _is_home(hass: HomeAssistant, recipient: dict[str, Any]) -> bool:
    """Whether a recipient's assigned person is home.

    Recipients with no person entity assigned aren't subject to the home_only filter at all -
    there's nothing to check presence against - so this only returns False for a recipient that
    *has* a person assigned and that person isn't home (or the entity is missing/unavailable).
    """
    person_entity_id = recipient.get("person_entity_id")
    if not person_entity_id:
        return True
    state = hass.states.get(person_entity_id)
    return state is not None and state.state == STATE_HOME


def _build_notify_payload(record: NotificationRecord) -> dict[str, Any]:
    """Build a plain notify.* payload for a generic (non companion-app) target."""
    return {
        "title": record["title"],
        "message": record["message"],
        "data": dict(record.get("data") or {}),
    }


def _build_mobile_app_payload(record: NotificationRecord, channel_name: str) -> dict[str, Any]:
    """Build the notify.* payload for a mobile_app companion-app target."""
    data: dict[str, Any] = {
        "tag": record["id"],
        "channel": channel_name,
        "importance": ANDROID_IMPORTANCE_MAP.get(record["importance"], "default"),
        "push": {
            "interruption-level": IOS_INTERRUPTION_MAP.get(record["importance"], "active")
        },
    }
    if record.get("actions"):
        data["actions"] = record["actions"]
    if record.get("persistent"):
        data["sticky"] = True
        data["persistent"] = True

    icon = record.get("icon")
    if icon:
        if icon.startswith("mdi:"):
            data["notification_icon"] = icon
        else:
            data["image"] = icon

    data.update(record.get("data") or {})  # caller-supplied extras win last

    return {"title": record["title"], "message": record["message"], "data": data}


async def async_handle_notification_event(
    hass: HomeAssistant, entry: ConfigEntry, store: NotificationStore, event: Event
) -> None:
    """Handle a k93_ans_notification event.

    Resolves the channel, filters configured recipients by importance/channel,
    dispatches to matching notify.* targets, creates a persistent_notification
    when required, and persists the resulting record to history.
    """
    record: NotificationRecord = dict(event.data)  # type: ignore[assignment]

    channels = {c["key"]: c for c in entry.options.get(CONF_CHANNELS, [])}
    recipients = entry.options.get(CONF_RECIPIENTS, [])

    channel = channels.get(record["channel"])
    if channel is None:
        if record["channel"] != DEFAULT_CHANNEL:
            _LOGGER.warning(
                "K93 ANS: unknown channel '%s', falling back to default", record["channel"]
            )
        channel = _FALLBACK_CHANNEL

    record_rank = _importance_rank(record["importance"])
    channel_rank = _importance_rank(channel["min_importance"])
    target_recipients = record.get("target_recipients")

    deliveries: dict[str, Any] = {}
    if channel.get("enabled", True):
        for recipient in recipients:
            if not recipient.get("enabled", True):
                continue
            if target_recipients and recipient["id"] not in target_recipients:
                continue

            recipient_rank = _importance_rank(recipient["min_importance"])
            allowed_channels = recipient.get("allowed_channels") or []
            channel_allowed = not allowed_channels or channel["key"] in allowed_channels
            present_ok = not record.get("home_only") or _is_home(hass, recipient)
            matched = (
                record_rank >= max(recipient_rank, channel_rank)
                and channel_allowed
                and present_ok
            )

            delivery: dict[str, Any] = {
                "notify_service": recipient["notify_service"],
                "matched": matched,
                "dispatched": False,
                "dispatch_error": None,
                "dispatched_at": None,
            }

            if matched:
                if _is_mobile_app_target(recipient["notify_service"]):
                    payload = _build_mobile_app_payload(record, channel["name"])
                else:
                    payload = _build_notify_payload(record)
                try:
                    await hass.services.async_call(
                        "notify", recipient["notify_service"], payload, blocking=False
                    )
                    delivery["dispatched"] = True
                    delivery["dispatched_at"] = dt_util.utcnow().isoformat()
                except ServiceNotFound as err:
                    delivery["dispatch_error"] = str(err)
                    _LOGGER.warning(
                        "K93 ANS could not deliver to notify.%s: %s",
                        recipient["notify_service"],
                        err,
                    )
                except Exception as err:  # noqa: BLE001 - a broken recipient must not block others
                    delivery["dispatch_error"] = str(err)
                    _LOGGER.exception(
                        "K93 ANS failed delivering to notify.%s", recipient["notify_service"]
                    )

            deliveries[recipient["id"]] = delivery

    record["recipients"] = deliveries

    if record.get("persistent"):
        persistent_notification.async_create(
            hass, record["message"], title=record["title"], notification_id=record["id"]
        )

    await store.async_add(record)
    async_dispatcher_send(hass, SIGNAL_UPDATED, record)


async def _clear_mobile_notifications(hass: HomeAssistant, record: NotificationRecord) -> None:
    """Clear the pushed notification on every companion-app recipient that received it.

    The companion apps recognize a notify call with message "clear_notification" plus a
    matching "tag" as a command to remove that specific notification, rather than show a new
    one - this is how an in-app/card acknowledgement also dismisses the phone's push banner.
    """
    for delivery in (record.get("recipients") or {}).values():
        notify_service = delivery.get("notify_service")
        if not delivery.get("dispatched") or not notify_service:
            continue
        if not _is_mobile_app_target(notify_service):
            continue
        try:
            await hass.services.async_call(
                "notify",
                notify_service,
                {"message": "clear_notification", "data": {"tag": record["id"]}},
                blocking=False,
            )
        except Exception:  # noqa: BLE001 - clearing one phone must not block the rest
            _LOGGER.exception(
                "K93 ANS failed clearing pushed notification on notify.%s", notify_service
            )


async def async_acknowledge(
    hass: HomeAssistant, store: NotificationStore, notification_id: str, via: str
) -> NotificationRecord | None:
    """Acknowledge a notification, dismiss its persistent_notification, and notify listeners.

    Shared by the k93_ans.acknowledge service and the mobile_app_notification_action
    listener so the card and the phone stay in sync regardless of which side acked.
    """
    record = await store.async_acknowledge(notification_id, via)
    if record is None:
        return None
    if record.get("persistent"):
        persistent_notification.async_dismiss(hass, notification_id)
    await _clear_mobile_notifications(hass, record)
    async_dispatcher_send(hass, SIGNAL_UPDATED, record)
    return record