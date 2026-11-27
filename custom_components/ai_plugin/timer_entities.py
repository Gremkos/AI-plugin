"""Mirror HA voice timers onto dashboard-ready timer.* entities.

Exposes up to three stable slots (timer.ai_plugin_1 .. timer.ai_plugin_3)
plus a summary sensor (sensor.ai_plugin_timers) and dashboard services
(ai_plugin.toggle_timer, ai_plugin.cancel_timer). Because the slot entities
use the ``timer.`` domain with ``finishes_at``, Home Assistant's frontend
(Mushroom, Tile, Entity, button-card) renders a live second-by-second
countdown in the browser without polling or recorder spam.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, ServiceCall, callback

from .const import DOMAIN, TIMER_DONE_SENTINEL
from .tools.ha_local.exposure import _TIMER_DATA_KEY

_LOGGER = logging.getLogger(__name__)

TIMER_SLOT_COUNT = 3
TIMER_SUMMARY_ENTITY_ID = "sensor.ai_plugin_timers"


def _format_hms(total_seconds: int | float) -> str:
    """Format seconds as H:MM:SS (Home Assistant timer convention)."""
    secs = max(0, int(round(total_seconds)))
    hours, rem = divmod(secs, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _visible_timers(manager: Any) -> list[Any]:
    """Return plain and AI-Plugin timers, skipping unrelated delayed commands."""
    return [
        t
        for t in list(manager.timers.values())
        if not t.conversation_command
        or t.conversation_command.startswith(TIMER_DONE_SENTINEL)
    ]


def _ensure_manager_listener(hass: HomeAssistant, manager: Any) -> None:
    """Attach a TimerManager event callback once so native expiries sync too."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("_timer_manager_ref") is manager:
        return

    old_unsub = domain_data.pop("_timer_manager_unsub", None)
    if callable(old_unsub):
        try:
            old_unsub()
        except Exception:  # noqa: BLE001
            pass

    domain_data["_timer_manager_ref"] = manager
    add_handler = getattr(manager, "add_handler", None)
    if not callable(add_handler):
        return

    @callback
    def _on_timer_event(*_args: Any) -> None:
        sync_timer_states(hass)

    try:
        domain_data["_timer_manager_unsub"] = add_handler(_on_timer_event)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("AI Plugin: could not attach TimerManager listener", exc_info=True)


def sync_timer_states(hass: HomeAssistant | None) -> None:
    """Publish active HA voice timers to timer.ai_plugin_1..3 and summary sensor."""
    if hass is None:
        return
    domain_data = hass.data.setdefault(DOMAIN, {})
    slots: dict[int, str] = domain_data.setdefault("_timer_slots", {})

    manager = hass.data.get(_TIMER_DATA_KEY)
    visible: list[Any] = []
    if manager is not None:
        _ensure_manager_listener(hass, manager)
        visible = _visible_timers(manager)

    by_id = {t.id: t for t in visible}

    # Drop finished/cancelled timers from their slots.
    for slot_idx in list(slots):
        if slots[slot_idx] not in by_id:
            slots.pop(slot_idx, None)

    # Assign newly started timers to the lowest free slot without shuffling existing ones.
    assigned_ids = set(slots.values())
    for timer in visible:
        if timer.id in assigned_ids:
            continue
        for slot_idx in range(1, TIMER_SLOT_COUNT + 1):
            if slot_idx not in slots:
                slots[slot_idx] = timer.id
                assigned_ids.add(timer.id)
                break

    now_utc = datetime.now(timezone.utc)
    summary_items: list[dict[str, Any]] = []

    for slot_idx in range(1, TIMER_SLOT_COUNT + 1):
        entity_id = f"timer.ai_plugin_{slot_idx}"
        timer_id = slots.get(slot_idx)
        timer = by_id.get(timer_id) if timer_id else None

        if timer is None:
            hass.states.async_set(
                entity_id,
                "idle",
                {
                    "friendly_name": f"Timer {slot_idx}",
                    "icon": "mdi:timer-outline",
                    "duration": "0:00:00",
                    "remaining": "0:00:00",
                    "editable": False,
                    "restore": False,
                    "timer_id": None,
                    "timer_name": "",
                    "slot": slot_idx,
                },
            )
            continue

        left_float = max(0.0, float(getattr(timer, "seconds_left", 0) or 0))
        left_int = int(round(left_float))
        set_for = (
            3600 * int(getattr(timer, "start_hours", 0) or 0)
            + 60 * int(getattr(timer, "start_minutes", 0) or 0)
            + int(getattr(timer, "start_seconds", 0) or 0)
        )
        duration_s = set_for if set_for > 0 else left_int
        raw_name = (getattr(timer, "name", None) or "").strip()
        display_name = raw_name or f"Timer {slot_idx}"
        is_active = bool(getattr(timer, "is_active", False))
        state = "active" if is_active else "paused"

        attrs: dict[str, Any] = {
            "friendly_name": display_name,
            "icon": "mdi:timer-outline" if is_active else "mdi:timer-pause-outline",
            "duration": _format_hms(duration_s),
            "remaining": _format_hms(left_int),
            "editable": False,
            "restore": False,
            "timer_id": timer.id,
            "timer_name": raw_name,
            "slot": slot_idx,
        }
        finishes_at: str | None = None
        if is_active:
            finishes_at = (now_utc + timedelta(seconds=left_float)).isoformat(
                timespec="seconds"
            )
            attrs["finishes_at"] = finishes_at

        hass.states.async_set(entity_id, state, attrs)
        summary_items.append(
            {
                "slot": slot_idx,
                "entity_id": entity_id,
                "timer_id": timer.id,
                "name": display_name,
                "state": state,
                "duration": _format_hms(duration_s),
                "remaining": _format_hms(left_int),
                "seconds_left": left_int,
                "finishes_at": finishes_at,
            }
        )

    hass.states.async_set(
        TIMER_SUMMARY_ENTITY_ID,
        str(len(visible)),
        {
            "friendly_name": "AI Plugin Timer",
            "icon": "mdi:timer-outline",
            "unit_of_measurement": "aktiv",
            "timers": summary_items,
        },
    )


