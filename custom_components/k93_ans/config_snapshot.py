"""Write-only JSON snapshot of the integration's config-entry options, for manual backup/
debugging purposes only - see CONFIG_SNAPSHOT_FILENAME in const.py. Never read back
automatically; if the real config entry is ever lost, this is meant to be eyeballed or manually
copied back into the options flow by hand, not auto-restored."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from homeassistant.core import HomeAssistant

from .const import CONFIG_SNAPSHOT_FILENAME
from .store import NotificationStore

_LOGGER = logging.getLogger(__name__)


def _write_snapshot(path: Path, options: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(options, indent=2, ensure_ascii=False), encoding="utf-8")


async def async_write_config_snapshot(
    hass: HomeAssistant, store: NotificationStore, options: Mapping[str, Any]
) -> None:
    """Best-effort write of `options` to "<storage dir>/config.json", alongside database.db -
    called once at setup and after every options-flow save (see __init__.py/config_flow.py).
    Failures are logged, not raised - a backup snapshot must never block setup or a save.
    """
    path = store.storage_dir / CONFIG_SNAPSHOT_FILENAME
    try:
        await hass.async_add_executor_job(_write_snapshot, path, dict(options))
    except OSError:
        _LOGGER.exception("K93 ANS failed writing config snapshot to %s", path)
