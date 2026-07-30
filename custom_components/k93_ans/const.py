"""Constants for the K93 ANS integration."""
from __future__ import annotations

import uuid

DOMAIN = "k93_ans"

EVENT_NOTIFICATION = "k93_ans_notification"
SIGNAL_UPDATED = "k93_ans_updated"
SIGNAL_DELETED = "k93_ans_deleted"

STORAGE_VERSION = 1
STORAGE_KEY = "k93_ans_notifications"

DEFAULT_CHANNEL = "default"

IMPORTANCE_LEVELS = ["low", "normal", "high", "critical"]
DEFAULT_IMPORTANCE = "normal"

BUILTIN_CHANNELS = [
    {"key": "info", "name": "Info", "min_importance": "low"},
    {"key": "alert", "name": "Alert", "min_importance": "high"},
    {"key": "event", "name": "Event", "min_importance": "normal"},
    {"key": "reminder", "name": "Reminder", "min_importance": "normal"},
    {"key": "security", "name": "Security", "min_importance": "high"},
    {"key": "system", "name": "System", "min_importance": "normal"},
]

ANDROID_IMPORTANCE_MAP = {"low": "low", "normal": "default", "high": "high", "critical": "max"}
IOS_INTERRUPTION_MAP = {
    "low": "passive",
    "normal": "active",
    "high": "time-sensitive",
    "critical": "critical",
}

ACK_ACTION_PREFIX = "K93_ACK__"
MAX_ACTIONS = 3

CONF_RECIPIENTS = "recipients"
CONF_CHANNELS = "channels"
CONF_HISTORY_RETENTION_DAYS = "history_retention_days"
CONF_HISTORY_MAX_RECORDS = "history_max_records"
CONF_LANGUAGE = "language"

DEFAULT_HISTORY_RETENTION_DAYS = 90
DEFAULT_HISTORY_MAX_RECORDS = 500
DEFAULT_LANGUAGE = "auto"

SUPPORTED_LANGUAGES = ["en", "no"]
ACK_ACTION_LABELS = {"en": "Acknowledge", "no": "Bekreft"}


def default_options() -> dict:
    """Return a fresh copy of the default options structure, seeded with the built-in channels."""
    return {
        CONF_RECIPIENTS: [],
        CONF_CHANNELS: [
            {
                "id": str(uuid.uuid4()),
                "key": channel["key"],
                "name": channel["name"],
                "min_importance": channel["min_importance"],
                "enabled": True,
            }
            for channel in BUILTIN_CHANNELS
        ],
        CONF_HISTORY_RETENTION_DAYS: DEFAULT_HISTORY_RETENTION_DAYS,
        CONF_HISTORY_MAX_RECORDS: DEFAULT_HISTORY_MAX_RECORDS,
        CONF_LANGUAGE: DEFAULT_LANGUAGE,
    }
