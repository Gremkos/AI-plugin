"""AI Plugin conversation entity — integrates with HA Assist pipeline."""

from __future__ import annotations

import asyncio
import logging
import re

from homeassistant.components import conversation
from homeassistant.components.conversation import (
    ConversationEntityFeature,
    ConversationInput,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import ulid as ulid_util

from .const import (
    DOMAIN,
    FOLLOW_UP_ALWAYS,
    FOLLOW_UP_AUTO,
    ends_with_question,
    follow_up_mode,
)
from .exceptions import OrchestratorError
from .i18n import L
from .orchestrator import Orchestrator

_LOGGER = logging.getLogger(__name__)

STATUS_ENTITY_ID = "sensor.alice_assist_status"
_STATUS_ICONS = {
    "idle": "mdi:robot-happy-outline",
    "listening": "mdi:microphone",
    "processing": "mdi:brain",
    "responding": "mdi:account-voice",
}
_STAGE_TAG_RE = re.compile(r"\[[^\]]*\]")


def async_set_assist_status(
    hass: HomeAssistant,
    status: str,
    auto_idle_delay: float | None = None,
) -> None:
    """Update sensor.alice_assist_status immediately in Home Assistant."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    old_timer: asyncio.TimerHandle | None = domain_data.pop("_status_timer", None)
    if old_timer is not None:
        old_timer.cancel()

    hass.states.async_set(
        STATUS_ENTITY_ID,
        status,
        {
            "friendly_name": "Alice Assist Status",
            "icon": _STATUS_ICONS.get(status, "mdi:robot-outline"),
        },
    )

    if auto_idle_delay is not None and auto_idle_delay > 0:
        loop = asyncio.get_running_loop()
        domain_data["_status_timer"] = loop.call_later(
            auto_idle_delay,
            lambda: async_set_assist_status(hass, "idle"),
        )


def _estimate_speech_seconds(reply: str, expects_follow_up: bool) -> float:
    """Estimate spoken TTS duration in seconds, ignoring [emotion] tags."""
    spoken = _STAGE_TAG_RE.sub("", reply or "").strip()
    duration = max(1.5, round(len(spoken) / 14.0 + 1.2, 1))
    if expects_follow_up:
        duration += 4.0
    return duration


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the AI Plugin conversation entity from a config entry."""
    entity = AIPluginConversationEntity(hass, config_entry)
    hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})["conversation_entity"] = entity
    async_add_entities([entity])


class AIPluginConversationEntity(conversation.ConversationEntity):
    """Conversation entity that routes HA Assist through AI Plugin."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_languages = "*"
    _attr_supported_features = ConversationEntityFeature.CONTROL
    _attr_supports_streaming = True

    @property
    def supported_languages(self) -> list[str] | str:
        return "*"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._orchestrator = Orchestrator(hass, entry)
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
        )

    async def _async_handle_message(
        self, user_input: ConversationInput, chat_log
    ) -> ConversationResult:
        """Process a user message from HA Assist and return a reply."""
        async_set_assist_status(self.hass, "processing")

        user_id: str | None = (
            getattr(user_input.context, "user_id", None) if user_input.context else None
        )
        conversation_id = (
            getattr(chat_log, "conversation_id", None)
            or user_input.conversation_id
            or ulid_util.ulid_now()
        )
        _LOGGER.info(
            "AI Plugin: conv_id=%s user_id=%s input=%r",
            conversation_id,
            user_id,
            user_input.text[:80],
        )

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        consumer: asyncio.Task | None = None
        agent_id = getattr(self, "entity_id", None) or DOMAIN

        async def _deltas():
            yield {"role": "assistant"}
            while (item := await queue.get()) is not None:
                yield {"content": item}

        async def _consume() -> None:
            try:
                async for _ in chat_log.async_add_delta_content_stream(
                    agent_id, _deltas()
                ):
                    pass
            except Exception:  # noqa: BLE001
                _LOGGER.debug("AI Plugin: chat-log delta stream failed", exc_info=True)

        def _on_delta(text: str) -> None:
            nonlocal consumer
            queue.put_nowait(text)
            if consumer is None:
                consumer = asyncio.get_running_loop().create_task(_consume())

        stream_capable = hasattr(chat_log, "async_add_delta_content_stream")
        try:
            reply = await self._orchestrator.async_process(
                message=user_input.text,
                conversation_id=conversation_id,
                language=user_input.language,
                device_id=user_input.device_id,
                user_id=user_id,
                on_delta=_on_delta if stream_capable else None,
            )
        except OrchestratorError as exc:
            _LOGGER.error("AI Plugin error processing message: %s", exc)
            _lang = (user_input.language or "en").split("-")[0].lower()
            reply = f"{L.template('err_process', _lang)} ({exc})"
        finally:
            if consumer is not None:
                queue.put_nowait(None)
                try:
                    await consumer
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("AI Plugin: delta consumer failed", exc_info=True)

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(reply)
        mode = follow_up_mode(self._entry.options)
        if mode == FOLLOW_UP_AUTO:
            continue_conversation = ends_with_question(reply)
        else:
            continue_conversation = mode == FOLLOW_UP_ALWAYS

        async_set_assist_status(
            self.hass,
            "responding",
            auto_idle_delay=_estimate_speech_seconds(reply, continue_conversation),
        )

        return ConversationResult(
            response=intent_response,
            conversation_id=conversation_id,
            continue_conversation=continue_conversation,
        )

    async def async_added_to_hass(self) -> None:
        """Start keeping the voice prompt read in by the model."""
        await super().async_added_to_hass()
        async_set_assist_status(self.hass, "idle")
        self._orchestrator.async_start_warmup()

    async def async_will_remove_from_hass(self) -> None:
        """Close provider sessions when the entity is removed."""
        old_timer: asyncio.TimerHandle | None = self.hass.data.get(DOMAIN, {}).pop(
            "_status_timer", None
        )
        if old_timer is not None:
            old_timer.cancel()
        await self._orchestrator.async_close()