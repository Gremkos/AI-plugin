"""AI Plugin conversation entity — integrates with HA Assist pipeline."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import difflib
import logging
from pathlib import Path
import re
import time
from typing import Any

from homeassistant.components import conversation
from homeassistant.components.conversation import (
    ConversationEntityFeature,
    ConversationInput,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, intent
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import ulid as ulid_util

from .const import (
    CONF_AUTO_DUCKING,
    CONF_FEEDBACK_LOOP_DEVICES,
    CONF_OFFLINE_FALLBACK,
    CONF_SELF_ECHO_FILTER,
    CONF_WORKER_STATUS_ENTITY,
    DEFAULT_AUTO_DUCKING,
    DEFAULT_OFFLINE_FALLBACK,
    DEFAULT_SELF_ECHO_FILTER,
    DOMAIN,
    FOLLOW_UP_ALWAYS,
    FOLLOW_UP_AUTO,
    ends_with_question,
    follow_up_mode,
    matches_ephemeral_prefix,
    strip_ephemeral_prefix,
)
from .exceptions import OrchestratorError
from .i18n import L
from .music_ducking import (
    all_playing_players,
    duck as _duck_players,
    release as _release_players,
)
from .orchestrator import Orchestrator

_LOGGER = logging.getLogger(__name__)

STATUS_ENTITY_ID = "sensor.ai_plugin_assist_status"
_STATUS_ICONS = {
    "idle": "mdi:robot-happy-outline",
    "listening": "mdi:microphone",
    "processing": "mdi:brain",
    "responding": "mdi:account-voice",
}
_STAGE_TAG_RE = re.compile(r"\[([^\]]+)\]")
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
_PLAYER_DONE_STATES = frozenset({"idle", "paused", "off", "standby"})
_UNSET = object()
_CONV_DUCK_HOLDER = "ai_plugin_conversation"
_SELF_ECHO_TTL_S = 25.0


def _norm_echo_text(text: str) -> str:
    """Normalize text for self-echo comparison (strip stage tags & punctuation)."""
    cleaned = _STAGE_TAG_RE.sub(" ", text or "")
    return _NON_WORD_RE.sub(" ", cleaned.casefold()).strip()


def _is_self_echo(incoming: str, last_reply: str) -> bool:
    """True when ``incoming`` repeats parts of the assistant's own TTS reply."""
    inc = _norm_echo_text(incoming)
    rep = _norm_echo_text(last_reply)
    if not inc or not rep:
        return False
    if inc == rep:
        return True
    if len(inc) >= 10 and inc in rep:
        return True
    inc_words = inc.split()
    rep_words = set(rep.split())
    if len(inc_words) >= 2:
        overlap = sum(1 for w in inc_words if w in rep_words) / len(inc_words)
        if overlap >= 0.75:
            return True
    return difflib.SequenceMatcher(None, inc, rep).ratio() >= 0.65


def _should_auto_duck(user_input: ConversationInput) -> bool:
    """Duck on HA App devices (tablet/phone) and satellites, skip PC browser chats."""
    return bool(
        getattr(user_input, "satellite_id", None)
        or getattr(user_input, "device_id", None)
    )


def _read_attachment_b64(path: Path) -> str | None:
    """Read a binary attachment file from disk and return its base64 string."""
    try:
        raw = path.read_bytes()
        return base64.b64encode(raw).decode("ascii") if raw else None
    except Exception:  # noqa: BLE001
        return None


