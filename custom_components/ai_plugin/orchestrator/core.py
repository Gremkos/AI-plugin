"""The Orchestrator itself: one user turn, start to finish."""

from __future__ import annotations

import asyncio
import copy
import contextvars
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from ..const import (
    CONF_CONTEXT_WINDOW,
    CONF_DIARY_DAYS,
    CONF_DIARY_ENABLED,
    CONF_DIARY_SHARE,
    CONF_ENABLE_THINKING,
    CONF_MODEL,
    CONF_MAX_TOOL_ITERATIONS,
    CONF_PRUNE_TOOL_SCHEMAS,
    CONF_SUMMARIZATION_ENABLED,
    CONF_TOOL_ALARMS,
    CONF_TOOL_BROADCAST,
    CONF_TOOL_FIND_PHONE,
    CONF_TOOL_MESSAGES,
    CONF_VOICE_MODE,
    CONF_WEB_SEARCH_ENABLED,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_DIARY_DAYS,
    DEFAULT_DIARY_ENABLED,
    DEFAULT_DIARY_SHARE,
    DEFAULT_ENABLE_THINKING,
    DEFAULT_MAX_TOOL_ITERATIONS,
    DEFAULT_PRUNE_TOOL_SCHEMAS,
    DEFAULT_SUMMARIZATION_ENABLED,
    DOMAIN,
    features_off,
    tools_off,
)
from ..context_manager import ContextManager
from ..diary import BLOCK_MARKER as DIARY_MARKER, format_block as format_diary_block
from ..diary.writer import build_prompt as diary_prompt, parse_notes as diary_notes
from ..i18n import L, SUPPORTED_LANGS
from ..providers.openai_compat import OpenAICompatProvider
from ..shortcuts import (
    async_try_action_shortcut,
    async_try_domain_sweep_shortcut,
    async_try_media_shortcut,
    try_shortcut,
)
from ..shortcuts.phone import (
    RING_PHONE_SCHEMA,
    RING_PHONE_TOOL,
    async_ring_phone,
    match_find_phone,
    people_with_phones,
    person_for_user,
    resolve_owner,
)
from ..shortcuts.registry import _caller_area_id
from ..tools.ha_local import HALocalToolRegistry
from ..tools.ha_local.formatting import _s
from ..tools.ha_local.lights import _USER_SAID_ALL_RE, is_generic_lights_command
from ..tools.ha_local.lights import (
    WINDOW_COVERINGS,
    hass_light_set_args,
    widen_cover_classes,
)
from ..scheduler import (
    asks_for_phrase_routine,
    TOOL_NAMES as SCHEDULER_TOOL_NAMES,
    TOOL_SCHEMAS as SCHEDULER_TOOL_SCHEMAS,
    WRITE_TOOL_NAMES as SCHEDULER_WRITE_TOOL_NAMES,
    Job,
    async_call_tool as async_call_scheduler_tool,
    async_drop_replaced,
    names_later_time,
)
from ..tools.calendar_events import (
    TOOL_NAME as CALENDAR_TOOL,
    TOOL_SCHEMA as CALENDAR_SCHEMA,
    REMOVE_TOOL_NAME as CALENDAR_REMOVE_TOOL,
    REMOVE_TOOL_SCHEMA as CALENDAR_REMOVE_SCHEMA,
    async_add_event,
    async_remove_event,
)
from ..tools.ha_local.schemas import MOVE_MUSIC_SCHEMA, TOOLS_THE_PLUGIN_COVERS
from ..tools.tell import (
    BROADCAST_TOOL_NAME,
    TOOL_NAME as TELL_TOOL,
    TOOL_SCHEMA as TELL_SCHEMA,
    async_broadcast,
    async_tell,
    tell_schema,
)
from ..tools.memory import (
    asks_to_remember,
    TOOL_NAMES as MEMORY_TOOL_NAMES,
    TOOL_SCHEMAS as MEMORY_TOOL_SCHEMAS,
    WRITE_TOOL_NAMES as MEMORY_WRITE_TOOLS,
    MemoryTool,
)
from ..tools.web_search import TOOL_SCHEMA as WEB_SEARCH_SCHEMA, WebSearchTool
from ..tools.browse_url import TOOL_SCHEMA as BROWSE_URL_SCHEMA, BrowseUrlTool
from ..voice_id import VOICE_ENROLL_SCHEMA, VOICE_ENROLL_TOOL
from .. import satellite_speech

if TYPE_CHECKING:
    from ..providers.base import AbstractProvider
    from ..tools.mcp_client import MCPToolRegistry

from .delta_gate import _DeltaGate
from .grounding import (
    _TIMER_WRITE_TOOL_NAMES,
    _ACTUATOR_TOOL_NAMES,
    _MISS_RESULT_PATTERNS,
    _any_actuator_success,
    _any_media_success,
    _any_tool_call,
    _any_unobservable_action,
    _is_action_command,
)
from .entity_context import EntityContextMixin
from .location import LocationProvider
from .prompts import PromptBlocksMixin
from .textproc import (
    _flatten_for_voice,
    _strip_emoji,
    _strip_filler_closing,
    _strip_narration,
)
from .timers import TimerAnnounceMixin
from .warmup import PromptWarmupMixin
from .toolcalls import (
    _parse_raw_tool_call,
    _prune_ha_local_schemas,
    name_without_room,
    split_list_items,
)

_LOGGER = logging.getLogger(__name__)

TOOL_WAIT_LINE_DELAY_S = 2.0
ACTION_FIRST_REPLY_MAX_TOKENS = 128

ROOM_COLOUR_REPLACED_RESULT = (
    "[Not changed: another colour for the whole room would only replace the one "
    "just set. To give lamps different colours, call set_brightness once per lamp "
    "with name and color; list_entities(domain: 'light') names them. If one colour "
    "was all that was asked for, stop here.]"
)

REPEATED_LOOKUP_RESULT = (
    "You already ran this exact {name} call in this turn; its result is above "
    "and running it again returns the same thing. Do not repeat it. Answer the "
    "user now with what you found, and say plainly what you could not find."
)


class _WaitLine:
    """Per-tool-loop state of the wait line: pending timer, speaking task."""

    __slots__ = ("task", "timer")

    def __init__(self) -> None:
        self.timer: asyncio.TimerHandle | None = None
        self.task: asyncio.Task | None = None


_IN_SCHEDULED_RUN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ai_plugin_in_scheduled_run", default=False
)
_PREVIOUS_REPLY: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ai_plugin_previous_reply", default=""
)

DIARY_QUIET_S = 600.0
DIARY_TICK_S = 60.0


def _new_diary_conv() -> dict:
    return {
        "turns": [],
        "owner": None,
        "mixed": False,
        "language": "en",
        "last": 0.0,
        "when": None,
        "known": set(),
    }


def asks_to_forget_diary(message: str, lang: str) -> bool:
    """True for 'Forget our conversations': the diary, not a saved fact."""
    text = (message or "").strip().lower()
    return any(rx.search(text) for rx in L.pattern_list("diary_forget", lang))


_ROOM_FREE_INTENTS = frozenset({
    "HassTurnOn",
    "HassTurnOff",
    "HassToggle",
    "HassFanSetSpeed",
    "HassSetPosition",
})


