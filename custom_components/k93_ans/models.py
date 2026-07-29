"""Data shapes used by K93 ANS."""
from __future__ import annotations

from typing import Any, TypedDict


class RecipientDelivery(TypedDict):
    """Per-recipient delivery outcome for a notification."""

    notify_service: str
    matched: bool
    dispatched: bool
    dispatch_error: str | None
    dispatched_at: str | None


class NotificationRecord(TypedDict):
    """A single notification, as stored in the history."""

    id: str
    created: str
    title: str
    message: str
    icon: str | None
    channel: str
    importance: str
    actions: list[dict[str, str]]
    persistent: bool
    data: dict[str, Any]
    recipients: dict[str, RecipientDelivery]
    requires_ack: bool
    acknowledged: bool
    acknowledged_at: str | None
    acknowledged_via: str | None
    dismissed: bool
    source: str | None
    target_recipients: list[str] | None
    home_only: bool


class Recipient(TypedDict):
    """A configured notification recipient."""

    id: str
    name: str
    notify_service: str
    min_importance: str
    allowed_channels: list[str]
    enabled: bool
    person_entity_id: str | None


class Channel(TypedDict):
    """A configured notification channel."""

    id: str
    key: str
    name: str
    min_importance: str
    enabled: bool