def _resolve_target_timer_id(hass: HomeAssistant, data: dict[str, Any]) -> str | None:
    """Resolve a timer_id from service data (timer_id, entity_id, or slot)."""
    timer_id = data.get("timer_id")
    if isinstance(timer_id, str) and timer_id.strip():
        return timer_id.strip()

    entity_id = data.get("entity_id")
    if isinstance(entity_id, list) and entity_id:
        entity_id = entity_id[0]
    if isinstance(entity_id, str) and entity_id.strip():
        st = hass.states.get(entity_id.strip())
        if st is not None and st.attributes.get("timer_id"):
            return str(st.attributes["timer_id"])

    slot = data.get("slot")
    if slot is not None:
        try:
            slots = hass.data.get(DOMAIN, {}).get("_timer_slots", {})
            return slots.get(int(slot))
        except (TypeError, ValueError):
            return None
    return None


def async_setup_timer_entities(hass: HomeAssistant) -> Callable[[], None]:
    """Register dashboard timer services and keep slot entities in sync."""

    async def _handle_cancel_timer(call: ServiceCall) -> None:
        manager = hass.data.get(_TIMER_DATA_KEY)
        if manager is None:
            return
        timer_id = _resolve_target_timer_id(hass, dict(call.data))
        if not timer_id or timer_id not in manager.timers:
            sync_timer_states(hass)
            return
        try:
            manager.cancel_timer(timer_id)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("AI Plugin: cancel_timer service failed", exc_info=True)
        sync_timer_states(hass)

    async def _handle_toggle_timer(call: ServiceCall) -> None:
        manager = hass.data.get(_TIMER_DATA_KEY)
        if manager is None:
            return
        timer_id = _resolve_target_timer_id(hass, dict(call.data))
        if not timer_id or timer_id not in manager.timers:
            sync_timer_states(hass)
            return
        timer = manager.timers[timer_id]
        try:
            if timer.is_active:
                manager.pause_timer(timer_id)
            else:
                manager.unpause_timer(timer_id)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("AI Plugin: toggle_timer service failed", exc_info=True)
        sync_timer_states(hass)

    if not hass.services.has_service(DOMAIN, "cancel_timer"):
        hass.services.async_register(DOMAIN, "cancel_timer", _handle_cancel_timer)
    if not hass.services.has_service(DOMAIN, "toggle_timer"):
        hass.services.async_register(DOMAIN, "toggle_timer", _handle_toggle_timer)

    sync_timer_states(hass)

    unsub_started: Callable[[], None] | None = None
    if not getattr(hass, "is_running", True):
        @callback
        def _on_started(_event: Any) -> None:
            sync_timer_states(hass)

        unsub_started = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _on_started
        )

    @callback
    def _cleanup() -> None:
        if unsub_started is not None:
            try:
                unsub_started()
            except Exception:  # noqa: BLE001
                pass
        domain_data = hass.data.get(DOMAIN, {})
        unsub_mgr = domain_data.pop("_timer_manager_unsub", None)
        domain_data.pop("_timer_manager_ref", None)
        if callable(unsub_mgr):
            try:
                unsub_mgr()
            except Exception:  # noqa: BLE001
                pass

    return _cleanup