def says_this_room(message: str) -> bool:
    """True when a command is for the room the speaker is in: 'in here', 'hier'."""
    text = (message or "").lower()

    def found(key: str) -> bool:
        return any(
            rx.search(text)
            for code in SUPPORTED_LANGS
            for rx in L.pattern_list(key, code)
        )

    return found("this_room") and not found("this_room_except")


def moves_here(message: str) -> bool:
    """True when something is to be moved to the speaker's room: 'move it here'."""
    text = (message or "").strip().lower()
    return any(
        rx.search(text)
        for code in SUPPORTED_LANGS
        for rx in L.pattern_list("move_here", code)
    ) and not any(
        rx.search(text)
        for code in SUPPORTED_LANGS
        for rx in L.pattern_list("this_room_except", code)
    )


def asks_for_second_room(message: str, lang: str) -> bool:
    """True for 'also play in the bathroom': music in another room as well."""
    text = (message or "").strip().lower()
    return any(rx.search(text) for rx in L.pattern_list("second_room", lang))


_BROADCAST_ANSWER_S = 60.0
_HOME_ASSISTANT_BUILTIN_TOOLS = frozenset({
    "GetLiveContext",
    "GetDateTime",
    "todo_get_items",
    "calendar_get_events",
})


def _broadcast_words(message: str) -> str:
    return (message or "").strip().strip(" .,!?¿¡").lower()


def asks_for_empty_broadcast(message: str, lang: str) -> bool:
    """True for 'Broadcast.' alone: there is no message yet."""
    text = _broadcast_words(message)
    return any(rx.search(text) for rx in L.pattern_list("broadcast_empty", lang))


def person_asked_about(reply: str, lang: str, names) -> str | None:
    """The one person a reply asks the message for: 'What should I say to Anna?'."""
    text = (reply or "").strip().lower()
    if not text.endswith("?"):
        return None
    question = re.split(r"(?<=[.!])\s+", text)[-1]
    if not any(rx.search(question) for rx in L.pattern_list("message_asked", lang)):
        return None

    def named_in(part: str) -> list[str]:
        return [n for n in names if re.search(rf"\b{re.escape(n.lower())}\b", part)]

    named = named_in(question) or named_in(text)
    return named[0] if len(named) == 1 else None


def corrects_message_target(message: str, lang: str) -> bool:
    """True for 'Not to Anna, the other person'."""
    text = _broadcast_words(message)
    return any(rx.search(text) for rx in L.pattern_list("message_correction", lang))


def asks_what_to_broadcast(message: str, reply: str, lang: str) -> bool:
    """True when the model answered a request to announce by asking what to say."""
    if not (reply or "").strip().endswith("?"):
        return False
    return all(
        any(rx.search(text.lower()) for rx in L.pattern_list("broadcast_word", lang))
        for text in (message or "", reply)
    )


def cancels_broadcast(message: str, lang: str) -> bool:
    """True for 'never mind' in answer to what to announce."""
    text = _broadcast_words(message)
    return any(rx.search(text) for rx in L.pattern_list("broadcast_cancel", lang))


def _room_colour_scope(name: str, arguments: dict) -> tuple[str, str] | None:
    """(room, colour) when this call colours a whole room, else None."""
    if name != "set_brightness" or arguments.get("name"):
        return None
    colour = str(arguments.get("color") or "").strip().lower()
    if not colour:
        return None
    return str(arguments.get("area") or "").strip().lower(), colour


