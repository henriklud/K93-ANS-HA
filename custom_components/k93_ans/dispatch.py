"""Dispatch handling for incoming K93 ANS notification events."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.components.persistent_notification import (
    SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED,
    UpdateType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_HOME
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    ANDROID_IMPORTANCE_MAP,
    CONF_CHANNELS,
    CONF_RECIPIENTS,
    DEFAULT_CHANNEL,
    IMPORTANCE_LEVELS,
    IOS_INTERRUPTION_MAP,
    SIGNAL_DELETED,
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


def _recipient_targeted(recipient: dict[str, Any], target_recipients: list[str]) -> bool:
    """Whether a recipient is included in a target_recipients list.

    Matched by id (the stable value automations should use) or by the recipient's configured
    name (case-insensitive) - the id is an opaque uuid a human can't easily look up, so the name
    gives a usable value for anyone picking recipients by hand (e.g. in Developer Tools).
    """
    targets = {t.strip().lower() for t in target_recipients if t and t.strip()}
    return (
        recipient["id"].lower() in targets
        or recipient.get("name", "").strip().lower() in targets
    )


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


def _resolve_image(record: NotificationRecord) -> str | None:
    """The image (URL or HA-relative path, e.g. /local/...) to attach, if any.

    Prefers the explicit `image` field. Falls back to a non-mdi `icon` for backward
    compatibility with the earlier behavior where `icon` alone doubled as the picture.
    """
    image = record.get("image")
    if image:
        return image
    icon = record.get("icon")
    if icon and not icon.startswith("mdi:"):
        return icon
    return None


def _create_persistent_notification(hass: HomeAssistant, record: NotificationRecord) -> None:
    """Create (or, reusing the same notification_id, update in place) the HA persistent
    notification for a record, embedding its image as markdown if it has one since
    persistent_notification has no dedicated image field."""
    message = record["message"]
    image = _resolve_image(record)
    if image:
        message = f"{message}\n\n![]({image})"
    persistent_notification.async_create(
        hass, message, title=record["title"], notification_id=record["id"]
    )


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
        if not record.get("dismiss_on_action"):
            data["sticky"] = True
    if record.get("persistent"):
        data["sticky"] = True
        data["persistent"] = True

    icon = record.get("icon")
    if icon and icon.startswith("mdi:"):
        data["notification_icon"] = icon

    image = _resolve_image(record)
    if image:
        data["image"] = image

    data.update(record.get("data") or {})

    return {"title": record["title"], "message": record["message"], "data": data}


async def async_handle_notification_event(
    hass: HomeAssistant, entry: ConfigEntry, store: NotificationStore, event: Event
) -> None:
    """Handle a k93_ans_notification event.

    Resolves the channel, filters configured recipients by importance/channel,
    dispatches to matching notify.* targets, creates a persistent_notification
    when required, and persists the resulting record to history.
    """
    record: NotificationRecord = dict(event.data)

    previous = store.async_get(record["id"])
    previous_deliveries: dict[str, Any] = (previous or {}).get("recipients") or {}

    channel_defs = {c["key"]: c for c in entry.options.get(CONF_CHANNELS, [])}
    recipients = entry.options.get(CONF_RECIPIENTS, [])

    record_channel_keys = record.get("channels") or [record["channel"]]
    resolved_channels = []
    for key in record_channel_keys:
        channel = channel_defs.get(key)
        if channel is None:
            if key != DEFAULT_CHANNEL:
                _LOGGER.warning("K93 ANS: unknown channel '%s', falling back to default", key)
            channel = _FALLBACK_CHANNEL
        resolved_channels.append(channel)
    primary_channel = resolved_channels[0]

    record_rank = _importance_rank(record["importance"])
    target_recipients = record.get("target_recipients")

    deliveries: dict[str, Any] = {}
    for recipient in recipients:
        if not recipient.get("enabled", True):
            continue
        if target_recipients and not _recipient_targeted(recipient, target_recipients):
            continue

        recipient_rank = _importance_rank(recipient["min_importance"])
        allowed_channels = recipient.get("allowed_channels") or []
        present_ok = not record.get("home_only") or _is_home(hass, recipient)
        matched = present_ok and any(
            channel.get("enabled", True)
            and (not allowed_channels or channel["key"] in allowed_channels)
            and record_rank >= max(recipient_rank, _importance_rank(channel["min_importance"]))
            for channel in resolved_channels
        )

        prior = previous_deliveries.get(recipient["id"])
        delivery: dict[str, Any] = {
            "notify_service": recipient["notify_service"],
            "matched": matched,
            "dispatched": bool(prior and prior.get("dispatched")),
            "dispatch_error": None,
            "dispatched_at": (prior or {}).get("dispatched_at"),
        }

        if matched:
            if _is_mobile_app_target(recipient["notify_service"]):
                payload = _build_mobile_app_payload(record, primary_channel["name"])
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
            except Exception as err:
                delivery["dispatch_error"] = str(err)
                _LOGGER.exception(
                    "K93 ANS failed delivering to notify.%s", recipient["notify_service"]
                )

        deliveries[recipient["id"]] = delivery

    for recipient_id, prior_delivery in previous_deliveries.items():
        deliveries.setdefault(recipient_id, prior_delivery)

    record["recipients"] = deliveries

    if record.get("persistent"):
        _create_persistent_notification(hass, record)

    await store.async_add(record)
    async_dispatcher_send(hass, SIGNAL_UPDATED, record)


def async_restore_persistent_notifications(hass: HomeAssistant, store: NotificationStore) -> None:
    """Recreate persistent_notifications for still-unacknowledged records after a restart.

    HA's built-in persistent_notification system only lives in memory, so restarting HA clears
    the bell entirely even though our own Store-backed history survives fine. Call this once at
    setup, after the store has loaded, so anything still outstanding reappears in the bell using
    the same notification_id it already had - it's exactly the same "create" call dispatch uses,
    just replayed from history instead of triggered by a fresh event. Deliberately does NOT
    re-dispatch to notify.* recipients: unlike persistent_notification, a companion app's pushed
    notification isn't cleared by an HA restart, so re-sending it would just be a noisy duplicate.
    """
    for record in store.async_list():
        if record.get("persistent") and not record.get("acknowledged"):
            _create_persistent_notification(hass, record)


async def _clear_mobile_notifications(hass: HomeAssistant, record: NotificationRecord) -> None:
    """Clear the pushed notification on every companion-app recipient that received it.

    The companion apps recognize a notify call with message "clear_notification" plus a
    matching "tag" as a command to remove that specific notification, rather than show a new
    one - this is how an in-app/card acknowledgement also dismisses the phone's push banner.
    """
    recipients = record.get("recipients") or {}
    _LOGGER.debug(
        "K93 ANS clearing pushed notifications for %s: recipients=%s",
        record["id"],
        recipients,
    )
    for delivery in recipients.values():
        notify_service = delivery.get("notify_service")
        if not delivery.get("dispatched") or not notify_service:
            _LOGGER.debug(
                "K93 ANS skip clear for %s: dispatched=%s notify_service=%s",
                record["id"],
                delivery.get("dispatched"),
                notify_service,
            )
            continue
        if not _is_mobile_app_target(notify_service):
            _LOGGER.debug(
                "K93 ANS skip clear for %s: notify.%s is not a mobile_app target",
                record["id"],
                notify_service,
            )
            continue
        for attempt in (1, 2):
            try:
                _LOGGER.debug(
                    "K93 ANS sending clear_notification to notify.%s for %s (attempt %d)",
                    notify_service,
                    record["id"],
                    attempt,
                )
                await hass.services.async_call(
                    "notify",
                    notify_service,
                    {"message": "clear_notification", "data": {"tag": record["id"]}},
                    blocking=True,
                )
                break
            except Exception:
                if attempt == 1:
                    _LOGGER.warning(
                        "K93 ANS clear_notification to notify.%s failed, retrying once",
                        notify_service,
                    )
                    await asyncio.sleep(2)
                else:
                    _LOGGER.exception(
                        "K93 ANS failed clearing pushed notification on notify.%s",
                        notify_service,
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
    _LOGGER.debug(
        "K93 ANS acknowledge %s via=%s persistent=%s clear_on_acknowledge=%s",
        notification_id,
        via,
        record.get("persistent"),
        record.get("clear_on_acknowledge", True),
    )
    if record.get("persistent"):
        try:
            persistent_notification.async_dismiss(hass, notification_id)
        except Exception:
            _LOGGER.exception(
                "K93 ANS failed dismissing persistent_notification %s", notification_id
            )
    if record.get("clear_on_acknowledge", True):
        await _clear_mobile_notifications(hass, record)
    async_dispatcher_send(hass, SIGNAL_UPDATED, record)
    return record


def async_register_persistent_notification_listener(
    hass: HomeAssistant, store: NotificationStore
) -> Callable[[], None]:
    """Acknowledge our own record when its persistent_notification is dismissed outside k93_ans.

    HA's built-in notification drawer lets a user dismiss a persistent_notification directly -
    that bypasses k93_ans.acknowledge, the card, and the mobile_app_notification_action listener
    entirely, so without this the record would stay unacknowledged forever and the matching phone
    push would never get cleared. Records our own acknowledge flow already dismissed are skipped
    (they're already marked acknowledged by the time that dismiss fires this same signal), so this
    doesn't loop back on itself.
    """

    @callback
    def _on_update(update_type: UpdateType, notifications: dict[str, Any]) -> None:
        if update_type != UpdateType.REMOVED:
            return
        for notification_id in notifications:
            record = store.async_get(notification_id)
            if record is None or not record.get("persistent") or record.get("acknowledged"):
                continue
            hass.async_create_task(async_acknowledge(hass, store, notification_id, "ha_ui"))

    return async_dispatcher_connect(hass, SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED, _on_update)


async def async_delete_notifications(
    hass: HomeAssistant, store: NotificationStore, notification_ids: list[str]
) -> list[str]:
    """Delete notifications from history, cleaning up any still-live bell/push first.

    Used for both a single delete (one id) and "clear history" (many ids). Deleting a
    notification that's still outstanding also dismisses its persistent_notification and
    clears any pushed companion-app notification, the same as acknowledging would - otherwise
    it'd disappear from history while leaving an orphaned bell entry or phone notification with
    nothing behind it. Returns the ids that actually existed and were removed.
    """
    for notification_id in notification_ids:
        record = store.async_get(notification_id)
        if record is None:
            continue
        if record.get("persistent") and not record.get("acknowledged"):
            persistent_notification.async_dismiss(hass, notification_id)
        await _clear_mobile_notifications(hass, record)

    deleted_ids = await store.async_delete(notification_ids)
    if deleted_ids:
        async_dispatcher_send(hass, SIGNAL_DELETED, deleted_ids)
    return deleted_ids