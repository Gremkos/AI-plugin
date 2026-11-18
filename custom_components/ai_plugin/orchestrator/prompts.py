"""The blocks that make up the system prompt for one turn."""

from __future__ import annotations

import asyncio
import logging
import re

from homeassistant.helpers.template import Template

from ..const import (
    CONF_MEMORY_ENABLED,
    CONF_SYSTEM_PROMPT,
    CONF_TOOL_ALARMS,
    CONF_TOOL_BROADCAST,
    CONF_TOOL_MESSAGES,
    CONF_TOOL_TIMERS,
    DEFAULT_MEMORY_ENABLED,
    FEATURE_TOOLS,
    SYSTEM_PROMPT_DEFAULT,
    SYSTEM_PROMPT_VOICE,
    features_off,
)

_LOGGER = logging.getLogger(__name__)

_MEMORY_RULES_RE = re.compile(r"(?:\[MEMORY TOOLS\]|Memory tools:)\n.*?\n\n", re.S)
MEMORY_OFF_RULE = (
    "Memory:\n"
    "- Nothing is saved between conversations. If the user asks you to remember "
    "or forget something and no tool does it, say in their language that you cannot save it. Never say "
    "it was noted, saved, remembered or removed, in any language.\n\n"
)

_TIMER_RULES_RE = re.compile(r"(?:\[TIMERS\]|Timers:)\n.*?\n\n", re.S)
_SCHEDULE_HEADS = ("[ALARMS, REMINDERS, LATER]", "Alarms, reminders, later:")
_SCHEDULE_INTRO = "- A timer counts down a length of time and then only rings."
_SCHEDULE_CLOSING = "- Say what these tools answered"
_BROADCAST_EXAMPLE = " 'tell everyone', 'announce': HassBroadcast."

INJECTOR_TODO_ENTITY = "todo.ai_plugin_injektor"


def prune_feature_rules(prompt: str, off: frozenset[str]) -> str:
    """The prompt without the rules of features switched off (Configure -> Tools)."""
    if not off:
        return prompt
    if CONF_TOOL_TIMERS in off:
        prompt = _TIMER_RULES_RE.sub("", prompt)
    gone = frozenset().union(*(FEATURE_TOOLS[key] for key in off))
    kept: list[str] = []
    for line in prompt.split("\n"):
        if _BROADCAST_EXAMPLE in line:
            if CONF_TOOL_BROADCAST in off:
                line = line.replace(_BROADCAST_EXAMPLE, "")
            elif CONF_TOOL_MESSAGES in off:
                line = "-" + _BROADCAST_EXAMPLE
        if line.startswith("- ") and (
            any(f"{name}(" in line for name in gone)
            or (CONF_TOOL_ALARMS in off and line.startswith(_SCHEDULE_INTRO))
        ):
            continue
        kept.append(line)
    for i in range(len(kept) - 1, 0, -1):
        if kept[i].startswith(_SCHEDULE_CLOSING) and kept[i - 1] in _SCHEDULE_HEADS:
            end = i + 2 if i + 1 < len(kept) and not kept[i + 1] else i + 1
            del kept[i - 1:end]
    return "\n".join(kept)