class Orchestrator(
    EntityContextMixin, PromptBlocksMixin, PromptWarmupMixin, TimerAnnounceMixin
):
    """Processes messages: history -> system prompt -> LLM -> reply."""

    _thinking_announced = False
    _voice_id = None
    _turn_speakers: dict | None = None
    _enroll_replies: dict | None = None

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._provider: AbstractProvider = self._build_provider()

        entry_data: dict = (
            hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
            if hass is not None
            else {}
        )
        self._mcp: MCPToolRegistry | None = entry_data.get("mcp")
        self._memory: MemoryTool | None = entry_data.get("memory")
        self._ha_local: HALocalToolRegistry | None = entry_data.get("ha_local")
        self._voice_id = entry_data.get("voice_id")
        self._scheduler = entry_data.get("scheduler")
        if self._scheduler is not None:
            self._scheduler.set_command_runner(self._run_scheduled_command)

        opts = entry.options
        tool_budget = (
            self._mcp.estimate_schema_tokens() if self._mcp else 2000
        )
        self._context_mgr = ContextManager(
            max_tokens=opts.get(CONF_CONTEXT_WINDOW, DEFAULT_CONTEXT_WINDOW),
            tool_token_budget=tool_budget,
        )
        self._summarization_enabled: bool = opts.get(
            CONF_SUMMARIZATION_ENABLED, DEFAULT_SUMMARIZATION_ENABLED
        )
        self._prune_schemas: bool = opts.get(
            CONF_PRUNE_TOOL_SCHEMAS, DEFAULT_PRUNE_TOOL_SCHEMAS
        )
        self._bg_tasks: set[asyncio.Task] = set()
        self._max_tool_iterations: int = opts.get(
            CONF_MAX_TOOL_ITERATIONS, DEFAULT_MAX_TOOL_ITERATIONS
        )
        self._web_search: WebSearchTool | None = (
            WebSearchTool(opts) if opts.get(CONF_WEB_SEARCH_ENABLED, False) else None
        )
        self._browse_url: BrowseUrlTool | None = (
            BrowseUrlTool() if opts.get(CONF_WEB_SEARCH_ENABLED, False) else None
        )

        self._location: LocationProvider = LocationProvider(hass, entry)
        self._conv_locks: dict[str, asyncio.Lock] = {}
        self._last_entities: dict[str, str] = {}

        self._diary = (
            entry_data.get("diary")
            if opts.get(CONF_DIARY_ENABLED, DEFAULT_DIARY_ENABLED)
            else None
        )
        self._diary_share = bool(opts.get(CONF_DIARY_SHARE, DEFAULT_DIARY_SHARE))
        self._diary_days = float(opts.get(CONF_DIARY_DAYS, DEFAULT_DIARY_DAYS))
        self._diary_convs: dict[str, dict] = {}
        self._diary_pruned = 0.0
        if self._diary is not None and hass is not None:
            from homeassistant.helpers.event import (  # noqa: PLC0415
                async_track_time_interval,
            )

            entry.async_on_unload(
                async_track_time_interval(
                    hass, self._async_diary_tick, timedelta(seconds=DIARY_TICK_S)
                )
            )
            hass.async_create_task(self._diary.async_ensure_model())

    def _get_conv_lock(self, conv_id: str) -> asyncio.Lock:
        if not hasattr(self, "_conv_locks"):
            self._conv_locks = {}
        if conv_id not in self._conv_locks:
            self._conv_locks[conv_id] = asyncio.Lock()
        return self._conv_locks[conv_id]

    def _is_voice_device(self, device_id: str | None) -> bool:
        """True only when the device_id has an assist_satellite entity."""
        if not device_id or self._hass is None:
            return False
        try:
            ent_reg = er.async_get(self._hass)
            for entry in er.async_entries_for_device(ent_reg, device_id):
                if entry.entity_id.startswith("assist_satellite."):
                    return True
        except Exception:  # noqa: BLE001
            _LOGGER.debug("voice-device check failed", exc_info=True)
        return False

    def _build_provider(self) -> AbstractProvider:
        """Build provider from current entry options."""
        return OpenAICompatProvider.from_options(dict(self._entry.options))

    def _tool_schemas_for(
        self, message: str, voice_mode: bool, device_id: str | None
    ) -> list[dict]:
        """The tool schemas sent with a turn, in the order they are sent."""
        tool_schemas = self._mcp.get_tool_schemas() if self._mcp else []
        if self._ha_local is not None:
            tool_schemas = [
                s
                for s in tool_schemas
                if s.get("function", {}).get("name") not in TOOLS_THE_PLUGIN_COVERS
            ]
        if self._web_search is not None:
            tool_schemas = [WEB_SEARCH_SCHEMA, *tool_schemas]
        if self._browse_url is not None:
            tool_schemas = [BROWSE_URL_SCHEMA, *tool_schemas]
        if self._memory is not None:
            tool_schemas = [*MEMORY_TOOL_SCHEMAS, *tool_schemas]
        if (
            voice_mode
            and device_id
            and self._voice_id is not None
            and self._voice_id.enabled
            and self._voice_id.voice_enroll_allowed
        ):
            tool_schemas = [VOICE_ENROLL_SCHEMA, *tool_schemas]
        if self._can_ring_phones():
            tool_schemas = [RING_PHONE_SCHEMA, *tool_schemas]
        if self._ha_local is not None:
            ha_schemas = self._ha_local.get_schemas()
            if self._prune_schemas:
                ha_schemas = _prune_ha_local_schemas(message, ha_schemas)
            tool_schemas = [*ha_schemas, *tool_schemas]
        if getattr(self, "_scheduler", None) is not None:
            hass = getattr(self, "_hass", None)
            tell = (
                tell_schema(people_with_phones(hass).values())
                if hass is not None
                else TELL_SCHEMA
            )
            if not self._feature_on(CONF_TOOL_BROADCAST):
                tell = copy.deepcopy(tell)
                tell["function"]["description"] = tell["function"][
                    "description"
                ].replace(" For every room use HassBroadcast.", "")
            tool_schemas = [
                *tool_schemas,
                *SCHEDULER_TOOL_SCHEMAS,
                CALENDAR_SCHEMA,
                CALENDAR_REMOVE_SCHEMA,
                tell,
                MOVE_MUSIC_SCHEMA,
            ]
        off = self._tools_off()
        if off:
            tool_schemas = [
                s for s in tool_schemas if s.get("function", {}).get("name") not in off
            ]
        return tool_schemas

    async def async_process(
        self,
        message: str,
        conversation_id: str,
        language: str,
        device_id: str | None = None,
        user_id: str | None = None,
        on_delta=None,
    ) -> str:
        """Process a user message and return the assistant's reply."""
        voice_mode: bool = self._is_voice_device(device_id) or bool(
            self._entry.options.get(CONF_VOICE_MODE, False)
        )
        lang = (language or "en").split("-")[0].lower()

        from ..const import TIMER_DONE_SENTINEL  # noqa: PLC0415

        if message.startswith(TIMER_DONE_SENTINEL):
            return await self._handle_timer_done(message, device_id, lang)

        if self._voice_id is not None and self._voice_id.enrollment_active(device_id):
            enroll_reply = await self._voice_id.async_enrollment_turn(message, device_id)
            if enroll_reply is not None:
                return enroll_reply

        jobs_before: set[str] | None = None
        if getattr(self, "_scheduler", None) is not None:
            alarm_reply = await self._scheduler.async_handle_spoken(
                message, device_id, lang
            )
            if alarm_reply is not None:
                return alarm_reply
            jobs_before = {job.id for job in self._scheduler.jobs()}

        # Stream sentences immediately for normal queries; hold back only on
        # action commands so silent actuator suppression works cleanly.
        stream_safe = (
            on_delta is not None
            and not self._reasoning_active()
            and not _is_action_command(message)
        )
        gate = _DeltaGate(
            on_delta if stream_safe else None,
            lang=lang,
            voice_mode=voice_mode,
        )
        system_prompt = await self._build_system_prompt(voice_mode)

        for stale_id in self._context_mgr.evict_idle():
            self._conv_locks.pop(stale_id, None)
            self._last_entities.pop(stale_id, None)

        async with self._get_conv_lock(conversation_id):
            _PREVIOUS_REPLY.set(
                next(
                    (
                        m["content"]
                        for m in reversed(self._context_mgr.get_history(conversation_id))
                        if m.get("role") == "assistant"
                        and isinstance(m.get("content"), str)
                        and m["content"]
                    ),
                    "",
                )
            )
            await self._context_mgr.add_turn(conversation_id, "user", message)

            broadcast_reply = await self._broadcast_turn(
                message, conversation_id, device_id, lang
            )
            if broadcast_reply is not None:
                await self._context_mgr.add_turn(
                    conversation_id, "assistant", broadcast_reply
                )
                return broadcast_reply

            message_reply = await self._message_answer_turn(
                message, conversation_id, device_id, user_id, lang
            )
            if message_reply is not None:
                await self._context_mgr.add_turn(
                    conversation_id, "assistant", message_reply
                )
                return message_reply

            # Deterministic pre-LLM shortcuts
            try:
                shortcut_reply = try_shortcut(self._hass, message, lang=lang)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("AI Plugin: shortcut raised", exc_info=True)
                shortcut_reply = None
            if shortcut_reply:
                await self._context_mgr.add_turn(
                    conversation_id, "assistant", shortcut_reply
                )
                return shortcut_reply

            later = names_later_time(message, lang)

            if asks_for_phrase_routine(message, lang):
                refused = L.template("phrase_routine_refused", lang)
                await self._context_mgr.add_turn(conversation_id, "assistant", refused)
                return refused

            if (
                not later
                and self._ha_local is not None
                and asks_for_second_room(message, lang)
                and self._ha_local.music_players_playing()
            ):
                refused = L.template("second_room_refused", lang)
                await self._context_mgr.add_turn(conversation_id, "assistant", refused)
                return refused

            try:
                media_result = (
                    None
                    if later
                    else await async_try_media_shortcut(
                        self._hass, message, lang=lang, device_id=device_id
                    )
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("AI Plugin: media shortcut raised", exc_info=True)
                media_result = None
            if media_result is not None:
                handled, media_reply = media_result
                if handled:
                    await self._context_mgr.add_turn(
                        conversation_id, "assistant", media_reply
                    )
                    return media_reply

            try:
                sweep_result = (
                    None
                    if later
                    else await async_try_domain_sweep_shortcut(
                        self._hass, message, lang=lang, device_id=device_id
                    )
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("AI Plugin: sweep shortcut raised", exc_info=True)
                sweep_result = None
            if sweep_result is not None:
                handled, sweep_reply = sweep_result
                if handled:
                    await self._context_mgr.add_turn(
                        conversation_id, "assistant", sweep_reply
                    )
                    return sweep_reply

            try:
                action_result = (
                    None
                    if later
                    else await async_try_action_shortcut(
                        self._hass, message, lang=lang, device_id=device_id
                    )
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("AI Plugin: action shortcut raised", exc_info=True)
                action_result = None
            if action_result is not None:
                handled, action_reply = action_result
                if handled:
                    await self._context_mgr.add_turn(
                        conversation_id, "assistant", action_reply
                    )
                    return action_reply

            speaker_task: asyncio.Task | None = None
            owner = (
                match_find_phone(message, lang) if self._can_ring_phones() else None
            )
            if owner is not None:
                if (
                    not owner
                    and self._voice_id is not None
                    and voice_mode
                    and self._voice_id.should_identify(message, device_id, user_id)
                ):
                    speaker_task = asyncio.get_running_loop().create_task(
                        self._voice_id.async_identify(message, device_id)
                    )
                try:
                    phone_reply = await self._find_phone_shortcut(
                        owner, user_id, speaker_task, lang
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("AI Plugin: phone shortcut raised", exc_info=True)
                    phone_reply = None
                if phone_reply:
                    await self._context_mgr.add_turn(
                        conversation_id, "assistant", phone_reply
                    )
                    return phone_reply

            # Start async Voice-ID in parallel while building tool schemas
            if (
                speaker_task is None
                and self._voice_id is not None
                and voice_mode
                and self._voice_id.should_identify(message, device_id, user_id)
            ):
                speaker_task = asyncio.get_running_loop().create_task(
                    self._voice_id.async_identify(message, device_id)
                )

            tool_schemas = self._tool_schemas_for(message, voice_mode, device_id)
            self._note_prompt_head(system_prompt, tool_schemas, voice_mode)
            schema_tokens = self._context_mgr.estimate_tokens(str(tool_schemas))

            speaker = None
            if speaker_task is not None:
                try:
                    speaker = await speaker_task
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("AI Plugin: voice ID failed", exc_info=True)
                if speaker is not None:
                    user_id = speaker.memory_key
            if self._turn_speakers is None:
                self._turn_speakers = {}
            if device_id:
                if speaker is not None:
                    self._turn_speakers[device_id] = speaker.person_id
                elif speaker_task is not None:
                    self._turn_speakers[device_id] = None
                else:
                    self._turn_speakers.pop(device_id, None)

            diary = getattr(self, "_diary", None)
            diary_owner = (
                self._speaking_person(device_id, user_id) if diary is not None else None
            )
            if diary is not None and asks_to_forget_diary(message, lang):
                gone = await diary.async_forget(diary_owner)
                self._diary_convs.pop(conversation_id, None)
                forgot = L.template(
                    "diary_forgotten" if gone else "diary_forgotten_none", lang
                )
                await self._context_mgr.add_turn(conversation_id, "assistant", forgot)
                return forgot

            caller_room = None
            if device_id and self._hass is not None:
                _area_id = _caller_area_id(self._hass, device_id)
                if _area_id:
                    _area = ar.async_get(self._hass).async_get_area(_area_id)
                    if _area is not None:
                        caller_room = _area.name

            volatile = await self._build_volatile_block(
                user_id,
                conversation_id,
                speaker=speaker.name if speaker else None,
                speaker_checked=speaker_task is not None,
                signed_in=self._signed_in_name(user_id),
                system_prompt=system_prompt,
                tool_tokens=schema_tokens,
                room=caller_room,
            )
            notes, _diary_texts = await self._diary_recall_block(
                message, conversation_id, diary_owner, system_prompt, schema_tokens
            )
            volatile = "\n\n".join(p for p in (volatile, notes) if p)
            self._diary_note(conversation_id, "user", message, diary_owner, lang)
            reserved_tokens = schema_tokens + self._context_mgr.estimate_tokens(volatile)

            if volatile:
                await self._context_mgr.add_before_last_user(
                    conversation_id, {"role": "system", "content": volatile}
                )
            messages = await self._context_mgr.get_messages(
                conversation_id, system_prompt, tool_tokens=schema_tokens
            )

            # Single-pass LLM execution (no secondary grounding retry loops)
            try:
                if tool_schemas:
                    reply, tool_msgs = await self._tool_loop(
                        messages,
                        tool_schemas,
                        user_id,
                        voice_mode,
                        message,
                        device_id=device_id,
                        language=language,
                        gate=gate,
                    )
                elif gate.active:
                    response = await self._provider.async_chat_stream(
                        messages, on_delta=gate.feed
                    )
                    reply = response.reply_text()
                    tool_msgs = []
                else:
                    reply = await self._provider.async_complete(messages)
                    tool_msgs = []
            except Exception:
                await self._context_mgr.remove_last_turn(conversation_id)
                raise

            for msg in tool_msgs:
                await self._context_mgr.add_raw_message(conversation_id, msg)

            narration_stripped = _strip_narration(reply, lang=lang)
            if narration_stripped:
                stored_reply = narration_stripped
            elif reply.strip():
                stored_reply = ""
            else:
                stored_reply = reply

            stored_reply = _strip_emoji(stored_reply) or stored_reply
            stored_reply = _strip_filler_closing(stored_reply) or stored_reply
            if voice_mode:
                stored_reply = _flatten_for_voice(stored_reply) or stored_reply

            if jobs_before is not None and {job.id for job in self._scheduler.jobs()} != jobs_before:
                try:
                    await async_drop_replaced(
                        self._scheduler, jobs_before, message, stored_reply, lang
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("AI Plugin: could not cancel replaced jobs")

            media_acted = _any_media_success(tool_msgs)
            actuator_acted = _any_actuator_success(tool_msgs)
            history_reply = stored_reply

            # Keep silent TTS confirmation when actuators or media succeeded
            if (
                actuator_acted
                and voice_mode
                and not _any_unobservable_action(tool_msgs)
            ):
                stored_reply = ""
            elif media_acted:
                stored_reply = ""
            elif not stored_reply.strip():
                reply = L.template("err_no_answer", lang)
                stored_reply = reply
                history_reply = reply

            gate.flush_final(stored_reply)

            await self._context_mgr.add_turn(conversation_id, "assistant", history_reply)
            self._diary_note(conversation_id, "assistant", history_reply)
            await self._announce_thinking_refused()

            try:
                entity_ctx = self._extract_entity_context(tool_msgs)
            except Exception:  # noqa: BLE001
                entity_ctx = None
            if entity_ctx:
                self._last_entities[conversation_id] = entity_ctx

        if self._summarization_enabled:
            self._schedule_summarization(
                conversation_id, system_prompt, reserved_tokens
            )

        if (
            self._feature_on(CONF_TOOL_BROADCAST)
            and asks_what_to_broadcast(message, stored_reply, lang)
            and not _any_tool_call(tool_msgs, BROADCAST_TOOL_NAME)
        ):
            self.__dict__.setdefault("_broadcast_asked", {})[
                device_id or conversation_id
            ] = time.monotonic()

        self._note_message_question(
            stored_reply, tool_msgs, conversation_id, device_id, lang
        )

        return stored_reply

    def _schedule_summarization(
        self, conv_id: str, system_prompt: str, tool_tokens: int
    ) -> None:
        """Run summarize_if_needed as a background task under the conv lock."""

        async def _run() -> None:
            try:
                async with self._get_conv_lock(conv_id):
                    await self._context_mgr.summarize_if_needed(
                        conv_id,
                        system_prompt,
                        self._provider,
                        tool_tokens=tool_tokens,
                    )
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "AI Plugin: background summarization failed", exc_info=True
                )

        task = asyncio.get_running_loop().create_task(_run())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _is_lookup_tool(self, name: str) -> bool:
        """Tools that reach out to the network rather than HA, memory or enrollment."""
        if self._ha_local is not None and name in self._ha_local.tool_names:
            return False
        if self._mcp is not None and self._mcp.is_home_assistant_tool(name):
            return False
        return (
            name not in MEMORY_TOOL_NAMES
            and name not in SCHEDULER_TOOL_NAMES
            and name
            not in (
                VOICE_ENROLL_TOOL,
                CALENDAR_TOOL,
                CALENDAR_REMOVE_TOOL,
                TELL_TOOL,
                "move_music",
            )
        )

    def _lookup_key(self, name: str, arguments: object) -> str | None:
        """Identity of a look-up call within one turn."""
        if not self._is_lookup_tool(name):
            return None
        import json as _json

        try:
            args = _json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            return None
        return f"{name}:{' '.join(args.lower().split())}"

    async def _tool_loop(
        self,
        messages: list[dict],
        tool_schemas: list[dict],
        user_id: str | None = None,
        voice_mode: bool = False,
        user_message: str = "",
        device_id: str | None = None,
        language: str | None = None,
        gate: "_DeltaGate | None" = None,
    ) -> tuple[str, list[dict]]:
        """Run the tool loop; on slow look-ups say 'one second'."""
        wait = _WaitLine()
        try:
            return await self._run_tool_loop(
                messages,
                tool_schemas,
                user_id,
                voice_mode,
                user_message,
                device_id,
                language,
                gate,
                wait,
            )
        finally:
            if wait.timer is not None:
                wait.timer.cancel()
            if wait.task is not None and not wait.task.done():
                try:
                    await asyncio.shield(wait.task)
                except Exception:  # noqa: BLE001
                    pass

    def _arm_wait_line(
        self,
        wait: "_WaitLine",
        name: str,
        voice_mode: bool,
        device_id: str | None,
        gate: "_DeltaGate | None",
    ) -> None:
        if (
            wait.task is not None
            or not voice_mode
            or not device_id
            or self._hass is None
            or not self._is_lookup_tool(name)
        ):
            return

        def _speak() -> None:
            wait.timer = None
            if wait.task is not None:
                return
            if gate is not None:
                gate.close()
            wait.task = self._hass.async_create_task(
                satellite_speech.async_say(self._hass, device_id, "tool_wait")
            )

        wait.timer = asyncio.get_running_loop().call_later(
            TOOL_WAIT_LINE_DELAY_S, _speak
        )

    async def _run_tool_loop(
        self,
        messages: list[dict],
        tool_schemas: list[dict],
        user_id: str | None,
        voice_mode: bool,
        user_message: str,
        device_id: str | None,
        language: str | None,
        gate: "_DeltaGate | None",
        wait: "_WaitLine",
    ) -> tuple[str, list[dict]]:
        """Run the native function-calling loop: call LLM -> execute tools -> repeat."""
        import json as _json

        last_response = None
        history_messages: list[dict] = []
        seen_lookups: set[str] = set()
        room_colour: tuple[str, str] | None = None
        valid_tool_names = {
            s.get("function", {}).get("name")
            for s in tool_schemas
            if s.get("function", {}).get("name")
        }

        action_command = _is_action_command(user_message)
        for iteration in range(self._max_tool_iterations):
            cap = (
                {"max_tokens": ACTION_FIRST_REPLY_MAX_TOKENS}
                if action_command and iteration == 0 and not self._reasoning_active()
                else {}
            )
            if gate is not None and gate.active:
                gate.new_response()
                response = await self._provider.async_chat_stream(
                    messages, tool_schemas, on_delta=gate.feed, **cap
                )
            else:
                response = await self._provider.async_chat(
                    messages, tool_schemas, **cap
                )
            last_response = response

            if not response.has_tool_calls:
                reply_text = response.reply_text()
                recovered = _parse_raw_tool_call(reply_text, valid_tool_names)
                if recovered is not None:
                    if gate is not None:
                        gate.close()
                    rec_name, rec_args = recovered
                    call_id = f"call_recovered_{iteration}"
                    assistant_msg = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": rec_name,
                                    "arguments": _json.dumps(rec_args),
                                },
                            }
                        ],
                    }
                    messages.append(assistant_msg)
                    history_messages.append(assistant_msg)
                    result = await self._dispatch_tool(
                        rec_name,
                        rec_args,
                        user_id,
                        voice_mode,
                        user_message,
                        device_id=device_id,
                        language=language,
                        available_tools=valid_tool_names,
                    )
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result,
                    }
                    messages.append(tool_msg)
                    history_messages.append(tool_msg)
                    continue
                return reply_text, history_messages

            for tc in response.tool_calls:
                msg = tc.to_assistant_message()
                messages.append(msg)
                history_messages.append(msg)

            for tc in response.tool_calls:
                lookup_key = self._lookup_key(tc.name, tc.arguments)
                if lookup_key is not None and lookup_key in seen_lookups:
                    msg = tc.to_tool_result_message(
                        REPEATED_LOOKUP_RESULT.format(name=tc.name)
                    )
                    messages.append(msg)
                    history_messages.append(msg)
                    continue
                if lookup_key is not None:
                    seen_lookups.add(lookup_key)

                colour_scope = _room_colour_scope(tc.name, tc.arguments)
                if (
                    colour_scope is not None
                    and room_colour is not None
                    and colour_scope[0] == room_colour[0]
                    and colour_scope[1] != room_colour[1]
                ):
                    msg = tc.to_tool_result_message(ROOM_COLOUR_REPLACED_RESULT)
                    messages.append(msg)
                    history_messages.append(msg)
                    continue
                if colour_scope is not None and room_colour is None:
                    room_colour = colour_scope

                self._arm_wait_line(wait, tc.name, voice_mode, device_id, gate)
                result = await self._dispatch_tool(
                    tc.name,
                    tc.arguments,
                    user_id,
                    voice_mode,
                    user_message,
                    device_id=device_id,
                    language=language,
                    available_tools=valid_tool_names,
                )
                if wait.timer is not None:
                    wait.timer.cancel()
                    wait.timer = None
                if gate is not None and gate.active:
                    if tc.name in _ACTUATOR_TOOL_NAMES:
                        gate.close()
                    elif any(
                        p in (result or "").lower() for p in _MISS_RESULT_PATTERNS
                    ):
                        gate.close()
                msg = tc.to_tool_result_message(result)
                messages.append(msg)
                history_messages.append(msg)

            enroll_reply = (
                (self._enroll_replies or {}).pop(device_id, None) if device_id else None
            )
            if enroll_reply:
                if gate is not None:
                    gate.close()
                return enroll_reply, history_messages

        _lang = (language or "en").split("-")[0].lower()
        suffix = " " + L.template("note_tool_limit", _lang)
        return (
            last_response.reply_text() if last_response else ""
        ) + suffix, history_messages

    def _reasoning_active(self) -> bool:
        return bool(
            self._entry.options.get(CONF_ENABLE_THINKING, DEFAULT_ENABLE_THINKING)
        ) and getattr(self._provider, "thinking_refused", False) is not True

    async def _announce_thinking_refused(self) -> None:
        if (
            getattr(self._provider, "thinking_refused", False) is not True
            or self._thinking_announced
            or self._hass is None
        ):
            return
        self._thinking_announced = True
        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "AI Plugin",
                "message": (
                    f"The model {self._entry.options.get(CONF_MODEL, '')} cannot "
                    "reason, so 'Enable model reasoning' has no effect. Replies "
                    "work as usual. Turn the option off or choose a model that can."
                ),
                "notification_id": "ai_plugin_no_reasoning",
            },
            blocking=False,
        )

    def _confine_to_this_room(
        self, name: str, arguments: dict, user_message: str, device_id: str | None
    ) -> tuple[dict, str | None]:
        """Hold 'in here' to the satellite's room."""
        if (
            name in _ROOM_FREE_INTENTS
            and not any(_s(arguments.get(k)).strip() for k in ("name", "area", "floor"))
            and not _USER_SAID_ALL_RE.search(user_message or "")
        ):
            room = self._this_room(device_id, user_message)
            if room is not None:
                return {**arguments, "area": room}, None
        if name not in _ACTUATOR_TOOL_NAMES or not (
            says_this_room(user_message) or is_generic_lights_command(user_message)
        ):
            return arguments, None
        room = self._this_room(device_id, user_message)
        if room is None:
            return arguments, None
        device = _s(arguments.get("name")).strip()
        if device:
            if self._named_device_elsewhere(device, room):
                return arguments, (
                    f"[Not changed: {device} is not in this room. Tell the user, "
                    "and name a device that is here.]"
                )
            return arguments, None
        if "area" in arguments or name in ("set_brightness", "set_area_state"):
            return {**arguments, "area": room}, None
        return arguments, None

    async def _broadcast_turn(
        self, message: str, conversation_id: str, device_id: str | None, lang: str
    ) -> str | None:
        hass = getattr(self, "_hass", None)
        if hass is None or not self._feature_on(CONF_TOOL_BROADCAST):
            return None
        asked = self.__dict__.setdefault("_broadcast_asked", {})
        key = device_id or conversation_id
        asked_at = asked.pop(key, None)
        if asks_for_empty_broadcast(message, lang):
            asked[key] = time.monotonic()
            return L.template("broadcast_what", lang)
        if asked_at is None or time.monotonic() - asked_at > _BROADCAST_ANSWER_S:
            return None
        if cancels_broadcast(message, lang):
            return L.template("broadcast_cancelled", lang)
        result = await async_broadcast(
            hass, {"message": message}, language=lang, device_id=device_id
        )
        if result.startswith("["):
            return L.template("broadcast_failed", lang)
        return L.template("broadcast_sent", lang)

    def _note_message_question(
        self,
        reply: str,
        tool_msgs: list[dict],
        conversation_id: str,
        device_id: str | None,
        lang: str,
    ) -> None:
        hass = getattr(self, "_hass", None)
        if (
            hass is None
            or _any_tool_call(tool_msgs, TELL_TOOL)
            or not self._feature_on(CONF_TOOL_MESSAGES)
        ):
            return
        person = person_asked_about(reply, lang, people_with_phones(hass).values())
        if person is not None:
            self.__dict__.setdefault("_message_asked", {})[
                device_id or conversation_id
            ] = (person, time.monotonic())

    async def _message_answer_turn(
        self,
        message: str,
        conversation_id: str,
        device_id: str | None,
        user_id: str | None,
        lang: str,
    ) -> str | None:
        pending = self.__dict__.setdefault("_message_asked", {}).pop(
            device_id or conversation_id, None
        )
        if pending is None or time.monotonic() - pending[1] > _BROADCAST_ANSWER_S:
            return None
        if corrects_message_target(message, lang):
            return None
        if cancels_broadcast(message, lang):
            return L.template("message_cancelled", lang)
        person = pending[0]
        result = await self._dispatch_tool(
            TELL_TOOL,
            {"message": message, "person": person},
            user_id=user_id,
            device_id=device_id,
            language=lang,
        )
        if result.startswith("["):
            return L.template("message_not_sent", lang)
        return L.template("message_sent_to", lang, name=person, label=message)

    def _move_destination(
        self, area: str, user_message: str, device_id: str | None
    ) -> str:
        if self._hass is None or not moves_here(user_message):
            return area
        area_id = _caller_area_id(self._hass, device_id)
        here = ar.async_get(self._hass).async_get_area(area_id) if area_id else None
        return here.name if here else area

    def _this_room(self, device_id: str | None, user_message: str) -> str | None:
        if not device_id or getattr(self, "_hass", None) is None:
            return None
        area_id = _caller_area_id(self._hass, device_id)
        if not area_id:
            return None
        area = ar.async_get(self._hass).async_get_area(area_id)
        if area is None:
            return None
        named = (
            self._ha_local._area_named_in_message(user_message)
            if self._ha_local
            else None
        )
        if named and named.lower() != area.name.lower():
            return None
        return area.name

    def _named_device_elsewhere(self, device: str, room: str) -> bool:
        ent_reg = er.async_get(self._hass)
        dev_reg = dr.async_get(self._hass)
        area_reg = ar.async_get(self._hass)
        needle = device.lower()
        found = False
        for eid, entry in ent_reg.entities.items():
            state = self._hass.states.get(eid)
            friendly = _s(
                state.attributes.get("friendly_name") if state else ""
            ).lower()
            if needle != eid.lower() and needle not in friendly:
                continue
            found = True
            area_id = entry.area_id
            if not area_id and entry.device_id:
                dev = dev_reg.async_get(entry.device_id)
                area_id = dev.area_id if dev else None
            area = area_reg.async_get_area(area_id) if area_id else None
            if area is not None and area.name.lower() == room.lower():
                return False
        return found

    def _speaker_unrecognised(self, device_id: str | None) -> bool:
        turn = self._turn_speakers or {}
        return bool(device_id) and device_id in turn and turn[device_id] is None

    async def _dispatch_tool(
        self,
        name: str,
        arguments: dict,
        user_id: str | None = None,
        voice_mode: bool = False,
        user_message: str = "",
        device_id: str | None = None,
        language: str | None = None,
        available_tools: set[str] | None = None,
    ) -> str:
        """Route a tool call to ha_local, memory, web search, or MCP."""
        if self._hass is not None:
            self._hass.data.setdefault(DOMAIN, {}).setdefault(
                "_status_meta", {"area": None, "emotion": None, "last_tool": None}
            )["last_tool"] = name
        arguments, refused = self._confine_to_this_room(
            name, arguments, user_message, device_id
        )
        if refused:
            return refused
        builtin = name not in self._tools_off()
        if (
            not builtin
            and name.startswith("Hass")
            and self._mcp is not None
            and self._mcp.is_home_assistant_tool(name)
        ):
            return (
                f"[{name} is switched off in this assistant's settings. Use another "
                "tool for this, or tell the user it cannot be done here.]"
            )
        if builtin and self._ha_local is not None and name in self._ha_local.tool_names:
            return await self._ha_local.call_tool(
                name,
                arguments,
                device_id=device_id,
                language=language,
                user_message=user_message,
                available_tools=available_tools,
                announce_agent_id=self._timer_announce_agent_id(),
            )
        if name == VOICE_ENROLL_TOOL and self._voice_id is not None:
            return self._start_voice_enrollment(arguments, device_id, language)
        if builtin and name == RING_PHONE_TOOL and self._can_ring_phones():
            return await self._ring_phone_tool(arguments, user_id, device_id, language)
        if _IN_SCHEDULED_RUN.get() and (
            name in SCHEDULER_WRITE_TOOL_NAMES or name in _TIMER_WRITE_TOOL_NAMES
        ):
            return "[This is due now. Carry out the command now with the device tools.]"
        if (
            builtin
            and name in SCHEDULER_TOOL_NAMES
            and getattr(self, "_scheduler", None) is not None
        ):
            return await async_call_scheduler_tool(
                self._scheduler,
                self._hass,
                name,
                arguments,
                device_id=device_id,
                user_id=user_id,
                person_id=self._speaking_person(device_id, user_id),
                language=language,
                user_message=user_message,
                resolve_person=self._resolve_person,
            )
        if (
            builtin
            and name == TELL_TOOL
            and getattr(self, "_scheduler", None) is not None
        ):
            speaker = self._speaking_person(device_id, user_id)
            state = self._hass.states.get(speaker) if speaker else None
            return await async_tell(
                self._hass,
                arguments,
                language=language,
                resolve_person=self._resolve_person,
                sender_name=state.attributes.get("friendly_name") if state else None,
            )
        if builtin and name == BROADCAST_TOOL_NAME and self._hass is not None:
            return await async_broadcast(
                self._hass, arguments, language=language, device_id=device_id
            )
        if builtin and name == "move_music" and self._ha_local is not None:
            area = self._move_destination(
                arguments.get("area") or "", user_message, device_id
            )
            return await self._ha_local.move_music(area, device_id)
        if (
            builtin
            and name == CALENDAR_TOOL
            and getattr(self, "_scheduler", None) is not None
        ):
            return await async_add_event(
                self._hass,
                arguments,
                self._scheduler.now(),
                user_message=user_message,
                language=language,
            )
        if (
            builtin
            and name == CALENDAR_REMOVE_TOOL
            and getattr(self, "_scheduler", None) is not None
        ):
            return await async_remove_event(
                self._hass,
                arguments,
                self._scheduler.now(),
                user_message=user_message,
                previous_reply=_PREVIOUS_REPLY.get(),
            )
        if name in MEMORY_TOOL_NAMES and self._memory is not None:
            if name in MEMORY_WRITE_TOOLS and self._speaker_unrecognised(device_id):
                if name == "forget":
                    return (
                        "[Not removed: the speaker's voice was not recognised, so "
                        "their facts cannot be changed. Tell the user you did not "
                        "recognise their voice, so nothing was removed, and to try "
                        "again.]"
                    )
                return (
                    "[Not saved: the speaker's voice was not recognised, so nothing "
                    "can be stored for them. Tell the user you did not recognise "
                    "their voice, so nothing was saved, and to try again.]"
                )
            if (
                name == "remember"
                and user_message
                and not asks_to_remember(
                    user_message, (language or "en").split("-")[0].lower()
                )
            ):
                return (
                    "[Not saved: the user did not ask you to remember this. Do not "
                    "say it was saved or noted. Reply to what they said.]"
                )
            return await self._memory.call_tool(
                name, arguments, user_id=user_id, user_message=user_message
            )
        if name == "web_search" and self._web_search is not None:
            query = arguments.get("query", "")
            near_user = bool(arguments.get("near_user", False))
            try:
                location = await self._location.async_resolve()
            except Exception:  # noqa: BLE001
                location = {}
            return await self._web_search.async_search(
                query,
                strip_urls=voice_mode,
                near_user=near_user,
                location=location or None,
                language=(location or {}).get("language"),
            )
        if name == "browse_url" and self._browse_url is not None:
            url = arguments.get("url", "")
            return await self._browse_url.async_browse(url, strip_urls=voice_mode)
        if self._mcp is not None:
            if name == "HassLightSet":
                arguments = hass_light_set_args(arguments)
            arguments = name_without_room(arguments)
            if name == "HassListAddItem":
                items = split_list_items(arguments.get("item"))
                if len(items) > 1:
                    return "\n".join(
                        [
                            await self._mcp.call_tool(
                                name,
                                {**arguments, "item": one},
                                device_id=device_id,
                                user_id=user_id,
                            )
                            for one in items
                        ]
                    )
            arguments = widen_cover_classes(name, arguments)
            result = await self._mcp.call_tool(
                name, arguments, device_id=device_id, user_id=user_id
            )
            if result.lstrip().startswith("[") and set(
                arguments.get("device_class") or ()
            ) & WINDOW_COVERINGS:
                fallback = await self._cover_fallback(name, arguments, device_id)
                if fallback is not None:
                    return fallback
            return result
        return f"[Tool {name!r} unavailable — no handler configured]"

    def _resolve_person(self, name: str) -> str | None:
        return (
            resolve_owner(
                self._hass, name, speaker_person=None, find_person=self._find_person
            )
            if name
            else None
        )

    async def _cover_fallback(
        self, name: str, arguments: dict, device_id: str | None
    ) -> str | None:
        ha_local = getattr(self, "_ha_local", None)
        if ha_local is None or arguments.get("name"):
            return None
        area = arguments.get("area") or None
        if area is None and not device_id:
            return None
        try:
            if name == "HassSetPosition" and arguments.get("position") is not None:
                return await ha_local._set_cover_position(area, arguments["position"])
            if name in ("HassTurnOn", "HassTurnOff"):
                return await ha_local._set_area_state(
                    area,
                    "cover",
                    "turn_on" if name == "HassTurnOn" else "turn_off",
                    device_id=device_id,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("AI Plugin: cover fallback failed", exc_info=True)
        return None

    async def _diary_recall_block(
        self,
        message: str,
        conversation_id: str,
        owner: str | None,
        system_prompt: str,
        tool_tokens: int | None,
    ) -> tuple[str, list[str]]:
        diary = getattr(self, "_diary", None)
        if diary is None or _is_action_command(message) or len(message.split()) < 3:
            return "", []
        try:
            entries = diary.recall(
                message, owner, getattr(self, "_diary_share", False)
            )
        except Exception:  # noqa: BLE001
            return "", []
        if not entries:
            return "", []
        meta = self.__dict__.setdefault("_diary_convs", {}).setdefault(
            conversation_id, _new_diary_conv()
        )
        meta["known"].update(e["text"] for e in entries)
        block = format_diary_block(entries, self._local_tz(), "someone in the home")
        texts = [e["text"] for e in entries]
        if await self._context_mgr.newest_block_is(
            conversation_id, DIARY_MARKER, block, system_prompt, tool_tokens
        ):
            return "", texts
        return block, texts

    def _diary_note(
        self,
        conversation_id: str,
        role: str,
        content: str,
        owner: str | None = None,
        language: str | None = None,
    ) -> None:
        if getattr(self, "_diary", None) is None or not content:
            return
        meta = self.__dict__.setdefault("_diary_convs", {}).setdefault(
            conversation_id, _new_diary_conv()
        )
        if role == "user":
            if meta["turns"] and meta["owner"] != owner:
                meta["mixed"] = True
            meta["owner"] = owner
            meta["language"] = (language or "en").split("-")[0]
        meta["turns"].append({"role": role, "content": content})
        meta["last"] = time.monotonic()
        meta["when"] = datetime.now(timezone.utc)

    async def _async_diary_tick(self, _now=None) -> None:
        diary = getattr(self, "_diary", None)
        if diary is None:
            return
        if any(lock.locked() for lock in self._conv_locks.values()):
            return
        now = time.monotonic()
        if now - self._diary_pruned > 86400:
            self._diary_pruned = now
            await diary.async_prune(self._diary_days)
        for conv_id, meta in list(self._diary_convs.items()):
            if now - meta["last"] < DIARY_QUIET_S or not meta["turns"]:
                continue
            self._diary_convs.pop(conv_id, None)
            await self._async_write_diary(meta)
            if any(lock.locked() for lock in self._conv_locks.values()):
                return

    async def _async_write_diary(self, meta: dict) -> None:
        owner = None if meta.get("mixed") else meta.get("owner")
        hass = getattr(self, "_hass", None)
        state = hass.states.get(owner) if hass is not None and owner else None
        name = state.attributes.get("friendly_name") if state else None
        prompt = diary_prompt(
            meta["turns"], name, sorted(meta["known"]), meta.get("language") or "en"
        )
        if prompt is None:
            return
        try:
            reply = await asyncio.wait_for(self._provider.async_complete(prompt), 120)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.info("AI Plugin diary: writing notes failed: %s", exc)
            return
        notes = diary_notes(reply)
        if notes:
            await self._diary.async_add(
                notes,
                owner=owner,
                owner_name=name,
                language=meta.get("language") or "en",
                when=meta["when"],
            )

    def _local_tz(self):
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        hass = getattr(self, "_hass", None)
        name = (
            (getattr(getattr(hass, "config", None), "time_zone", None) or "")
            if hass
            else ""
        )
        try:
            return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo
        except Exception:  # noqa: BLE001
            return timezone.utc

    def _signed_in_name(self, user_id: str | None) -> str | None:
        hass = getattr(self, "_hass", None)
        person = person_for_user(hass, user_id) if hass is not None else None
        state = hass.states.get(person) if person else None
        return state.attributes.get("friendly_name") if state else None

    def _speaking_person(self, device_id: str | None, user_id: str | None) -> str | None:
        speaker = (self._turn_speakers or {}).get(device_id) if device_id else None
        return speaker or person_for_user(self._hass, user_id)

    async def _run_scheduled_command(self, job: Job) -> str:
        token = _IN_SCHEDULED_RUN.set(True)
        try:
            return await self.async_process(
                job.text,
                f"scheduled-{job.id}-{int(time.time())}",
                job.language,
                device_id=job.device_id,
                user_id=job.user_id,
            )
        finally:
            _IN_SCHEDULED_RUN.reset(token)

    def _can_ring_phones(self) -> bool:
        return getattr(self, "_hass", None) is not None and self._feature_on(
            CONF_TOOL_FIND_PHONE
        )

    def _feature_on(self, key: str) -> bool:
        entry = getattr(self, "_entry", None)
        return entry is None or key not in features_off(entry.options)

    def _tools_off(self) -> frozenset[str]:
        entry = getattr(self, "_entry", None)
        return tools_off(entry.options) if entry is not None else frozenset()

    def _find_person(self, name: str) -> str | None:
        return self._voice_id.find_person(name) if self._voice_id is not None else None

    async def _find_phone_shortcut(
        self,
        owner: str,
        user_id: str | None,
        speaker_task: asyncio.Task | None,
        lang: str,
    ) -> str | None:
        speaker = person_for_user(self._hass, user_id)
        if speaker is None and speaker_task is not None:
            try:
                identity = await speaker_task
            except Exception:  # noqa: BLE001
                identity = None
            speaker = identity.person_id if identity is not None else None
        person_id = resolve_owner(
            self._hass, owner, speaker_person=speaker, find_person=self._find_person
        )
        if person_id is None:
            return None
        return await async_ring_phone(
            self._hass, person_id, lang, own=not owner and person_id == speaker
        )

    async def _ring_phone_tool(
        self,
        arguments: dict,
        user_id: str | None,
        device_id: str | None,
        language: str | None,
    ) -> str:
        lang = (language or "en").split("-")[0].lower()
        name = str(arguments.get("person") or "").strip()
        speaker = (self._turn_speakers or {}).get(device_id) if device_id else None
        speaker = speaker or person_for_user(self._hass, user_id)
        person_id = resolve_owner(
            self._hass, name, speaker_person=speaker, find_person=self._find_person
        )
        if person_id is None:
            known = ", ".join(sorted(people_with_phones(self._hass).values()))
            heard = (
                f"The name {name!r} matches nobody. "
                if name
                else "The speaker's voice was not recognised. "
            )
            return (
                f"[No phone rang. {heard}Ask the user whose phone to ring "
                f"({known or 'nobody has a phone linked'}), then call ring_phone "
                "with that name.]"
            )
        reply = await async_ring_phone(
            self._hass, person_id, lang, own=not name and person_id == speaker
        )
        if reply is None:
            return (
                f"[No phone rang: {person_id} has no phone with the Home Assistant "
                "app linked to them. Tell the user.]"
            )
        return f"[Done. Tell the user, in their language: {reply}]"

    def _start_voice_enrollment(
        self, arguments: dict, device_id: str | None, language: str | None
    ) -> str:
        vid = self._voice_id
        if not device_id or not vid.enabled or not vid.voice_enroll_allowed:
            return "[Voice enrollment is not available here. Nothing was started.]"
        name = str(arguments.get("person") or "").strip()
        person_id = vid.find_person(name) if name else None
        if person_id is None and not name:
            person_id = (self._turn_speakers or {}).get(device_id)
        if person_id is None:
            known = ", ".join(sorted(str(p["name"]) for p in vid.persons().values()))
            heard = f"The name {name!r} matches nobody. " if name else ""
            return (
                f"[Voice enrollment NOT started. {heard}Ask the user which of these "
                f"people they are: {known or 'none'}. Then call this tool again with "
                "that name.]"
            )
        lang = (language or "en").split("-")[0].lower()
        text = vid.start_enrollment(device_id, person_id, lang)
        if self._enroll_replies is None:
            self._enroll_replies = {}
        self._enroll_replies[device_id] = text
        return f"Voice enrollment started. Say exactly: {text}"

    async def async_close(self) -> None:
        """Close provider/tool sessions and cancel background work."""
        for task in list(getattr(self, "_bg_tasks", ())):
            task.cancel()
        if self._web_search is not None:
            await self._web_search.async_close()
        if self._browse_url is not None:
            await self._browse_url.async_close()
        await self._provider.async_close()