"""Timer completions that arrive as a conversation turn.

In announce mode every timer carries a conversation_command, so HA calls
the agent back at expiry with a sentinel instead of ringing on the
satellite. This mixin recognises that call and plays the announcement
through a media player.
"""

from __future__ import annotations

import logging
import re

from homeassistant.helpers import entity_registry as er

from ..const import (
    CONF_TIMER_ANNOUNCE,
    CONF_TIMER_ANNOUNCE_CHIME,
    DEFAULT_TIMER_ANNOUNCE,
    DEFAULT_TIMER_ANNOUNCE_CHIME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
_TRAILING_TIMER_WORD_RE = re.compile(r"[\s\-]+(?:timer|wecker|countdown)$", re.IGNORECASE)


def _clean_timer_label(raw: str) -> str:
    """Clean trailing 'Timer' so 'Pizza-Timer' does not become 'Timer Pizza-Timer'."""
    cleaned = _TRAILING_TIMER_WORD_RE.sub("", (raw or "").strip()).strip()
    return cleaned or (raw or "").strip()


class TimerAnnounceMixin:
    """Timer-completion handling. Mixed into Orchestrator."""

    def is_voice_device(self, device_id: str | None) -> bool:
        """Public alias of _is_voice_device for callers in other modules."""
        return self._is_voice_device(device_id)

    def _timer_announce_agent_id(self) -> str | None:
        """Return this integration's conversation entity_id when timer
        announcements are enabled, else None (default off = ring on device)."""
        if not self._entry.options.get(CONF_TIMER_ANNOUNCE, DEFAULT_TIMER_ANNOUNCE):
            return None
        if self._hass is None:
            return None
        try:
            return er.async_get(self._hass).async_get_entity_id(
                "conversation", DOMAIN, self._entry.entry_id
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("AI Plugin: agent entity lookup failed", exc_info=True)
            return None

    async def _handle_timer_done(
        self, message: str, device_id: str | None, lang: str
    ) -> str:
        """Announce an expired timer on the satellite's speaker path.

        Returns "" — the announcement IS the reply; nothing is spoken twice.
        """
        from .. import satellite_speech  # noqa: PLC0415
        from ..const import TIMER_DONE_SENTINEL  # noqa: PLC0415
        from ..timer_entities import sync_timer_states  # noqa: PLC0415

        if self._hass is not None:
            sync_timer_states(self._hass)

        timer_name = _clean_timer_label(message[len(TIMER_DONE_SENTINEL):].strip())
        chime = str(
            self._entry.options.get(
                CONF_TIMER_ANNOUNCE_CHIME, DEFAULT_TIMER_ANNOUNCE_CHIME
            )
            or ""
        ).strip()
        text = None
        if self._hass is not None and device_id:
            if timer_name:
                text = await satellite_speech.async_announce(
                    self._hass,
                    device_id,
                    "timer_done_named",
                    lang,
                    chime=chime,
                    label=timer_name,
                )
            else:
                text = await satellite_speech.async_announce(
                    self._hass,
                    device_id,
                    "timer_done",
                    lang,
                    chime=chime,
                )
        if text is None:
            _LOGGER.warning(
                "AI Plugin: timer expired but could not be announced on device "
                "%s", device_id,
            )
        else:
            _LOGGER.info("AI Plugin: timer announcement %r on %s", text, device_id)
        return ""