async def _extract_turn_attachments(
    hass: HomeAssistant, user_input: ConversationInput, chat_log: Any
) -> list[dict[str, Any]]:
    """Extract image/audio attachments from ChatLog or ConversationInput (HA #1540 ready)."""
    raw_attachments: list[Any] = []

    input_atts = getattr(user_input, "attachments", None)
    if input_atts:
        raw_attachments.extend(input_atts)

    content_list = getattr(chat_log, "content", None)
    if content_list:
        for item in reversed(content_list):
            if getattr(item, "role", None) == "user":
                item_atts = getattr(item, "attachments", None)
                if item_atts:
                    raw_attachments.extend(item_atts)
                break

    if not raw_attachments:
        return []

    parsed: list[dict[str, Any]] = []
    for att in raw_attachments:
        mime = str(getattr(att, "mime_type", "") or "").lower()
        if mime.startswith("image/"):
            att_type = "image"
        elif mime.startswith("audio/"):
            att_type = "audio"
        else:
            continue

        b64_data: str | None = None
        path = getattr(att, "path", None)
        if path is not None:
            b64_data = await hass.async_add_executor_job(_read_attachment_b64, Path(path))
        elif getattr(att, "data", None):
            raw_data = getattr(att, "data")
            if isinstance(raw_data, bytes):
                b64_data = base64.b64encode(raw_data).decode("ascii")
            elif isinstance(raw_data, str):
                b64_data = raw_data

        if b64_data:
            parsed.append(
                {
                    "type": att_type,
                    "data": b64_data,
                    "mime_type": mime,
                }
            )
    return parsed


def _resolve_device_area(hass: HomeAssistant, device_id: str | None) -> str | None:
    """Resolve Home Assistant area name for a device_id."""
    if not device_id:
        return None
    try:
        dev = dr.async_get(hass).async_get(device_id)
        if dev is None or not dev.area_id:
            return None
        area = ar.async_get(hass).async_get_area(dev.area_id)
        return area.name if area is not None else None
    except Exception:  # noqa: BLE001
        return None


def _device_media_player(hass: HomeAssistant, device_id: str | None) -> str | None:
    """Find a media_player entity belonging to device_id, if any."""
    if not device_id:
        return None
    try:
        for entry in er.async_entries_for_device(er.async_get(hass), device_id):
            if entry.entity_id.startswith("media_player."):
                return entry.entity_id
    except Exception:  # noqa: BLE001
        return None
    return None


def _extract_emotion(reply: str) -> str | None:
    """Extract the first [emotion] tag from reply, if present."""
    match = _STAGE_TAG_RE.search(reply or "")
    if not match:
        return None
    tag = match.group(1).strip().lower()
    return tag or None


def async_update_assist_meta(
    hass: HomeAssistant,
    *,
    area: str | None | object = _UNSET,
    emotion: str | None | object = _UNSET,
    last_tool: str | None | object = _UNSET,
) -> None:
    """Update metadata attributes stored for sensor.ai_plugin_assist_status."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    meta: dict[str, str | None] = domain_data.setdefault(
        "_status_meta",
        {"area": None, "emotion": None, "last_tool": None},
    )
    if area is not _UNSET:
        meta["area"] = area  # type: ignore[assignment]
    if emotion is not _UNSET:
        meta["emotion"] = emotion  # type: ignore[assignment]
    if last_tool is not _UNSET:
        meta["last_tool"] = last_tool  # type: ignore[assignment]


def _apply_media_ducking(hass: HomeAssistant, duck: bool) -> None:
    """Lower active media_player volumes during Assist turns and restore on idle."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if duck:
        if not domain_data.get("auto_ducking", False):
            return
        players = all_playing_players(hass)
        if players:
            _duck_players(hass, _CONV_DUCK_HOLDER, players)
    else:
        _release_players(hass, _CONV_DUCK_HOLDER)


