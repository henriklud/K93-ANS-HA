"""Services for K93 ANS."""
from __future__ import annotations

import uuid

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    ACK_ACTION_LABELS,
    ACK_ACTION_PREFIX,
    CONF_LANGUAGE,
    DEFAULT_CHANNEL,
    DEFAULT_IMPORTANCE,
    DEFAULT_LANGUAGE,
    DOMAIN,
    EVENT_NOTIFICATION,
    IMPORTANCE_LEVELS,
    MAX_ACTIONS,
)
from .dispatch import async_acknowledge, async_delete_notifications
from .models import NotificationRecord
from .store import NotificationStore

SERVICE_SEND_NOTIFICATION = "send_notification"
SERVICE_ACKNOWLEDGE = "acknowledge"
SERVICE_END_LIVE_NOTIFICATION = "end_live_notification"
SERVICE_DELETE_NOTIFICATION = "delete_notification"
SERVICE_CLEAR_HISTORY = "clear_history"

ACKNOWLEDGE_SCHEMA = vol.Schema({vol.Required("notification_id"): cv.string})
END_LIVE_NOTIFICATION_SCHEMA = vol.Schema({vol.Required("live_id"): cv.string})
DELETE_NOTIFICATION_SCHEMA = vol.Schema({vol.Required("notification_id"): cv.string})
CLEAR_HISTORY_SCHEMA = vol.Schema(
    {vol.Optional("include_unacknowledged", default=False): cv.boolean}
)

ACTION_SCHEMA = vol.Schema(
    {
        vol.Required("action"): cv.string,
        vol.Required("title"): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)

SEND_NOTIFICATION_SCHEMA = vol.Schema(
    {
        vol.Required("title"): cv.string,
        vol.Required("message"): cv.string,
        vol.Optional("icon"): cv.string,
        vol.Optional("image"): cv.string,
        vol.Optional("channel", default=DEFAULT_CHANNEL): cv.string,
        vol.Optional("importance", default=DEFAULT_IMPORTANCE): vol.In(IMPORTANCE_LEVELS),
        vol.Optional("actions", default=list): [ACTION_SCHEMA],
        vol.Optional("persistent", default=False): cv.boolean,
        vol.Optional("data", default=dict): dict,
        vol.Optional("target_recipients"): [cv.string],
        vol.Optional("home_only", default=False): cv.boolean,
        vol.Optional("live_id"): cv.string,
        vol.Optional("source"): cv.string,
    }
)


def _resolve_language(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Resolve which language to use for text baked into outgoing notification payloads."""
    configured = entry.options.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
    if configured and configured != "auto":
        return configured
    hass_language = (hass.config.language or "").lower()
    return "no" if hass_language.startswith(("nb", "nn", "no")) else "en"


def _build_record(
    call: ServiceCall, ack_label: str, existing: NotificationRecord | None
) -> NotificationRecord:
    """Build a notification record from a send_notification service call.

    If `existing` is given (a live notification being refreshed - see live_id), its id and
    original creation time are reused instead of minting a new notification, so the store
    update, the persistent_notification, and the companion-app tag all refer to the same
    underlying notification and get updated in place rather than piling up duplicates.
    """
    notification_id = existing["id"] if existing else str(uuid.uuid4())
    created = existing["created"] if existing else dt_util.utcnow().isoformat()
    actions = list(call.data["actions"])
    channel = call.data["channel"]
    persistent = call.data["persistent"]

    has_ack_action = any(a["action"].startswith(ACK_ACTION_PREFIX) for a in actions)
    if persistent and not has_ack_action:
        actions = actions[: MAX_ACTIONS - 1]
        actions.append(
            {"action": f"{ACK_ACTION_PREFIX}{notification_id}", "title": ack_label}
        )
    actions = actions[:MAX_ACTIONS]

    return {
        "id": notification_id,
        "created": created,
        "updated_at": dt_util.utcnow().isoformat() if existing else None,
        "live_id": call.data.get("live_id"),
        "title": call.data["title"],
        "message": call.data["message"],
        "icon": call.data.get("icon"),
        "image": call.data.get("image"),
        "channel": channel,
        "importance": call.data["importance"],
        "actions": actions,
        "persistent": persistent,
        "data": call.data["data"],
        "recipients": {},
        "requires_ack": persistent,
        "acknowledged": False,
        "acknowledged_at": None,
        "acknowledged_via": None,
        "dismissed": False,
        "source": call.data.get("source"),
        "target_recipients": call.data.get("target_recipients"),
        "home_only": call.data["home_only"],
    }


def async_register_services(hass: HomeAssistant, entry: ConfigEntry, store: NotificationStore) -> None:
    """Register K93 ANS services."""

    async def handle_send_notification(call: ServiceCall) -> None:
        language = _resolve_language(hass, entry)
        ack_label = ACK_ACTION_LABELS.get(language, ACK_ACTION_LABELS["en"])
        live_id = call.data.get("live_id")
        existing = store.async_get_by_live_id(live_id) if live_id else None
        record = _build_record(call, ack_label, existing)
        hass.bus.async_fire(EVENT_NOTIFICATION, record)

    async def handle_acknowledge(call: ServiceCall) -> None:
        await async_acknowledge(hass, store, call.data["notification_id"], "service")

    async def handle_end_live_notification(call: ServiceCall) -> None:
        record = store.async_get_by_live_id(call.data["live_id"])
        if record is None:
            return
        await async_acknowledge(hass, store, record["id"], "live_ended")

    async def handle_delete_notification(call: ServiceCall) -> None:
        await async_delete_notifications(hass, store, [call.data["notification_id"]])

    async def handle_clear_history(call: ServiceCall) -> None:
        ids = store.async_history_ids(call.data["include_unacknowledged"])
        await async_delete_notifications(hass, store, ids)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_NOTIFICATION,
        handle_send_notification,
        schema=SEND_NOTIFICATION_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ACKNOWLEDGE,
        handle_acknowledge,
        schema=ACKNOWLEDGE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_END_LIVE_NOTIFICATION,
        handle_end_live_notification,
        schema=END_LIVE_NOTIFICATION_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_NOTIFICATION,
        handle_delete_notification,
        schema=DELETE_NOTIFICATION_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_HISTORY,
        handle_clear_history,
        schema=CLEAR_HISTORY_SCHEMA,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister K93 ANS services."""
    hass.services.async_remove(DOMAIN, SERVICE_SEND_NOTIFICATION)
    hass.services.async_remove(DOMAIN, SERVICE_ACKNOWLEDGE)
    hass.services.async_remove(DOMAIN, SERVICE_END_LIVE_NOTIFICATION)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_NOTIFICATION)
    hass.services.async_remove(DOMAIN, SERVICE_CLEAR_HISTORY)
