"""WebSocket API for the K93 ANS Lovelace card."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import SIGNAL_UPDATED
from .dispatch import async_acknowledge
from .store import NotificationStore


def async_register_websocket_api(hass: HomeAssistant, store: NotificationStore) -> None:
    """Register K93 ANS websocket commands."""

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "k93_ans/list",
            vol.Optional("include_acknowledged", default=True): bool,
            vol.Optional("limit"): int,
        }
    )
    @callback
    def handle_list(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        records = store.async_list(
            include_acknowledged=msg["include_acknowledged"], limit=msg.get("limit")
        )
        connection.send_result(msg["id"], {"notifications": records})

    @websocket_api.websocket_command({vol.Required("type"): "k93_ans/subscribe"})
    @callback
    def handle_subscribe(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        @callback
        def forward(record: dict) -> None:
            connection.send_message(
                websocket_api.event_message(msg["id"], {"notification": record})
            )

        connection.subscriptions[msg["id"]] = async_dispatcher_connect(
            hass_, SIGNAL_UPDATED, forward
        )
        connection.send_result(msg["id"])

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "k93_ans/acknowledge",
            vol.Required("notification_id"): str,
        }
    )
    @websocket_api.async_response
    async def handle_acknowledge(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        record = await async_acknowledge(hass_, store, msg["notification_id"], "card")
        if record is None:
            connection.send_error(msg["id"], "not_found", "Notification not found")
            return
        connection.send_result(msg["id"], {"notification": record})

    websocket_api.async_register_command(hass, handle_list)
    websocket_api.async_register_command(hass, handle_subscribe)
    websocket_api.async_register_command(hass, handle_acknowledge)