def async_set_assist_status(
    hass: HomeAssistant,
    status: str,
    auto_idle_delay: float | None = None,
    watch_media_player: str | None = None,
    expects_follow_up: bool = False,
    allow_ducking: bool = True,
) -> None:
    """Update sensor.ai_plugin_assist_status immediately in Home Assistant."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    old_timer: asyncio.TimerHandle | None = domain_data.pop("_status_timer", None)
    if old_timer is not None:
        old_timer.cancel()

    old_unsub: Callable[[], None] | None = domain_data.pop("_status_player_unsub", None)
    if old_unsub is not None:
        old_unsub()

    if status == "idle":
        async_update_assist_meta(hass, area=None, emotion=None)
        _apply_media_ducking(hass, duck=False)
    else:
        if status == "listening":
            async_update_assist_meta(hass, emotion=None)
        if allow_ducking:
            _apply_media_ducking(hass, duck=True)

    meta = domain_data.setdefault(
        "_status_meta",
        {"area": None, "emotion": None, "last_tool": None},
    )

    hass.states.async_set(
        STATUS_ENTITY_ID,
        status,
        {
            "friendly_name": "Assist Status",
            "icon": _STATUS_ICONS.get(status, "mdi:robot-outline"),
            "device_class": "enum",
            "options": ["idle", "listening", "processing", "responding"],
            "area": meta.get("area"),
            "emotion": meta.get("emotion"),
            "last_tool": meta.get("last_tool"),
        },
    )

    if status == "responding" and watch_media_player:
        player_state = hass.states.get(watch_media_player)
        seen_playing = player_state is not None and player_state.state == "playing"

        @callback
        def _on_player_change(event: Event) -> None:
            nonlocal seen_playing
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            if new_state.state == "playing":
                seen_playing = True
                return
            if seen_playing and new_state.state in _PLAYER_DONE_STATES:
                unsub = domain_data.pop("_status_player_unsub", None)
                if unsub is not None:
                    unsub()
                settle = 4.0 if expects_follow_up else 0.4
                if settle <= 0:
                    async_set_assist_status(hass, "idle", auto_idle_delay=None)
                t_old: asyncio.TimerHandle | None = domain_data.pop("_status_timer", None)
                if t_old is not None:
                    t_old.cancel()
                domain_data["_status_timer"] = asyncio.get_running_loop().call_later(
                    settle,
                    lambda: async_set_assist_status(hass, "idle"),
                )

        domain_data["_status_player_unsub"] = async_track_state_change_event(
            hass, [watch_media_player], _on_player_change
        )

    if status == "listening" and auto_idle_delay is None:
        auto_idle_delay = 25.0

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
    if not hass.services.has_service(DOMAIN, "set_assist_status"):
        async def _handle_set_assist_status(call: ServiceCall) -> None:
            new_status = str(call.data.get("status", "idle")).strip().lower()
            if new_status not in _STATUS_ICONS:
                new_status = "idle"
            delay = call.data.get("auto_idle_delay")
            allow_ducking = bool(call.data.get("duck", True))
            async_set_assist_status(
                hass,
                new_status,
                auto_idle_delay=float(delay) if delay is not None else None,
                allow_ducking=allow_ducking,
            )

        hass.services.async_register(
            DOMAIN,
            "set_assist_status",
            _handle_set_assist_status,
        )

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
        # key -> (reply, timestamp)
        self._recent_replies: dict[str, tuple[str, float]] = {}
        hass.data.setdefault(DOMAIN, {})["auto_ducking"] = bool(
            entry.options.get(CONF_AUTO_DUCKING, DEFAULT_AUTO_DUCKING)
        )

    def _self_echo_filter_enabled(self) -> bool:
        """Return True if the satellite self-echo filter option is enabled."""
        return bool(
            self._entry.options.get(CONF_SELF_ECHO_FILTER, DEFAULT_SELF_ECHO_FILTER)
        )

    def _remember_reply(self, target_id: str | None, reply: str) -> None:
        """Store the assistant's last spoken reply when self-echo filtering is on."""
        if not self._self_echo_filter_enabled() or not reply or not reply.strip():
            return
        now = time.monotonic()
        self._recent_replies["__last__"] = (reply, now)
        if target_id:
            self._recent_replies[target_id] = (reply, now)

    def _check_and_clear_echo(
        self, user_input: ConversationInput, conversation_id: str
    ) -> bool:
        """Return True if ``user_input.text`` is an echo of the assistant's own reply."""
        if not self._self_echo_filter_enabled():
            return False
        now = time.monotonic()
        keys = [
            k
            for k in (
                getattr(user_input, "satellite_id", None),
                getattr(user_input, "device_id", None),
                conversation_id,
                "__last__",
            )
            if k
        ]
        for key in keys:
            entry = self._recent_replies.get(key)
            if entry is None:
                continue
            last_reply, ts = entry
            if now - ts > _SELF_ECHO_TTL_S:
                self._recent_replies.pop(key, None)
                continue
            if _is_self_echo(user_input.text, last_reply):
                for other_k, (other_reply, _) in list(self._recent_replies.items()):
                    if other_reply == last_reply:
                        self._recent_replies.pop(other_k, None)
                return True
        return False

    def _is_feedback_loop_device(self, user_input: ConversationInput) -> bool:
        """True if the calling device/satellite is marked to suppress follow-up listening."""
        configured = self._entry.options.get(CONF_FEEDBACK_LOOP_DEVICES) or []
        if not configured:
            return False
        targets = {
            str(v)
            for v in (
                getattr(user_input, "device_id", None),
                getattr(user_input, "satellite_id", None),
            )
            if v
        }
        return bool(targets & {str(x) for x in configured})

    def _is_offline_fallback_enabled(self) -> bool:
        """Return True if offline fallback to HA Assist is enabled."""
        return bool(
            self._entry.options.get(
                CONF_OFFLINE_FALLBACK,
                self._entry.data.get(CONF_OFFLINE_FALLBACK, DEFAULT_OFFLINE_FALLBACK),
            )
        )

    def _is_worker_offline(self) -> bool:
        """Return True when a configured backend status sensor reports offline."""
        worker_entity = self._entry.options.get(
            CONF_WORKER_STATUS_ENTITY,
            self._entry.data.get(CONF_WORKER_STATUS_ENTITY),
        )
        if not worker_entity:
            return False
        state_obj = self.hass.states.get(worker_entity)
        return state_obj is not None and state_obj.state in (
            "offline",
            "unavailable",
            "unknown",
        )

    @staticmethod
    def _is_connection_error(exc: Exception) -> bool:
        """Return True if an OrchestratorError indicates an unreachable backend."""
        msg = str(exc).lower()
        return any(
            token in msg
            for token in (
                "cannot connect",
                "cannot_connect",
                "connection",
                "timed out",
                "timeout",
                "unreachable",
                "refused",
            )
        )

    async def _delegate_to_ha_assist(
        self, user_input: ConversationInput, conversation_id: str
    ) -> ConversationResult:
        """Forward the turn to Home Assistant's built-in conversation agent."""
        _LOGGER.debug(
            "AI Plugin offline fallback active -> delegating to conversation.home_assistant"
        )
        result = await conversation.async_converse(
            hass=self.hass,
            text=user_input.text,
            conversation_id=conversation_id,
            context=user_input.context,
            language=user_input.language or self.hass.config.language,
            agent_id="conversation.home_assistant",
            device_id=user_input.device_id,
            satellite_id=user_input.satellite_id,
        )
        speech_plain = result.response.speech.get("plain", {}).get("speech", "")
        player_entity = _device_media_player(self.hass, user_input.device_id)
        async_set_assist_status(
            self.hass,
            "responding",
            auto_idle_delay=_estimate_speech_seconds(
                speech_plain, result.continue_conversation
            ),
            watch_media_player=player_entity,
            expects_follow_up=result.continue_conversation,
            allow_ducking=_should_auto_duck(user_input),
        )
        return result

    async def _async_handle_message(
        self, user_input: ConversationInput, chat_log
    ) -> ConversationResult:
        """Process a user message from HA Assist and return a reply."""
        conversation_id = (
            getattr(chat_log, "conversation_id", None)
            or user_input.conversation_id
            or ulid_util.ulid_now()
        )
        can_duck = _should_auto_duck(user_input)
        ephemeral_input = matches_ephemeral_prefix(
            user_input.text, self._entry.options
        )

        # Optional self-echo guard (controlled via CONF_SELF_ECHO_FILTER)
        if not ephemeral_input and self._check_and_clear_echo(user_input, conversation_id):
            _LOGGER.info(
                "AI Plugin: ignored self-echo on %s (text=%r)",
                getattr(user_input, "satellite_id", None) or user_input.device_id,
                (user_input.text or "")[:80],
            )
            async_set_assist_status(self.hass, "idle")
            empty_response = intent.IntentResponse(language=user_input.language)
            empty_response.async_set_speech("")
            return ConversationResult(
                response=empty_response,
                conversation_id=conversation_id,
                continue_conversation=False,
            )

        area_name = _resolve_device_area(self.hass, user_input.device_id)
        async_update_assist_meta(self.hass, area=area_name, emotion=None)
        async_set_assist_status(self.hass, "processing", allow_ducking=can_duck)

        user_id: str | None = (
            getattr(user_input.context, "user_id", None) if user_input.context else None
        )
        _LOGGER.info(
            "AI Plugin: conv_id=%s user_id=%s input=%r",
            conversation_id,
            user_id,
            user_input.text[:80],
        )

        if self._is_offline_fallback_enabled() and self._is_worker_offline():
            return await self._delegate_to_ha_assist(user_input, conversation_id)

        turn_attachments = await _extract_turn_attachments(
            self.hass, user_input, chat_log
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

        # External satellites expect an atomic ConversationResult rather than
        # chunked delta streaming so the pipeline closes cleanly.
        is_satellite = bool(getattr(user_input, "satellite_id", None))
        stream_capable = (
            hasattr(chat_log, "async_add_delta_content_stream")
            and not is_satellite
            and not ephemeral_input
        )
        try:
            reply = await self._orchestrator.async_process(
                message=user_input.text,
                conversation_id=conversation_id,
                language=user_input.language,
                device_id=user_input.device_id,
                user_id=user_id,
                on_delta=_on_delta if stream_capable else None,
                attachments=turn_attachments or None,
            )
        except OrchestratorError as exc:
            if self._is_offline_fallback_enabled() and self._is_connection_error(exc):
                _LOGGER.warning(
                    "AI Plugin backend unreachable (%s), delegating to HA Assist", exc
                )
                return await self._delegate_to_ha_assist(user_input, conversation_id)
            _LOGGER.error("AI Plugin error processing message: %s", exc)
            _lang = (user_input.language or "en").split("-")[0].lower()
            reply = f"{L.template('err_process', _lang)} ({exc})"
        except BaseException:
            async_set_assist_status(self.hass, "idle")
            raise
        finally:
            if consumer is not None:
                queue.put_nowait(None)
                try:
                    await consumer
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("AI Plugin: delta consumer failed", exc_info=True)

        is_ephemeral = ephemeral_input or matches_ephemeral_prefix(
            reply, self._entry.options
        )
        if is_ephemeral:
            reply = strip_ephemeral_prefix(reply, self._entry.options)

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(reply)
        mode = follow_up_mode(self._entry.options)
        if is_ephemeral or self._is_feedback_loop_device(user_input):
            continue_conversation = False
        elif mode == FOLLOW_UP_AUTO:
            continue_conversation = ends_with_question(reply)
        else:
            continue_conversation = mode == FOLLOW_UP_ALWAYS

        if not is_ephemeral:
            self._remember_reply(
                getattr(user_input, "satellite_id", None) or user_input.device_id,
                reply,
            )
            self._remember_reply(conversation_id, reply)

        async_update_assist_meta(self.hass, emotion=_extract_emotion(reply))
        player_entity = _device_media_player(self.hass, user_input.device_id)
        async_set_assist_status(
            self.hass,
            "responding",
            auto_idle_delay=_estimate_speech_seconds(reply, continue_conversation),
            watch_media_player=player_entity,
            expects_follow_up=continue_conversation,
            allow_ducking=can_duck,
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
        domain_data = self.hass.data.get(DOMAIN, {})
        old_timer: asyncio.TimerHandle | None = domain_data.pop("_status_timer", None)
        if old_timer is not None:
            old_timer.cancel()
        old_unsub: Callable[[], None] | None = domain_data.pop("_status_player_unsub", None)
        if old_unsub is not None:
            old_unsub()
        _apply_media_ducking(self.hass, duck=False)
        await self._orchestrator.async_close()