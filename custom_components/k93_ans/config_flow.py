"""Config and options flow for K93 ANS."""
from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CHANNELS,
    CONF_HISTORY_MAX_RECORDS,
    CONF_HISTORY_RETENTION_DAYS,
    CONF_LANGUAGE,
    CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES,
    CONF_RECIPIENTS,
    DOMAIN,
    IMPORTANCE_LEVELS,
    default_options,
)

ADD_NEW = "__add_new__"
CHANNEL_IMPORTANCE_FIELD_PREFIX = "importance_for_"


class K93AnsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for K93 ANS. Singleton - only one entry allowed."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="K93 ANS", data={}, options=default_options())

        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> K93AnsOptionsFlow:
        """Get the options flow for this handler."""
        return K93AnsOptionsFlow()


class K93AnsOptionsFlow(config_entries.OptionsFlow):
    """Handle recipients/channels/advanced configuration."""

    def __init__(self) -> None:
        self._options: dict[str, Any] | None = None
        self._editing_id: str | None = None

    def _ensure_options(self) -> dict[str, Any]:
        if self._options is None:
            self._options = {**default_options(), **dict(self.config_entry.options)}
        return self._options

    async def _async_save(self) -> None:
        self.hass.config_entries.async_update_entry(
            self.config_entry, options=self._ensure_options()
        )


    async def async_step_init(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Show the main options menu."""
        self._ensure_options()
        return self.async_show_menu(
            step_id="init",
            menu_options=["manage_recipients", "manage_channels", "advanced", "finish"],
        )

    async def async_step_finish(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Finish the options flow."""
        return self.async_create_entry(title="", data=self._ensure_options())


    async def async_step_manage_recipients(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick an existing recipient to edit, or add a new one."""
        options = self._ensure_options()
        recipients = options[CONF_RECIPIENTS]

        if user_input is not None:
            self._editing_id = None if user_input["recipient"] == ADD_NEW else user_input["recipient"]
            return await self.async_step_edit_recipient()

        if not recipients:
            self._editing_id = None
            return await self.async_step_edit_recipient()

        choices = [{"value": r["id"], "label": r["name"]} for r in recipients]
        choices.append({"value": ADD_NEW, "label": "Add new recipient"})

        return self.async_show_form(
            step_id="manage_recipients",
            data_schema=vol.Schema(
                {
                    vol.Required("recipient"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=choices, mode="dropdown")
                    )
                }
            ),
        )

    async def async_step_edit_recipient(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Add, edit or remove a single recipient."""
        options = self._ensure_options()
        recipients = options[CONF_RECIPIENTS]
        existing = next((r for r in recipients if r["id"] == self._editing_id), None)

        if user_input is not None:
            if user_input.get("remove") and existing is not None:
                options[CONF_RECIPIENTS] = [r for r in recipients if r["id"] != existing["id"]]
            else:
                channel_importance = {
                    key[len(CHANNEL_IMPORTANCE_FIELD_PREFIX) :]: value
                    for key, value in user_input.items()
                    if key.startswith(CHANNEL_IMPORTANCE_FIELD_PREFIX) and value
                }
                recipient = {
                    "id": existing["id"] if existing else str(uuid.uuid4()),
                    "name": user_input["name"],
                    "notify_service": user_input["notify_service"],
                    "person_entity_id": user_input.get("person_entity_id") or None,
                    "interactive_entity_id": user_input.get("interactive_entity_id") or None,
                    "min_importance": user_input["min_importance"],
                    "allowed_channels": user_input.get("allowed_channels", []),
                    "channel_importance": channel_importance,
                    "enabled": user_input["enabled"],
                }
                if existing:
                    options[CONF_RECIPIENTS] = [
                        recipient if r["id"] == existing["id"] else r for r in recipients
                    ]
                else:
                    options[CONF_RECIPIENTS] = [*recipients, recipient]

            await self._async_save()
            return await self.async_step_init()

        notify_services = sorted(self.hass.services.async_services().get("notify", {}).keys())
        channel_choices = [c["key"] for c in options[CONF_CHANNELS]]

        notify_service_key = (
            vol.Required("notify_service", default=existing["notify_service"])
            if existing
            else vol.Required("notify_service")
        )

        schema_dict: dict[Any, Any] = {
            vol.Required("name", default=existing["name"] if existing else ""): selector.TextSelector(),
            notify_service_key: selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=notify_services, custom_value=True, mode="dropdown"
                )
            ),
            vol.Optional(
                "person_entity_id",
                default=(existing.get("person_entity_id") if existing else None),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="person")),
            vol.Optional(
                "interactive_entity_id",
                default=(existing.get("interactive_entity_id") if existing else None),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor")),
            vol.Optional(
                "min_importance",
                default=existing["min_importance"] if existing else "normal",
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=IMPORTANCE_LEVELS)
            ),
            vol.Optional(
                "allowed_channels",
                default=existing["allowed_channels"] if existing else [],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=channel_choices, multiple=True, custom_value=True
                )
            ),
            vol.Optional(
                "enabled", default=existing["enabled"] if existing else True
            ): selector.BooleanSelector(),
        }

        channel_importance = existing.get("channel_importance", {}) if existing else {}
        importance_override_options = [{"value": "", "label": "(use recipient default)"}] + [
            {"value": level, "label": level} for level in IMPORTANCE_LEVELS
        ]
        for channel_key in (existing.get("allowed_channels", []) if existing else []):
            schema_dict[
                vol.Optional(
                    f"{CHANNEL_IMPORTANCE_FIELD_PREFIX}{channel_key}",
                    default=channel_importance.get(channel_key, ""),
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=importance_override_options)
            )

        if existing:
            schema_dict[vol.Optional("remove", default=False)] = selector.BooleanSelector()

        schema = vol.Schema(schema_dict)

        return self.async_show_form(step_id="edit_recipient", data_schema=schema)


    async def async_step_manage_channels(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick an existing channel to edit, or add a new one."""
        options = self._ensure_options()
        channels = options[CONF_CHANNELS]

        if user_input is not None:
            self._editing_id = None if user_input["channel"] == ADD_NEW else user_input["channel"]
            return await self.async_step_edit_channel()

        if not channels:
            self._editing_id = None
            return await self.async_step_edit_channel()

        choices = [{"value": c["id"], "label": c["name"]} for c in channels]
        choices.append({"value": ADD_NEW, "label": "Add new channel"})

        return self.async_show_form(
            step_id="manage_channels",
            data_schema=vol.Schema(
                {
                    vol.Required("channel"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=choices, mode="dropdown")
                    )
                }
            ),
        )

    async def async_step_edit_channel(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Add, edit or remove a single channel."""
        options = self._ensure_options()
        channels = options[CONF_CHANNELS]
        existing = next((c for c in channels if c["id"] == self._editing_id), None)

        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get("remove") and existing is not None:
                options[CONF_CHANNELS] = [c for c in channels if c["id"] != existing["id"]]
                await self._async_save()
                return await self.async_step_init()

            key = user_input["key"].strip().lower().replace(" ", "_")
            duplicate = any(
                c["key"] == key and (existing is None or c["id"] != existing["id"])
                for c in channels
            )
            if duplicate:
                errors["key"] = "duplicate_key"
            else:
                channel = {
                    "id": existing["id"] if existing else str(uuid.uuid4()),
                    "key": key,
                    "name": user_input["name"],
                    "min_importance": user_input["min_importance"],
                    "enabled": user_input["enabled"],
                    "color": user_input.get("color") or None,
                }
                if existing:
                    options[CONF_CHANNELS] = [
                        channel if c["id"] == existing["id"] else c for c in channels
                    ]
                else:
                    options[CONF_CHANNELS] = [*channels, channel]

                await self._async_save()
                return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Required("key", default=existing["key"] if existing else ""): selector.TextSelector(),
                vol.Required("name", default=existing["name"] if existing else ""): selector.TextSelector(),
                vol.Optional(
                    "min_importance",
                    default=existing["min_importance"] if existing else "low",
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=IMPORTANCE_LEVELS)
                ),
                vol.Optional(
                    "enabled", default=existing["enabled"] if existing else True
                ): selector.BooleanSelector(),
                vol.Optional(
                    "color", default=(existing.get("color") if existing else None) or ""
                ): selector.TextSelector(),
            }
        )
        if existing:
            schema = schema.extend(
                {vol.Optional("remove", default=False): selector.BooleanSelector()}
            )

        return self.async_show_form(step_id="edit_channel", data_schema=schema, errors=errors)


    async def async_step_advanced(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Configure history retention and notification-payload language."""
        options = self._ensure_options()

        if user_input is not None:
            options[CONF_HISTORY_RETENTION_DAYS] = user_input[CONF_HISTORY_RETENTION_DAYS]
            options[CONF_HISTORY_MAX_RECORDS] = user_input[CONF_HISTORY_MAX_RECORDS]
            options[CONF_LANGUAGE] = user_input[CONF_LANGUAGE]
            options[CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES] = user_input[
                CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES
            ]
            await self._async_save()
            return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_HISTORY_RETENTION_DAYS,
                    default=options[CONF_HISTORY_RETENTION_DAYS],
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, mode="box")
                ),
                vol.Optional(
                    CONF_HISTORY_MAX_RECORDS,
                    default=options[CONF_HISTORY_MAX_RECORDS],
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, mode="box")
                ),
                vol.Optional(
                    CONF_LANGUAGE,
                    default=options[CONF_LANGUAGE],
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "auto", "label": "Automatic (match Home Assistant)"},
                            {"value": "en", "label": "English"},
                            {"value": "no", "label": "Norsk"},
                        ]
                    )
                ),
                vol.Optional(
                    CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES,
                    default=options[CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES],
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, mode="box")
                ),
            }
        )
        return self.async_show_form(step_id="advanced", data_schema=schema)