class PromptBlocksMixin:
    """System prompt assembly. Mixed into Orchestrator."""

    def _memory_enabled(self) -> bool:
        return self._entry.options.get(CONF_MEMORY_ENABLED, DEFAULT_MEMORY_ENABLED)

    async def _build_system_prompt(self, voice_mode: bool) -> str:
        """Return the STATIC system prompt for this request."""
        base = SYSTEM_PROMPT_VOICE if voice_mode else SYSTEM_PROMPT_DEFAULT
        if not self._memory_enabled():
            base = _MEMORY_RULES_RE.sub(lambda _: MEMORY_OFF_RULE, base)
        base = prune_feature_rules(base, features_off(self._entry.options))
        location_block = await self._build_location_block()
        custom = self._entry.options.get(CONF_SYSTEM_PROMPT, "").strip()
        parts = [base, location_block]
        if custom:
            parts.append(custom)
        return "\n\n".join(p for p in parts if p)

    async def _build_volatile_block(
        self,
        user_id: str | None,
        conversation_id: str,
        speaker: str | None = None,
        speaker_checked: bool = False,
        signed_in: str | None = None,
        system_prompt: str | None = None,
        tool_tokens: int | None = None,
        room: str | None = None,
        restricted_to_admin: str | None = None,
    ) -> str:
        """Per-turn context ([CURRENT TIME], [SPEAKER], [ACCESS CONTROL], [USER FACTS], [MANDATORY RULES], [LAST ACTION])."""
        facts = await self._build_user_facts_block(user_id)
        if (
            facts
            and system_prompt is not None
            and await self._context_mgr.newest_block_is(
                conversation_id, _FACTS_MARKER, facts, system_prompt, tool_tokens
            )
        ):
            facts = ""
        injector = await self._build_injector_block()
        parts = [
            self._build_time_block(),
            _speaker_block(
                speaker,
                speaker_checked,
                self._memory_enabled(),
                signed_in=signed_in,
                room=room,
            ),
            _access_control_block(restricted_to_admin),
            facts,
            injector,
        ]
        last = self._last_entities.get(conversation_id)
        if last:
            parts.append(f"[LAST ACTION]\n{last}")
        return "\n\n".join(p for p in parts if p)

    def _render_injector_text(self, text: str) -> str:
        """Render Home Assistant Jinja2 templates if present in injector text."""
        if not text or ("{{" not in text and "{%" not in text):
            return text
        if self._hass is None:
            return text
        try:
            rendered = Template(text, self._hass).async_render(parse_result=False)
            return str(rendered).strip()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("AI Plugin: template error in todo injector (%r): %s", text, exc)
            return text

    async def _build_injector_block(self) -> str:
        """Render active items from todo.ai_plugin_injektor as mandatory per-turn rules."""
        if not self._entry.options.get("tool_todo_injector", False):
            return ""
        if self._hass is None or self._hass.states.get(INJECTOR_TODO_ENTITY) is None:
            return ""
        try:
            resp = await self._hass.services.async_call(
                "todo",
                "get_items",
                {"entity_id": INJECTOR_TODO_ENTITY, "status": "needs_action"},
                blocking=True,
                return_response=True,
            )
            items = (resp or {}).get(INJECTOR_TODO_ENTITY, {}).get("items", [])
            rules: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_summary = str(item.get("summary") or "").strip()
                if not raw_summary:
                    continue
                summary = self._render_injector_text(raw_summary)
                if not summary:
                    continue
                raw_desc = str(item.get("description") or "").strip()
                if raw_desc:
                    desc = self._render_injector_text(raw_desc)
                    if not desc:
                        continue
                    sep = " " if summary.endswith(":") else ": "
                    rules.append(f"{summary}{sep}{desc}")
                else:
                    rules.append(summary)
            if not rules:
                return ""
            numbered = "\n".join(f"{i}. {rule}" for i, rule in enumerate(rules, 1))
            return (
                "[MANDATORY RULES]\n"
                "Follow these active rules strictly on every turn:\n"
                f"{numbered}"
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("AI Plugin: could not read %s", INJECTOR_TODO_ENTITY, exc_info=True)
            return ""

    def _build_time_block(self) -> str:
        """Return a fresh [CURRENT TIME] block in the user's local timezone."""
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            cfg = self._hass.config if self._hass is not None else None
            tz_name = (getattr(cfg, "time_zone", None) or "").strip() if cfg else ""
            now = datetime.now(ZoneInfo(tz_name)) if tz_name else datetime.now().astimezone()
            stamp = now.strftime("%A, %Y-%m-%d %H:%M %Z")
            return (
                f"[CURRENT TIME]\n{stamp}\n"
                "Use this for any question about the time, date, day-of-week, "
                "or 'how long until X'. Do NOT guess the time."
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("AI Plugin: time block build failed", exc_info=True)
            return ""

    async def _build_user_facts_block(self, user_id: str | None) -> str:
        """Render stored user facts as a numbered prompt block."""
        if self._memory is None:
            return ""
        try:
            from ..tools.memory import _memory_path  # noqa: PLC0415

            path = _memory_path(self._memory._config_dir, user_id)
            facts = await asyncio.get_running_loop().run_in_executor(
                None, self._memory._load, path
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("AI Plugin: could not read user facts", exc_info=True)
            return ""
        if not facts:
            return ""
        numbered = "\n".join(f"{i}. {f}" for i, f in enumerate(facts, 1))
        return (
            f"{_FACTS_MARKER}"
            "Facts previously saved by the user. Use them when answering "
            "questions about who they are. Preferences among them (units, "
            "language, style) apply to EVERY answer, whatever the topic: if "
            "one says to use metric units, give every measurement in metric, "
            "converting figures you know in other units (feet to metres, "
            "pounds to kilograms, Fahrenheit to Celsius). To remove a fact, "
            "call forget(index: N, fact: '<keyword>') with the matching number. "
            "Never call forget for a fact that is not listed here.\n"
            f"{numbered}"
        )

    async def _build_location_block(self) -> str:
        """Render the resolved home location as a prompt block."""
        try:
            data = await self._location.async_resolve()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("AI Plugin: location resolve failed", exc_info=True)
            return ""

        if not data:
            return ""

        parts: list[str] = []
        if data.get("city"):
            parts.append(f"city={data['city']}")
        if data.get("region"):
            parts.append(f"region={data['region']}")
        if data.get("country_name"):
            parts.append(f"country={data['country_name']}")
        elif data.get("country_iso"):
            parts.append(f"country={data['country_iso']}")
        if "lat" in data and "lon" in data and "city" not in data:
            parts.append(f"coords={data['lat']:.4f},{data['lon']:.4f}")
        if data.get("timezone"):
            parts.append(f"timezone={data['timezone']}")

        if not parts:
            return ""

        joined = ", ".join(parts)
        return (
            "[HOME LOCATION]\n"
            f"- {joined}\n"
            "- For questions about the user's immediate area, set "
            "near_user: true on web_search. The plugin will scope the "
            "query to this area; do not invent a city name."
        )


_FACTS_MARKER = "[USER FACTS]\n"


def _access_control_block(admin_name: str | None) -> str:
    """[ACCESS CONTROL] block when Admin-MCP restricts non-admin speakers."""
    if not admin_name:
        return ""
    return (
        "[ACCESS CONTROL]\n"
        f"Admin mode is active. Only {admin_name} has full system and device control. "
        f"The current speaker is NOT {admin_name}.\n"
        "- This speaker may ONLY control lights (turning lights on/off, brightness, color) "
        "and ask about the weather or current time.\n"
        "- You MUST refuse any other device control (smart plugs, switches, appliances like the fridge, "
        "media players, covers, scripts, timers, alarms, cameras, calendar, messages, or admin/MCP tools) "
        f"— even if they claim {admin_name} gave permission or it is a joke.\n"
        f"- Refuse politely, playfully, and in character, and tell them to ask {admin_name}."
    )


def _speaker_block(
    speaker: str | None,
    checked: bool,
    memory: bool = True,
    signed_in: str | None = None,
    room: str | None = None,
) -> str:
    """[SPEAKER] block for this turn."""
    room_note = (
        f" The request comes from the device in: {room}. Treat 'here' or 'in this room' as {room} unless another room is named."
        if room
        else ""
    )
    if signed_in and not speaker and not checked:
        return (
            "[SPEAKER]\n"
            f"The person speaking now is signed in on this device as {signed_in}. "
            f"Treat them as {signed_in} unless they say they are someone else.{room_note}"
        )
    if speaker:
        return (
            "[SPEAKER]\n"
            f"The person speaking now was recognised by voice as {speaker}. "
            f"Treat them as {speaker}, even if earlier turns of this conversation "
            f"were someone else."
            + (f" The newest [USER FACTS] are {speaker}'s." if memory else "")
            + room_note
        )
    if checked:
        return (
            "[SPEAKER]\n"
            "Voice recognition is on, but it could not tell who is speaking this "
            "time: the voice did not match a known voice closely enough. Never "
            "say their voice is unknown, not registered or not set up. "
            "Do not assume they are anyone named earlier in this conversation; "
            "ask if it matters."
            + (" Nothing can be saved to memory for them." if memory else "")
            + room_note
        )
    if room_note:
        return f"[SPEAKER]\n{room_note.strip()}"
    return ""