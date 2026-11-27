"""Timers, and why they do not all go through HA's intents.

Announce mode starts every timer with a conversation_command, and HA's
own timer intents skip such timers when looking one up — so status,
cancel, pause, resume and add/remove time work on the timer manager
directly, by id. Only start_timer still goes through the intent.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.core import Context
from homeassistant.helpers import (
    intent,
)

from .exposure import _TIMER_DATA_KEY
from .formatting import _s
from ...i18n import L
from ...timer_entities import sync_timer_states

_LOGGER = logging.getLogger(__name__)


_TIMER_INTENTS = {
    "start_timer": "HassStartTimer",
    "cancel_timer": "HassCancelTimer",
    "pause_timer": "HassPauseTimer",
    "unpause_timer": "HassUnpauseTimer",
    "increase_timer": "HassIncreaseTimer",
    "decrease_timer": "HassDecreaseTimer",
    "timer_status": "HassTimerStatus",
}

_UNIT_WORDS = r"(?:hours?|hrs?|stunden?|minutes?|mins?|minuten?|seconds?|secs?|sekunden?)"

_SIMPLE_NUMS: dict[str, int] = {
    # German
    "eins": 1, "ein": 1, "eine": 1, "einen": 1, "einem": 1, "einer": 1,
    "zwei": 2, "zwo": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5,
    "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10,
    "elf": 11, "zwölf": 12, "zwoelf": 12, "dreizehn": 13, "vierzehn": 14,
    "fünfzehn": 15, "fuenfzehn": 15, "sechzehn": 16, "siebzehn": 17,
    "achtzehn": 18, "neunzehn": 19, "zwanzig": 20, "fünfundzwanzig": 25,
    "fuenfundzwanzig": 25, "dreißig": 30, "dreissig": 30, "fünfunddreißig": 35,
    "vierzig": 40, "fünfundvierzig": 45, "fuenfundvierzig": 45, "fünfzig": 50,
    "fuenfzig": 50, "sechzig": 60, "neunzig": 90,
    # English
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "twenty-five": 25, "thirty": 30, "forty": 40, "forty-five": 45,
    "fifty": 50, "sixty": 60, "ninety": 90,
}

_NUM_BEFORE_UNIT_RE = re.compile(
    rf"\b({'|'.join(sorted(_SIMPLE_NUMS, key=len, reverse=True))})\s+({_UNIT_WORDS})\b",
    re.IGNORECASE,
)

_DURATION_UNIT_RE = re.compile(
    rf"(\d+)\s*({_UNIT_WORDS})\b",
    re.IGNORECASE,
)

# One CONTIGUOUS duration expression: "1 minute 30 seconds", "1 Stunde und
# 15 Minuten". Contiguity is the disambiguator — in "add 5 minutes to the
# 10 minute timer" the two numbers belong to different things (delta vs.
# the timer's NAME) and must never be combined.
_DURATION_PHRASE_RE = re.compile(
    rf"\d+\s*{_UNIT_WORDS}\b"
    rf"(?:\s*(?:,?\s*(?:and|und)\s+|\s+)\d+\s*{_UNIT_WORDS}\b)*",
    re.IGNORECASE,
)

# Never intercept alarms, reminders, or non-timer device/media/chat turns.
_TIMER_SHORTCUT_EXCLUDE_RE = re.compile(
    r"\b(?:weck\w*|alarm\w*|erinner\w*|licht\w*|lamp\w*|musik|fernseher|tv|"
    r"heizung|thermostat|rollo\w*|jalousie\w*|steckdose\w*|ventilator\w*|"
    r"klima\w*|staubsauger|radio|spotify|sonos|lautstärke|volume|grad|prozent|"
    r"manager|gelöscht|vergessen|gestern|morgen|frage|warum|wieso|kannst\s+du)\b",
    re.IGNORECASE,
)

_COMPOUND_CLAUSE_RE = re.compile(
    r"\b(?:und\s+(?:mach|schalt|stell|sag|spiel|start|stop|dimm|dreh)|"
    r"and\s+(?:turn|switch|set|play|start|stop|tell|dim))\b",
    re.IGNORECASE,
)

_LEADING_POLITE_RE = re.compile(
    r"^(?:(?:hey|hallo|hi|ok|okay|alice|bitte|please)[,\s]+)+",
    re.IGNORECASE,
)

_TRAILING_POLITE_RE = re.compile(
    r"(?:[,\s]+(?:bitte|please|danke|thanks|jetzt|now|mal))+[\s.!?]*$",
    re.IGNORECASE,
)

_BARE_STOPWORDS = frozenset({
    "", "timer", "countdown", "ein", "eine", "einen", "einem", "einer",
    "der", "die", "das", "den", "dem", "des", "mein", "meine", "meinen",
    "a", "an", "the", "my",
})


def _normalize_duration_words(text: str) -> str:
    """Convert spoken DE/EN number words before time units into digits."""
    s = text
    s = re.sub(r"\b(?:eine\s+|einer\s+)?dreiviertel\s*stunde\b", "45 Minuten", s, flags=re.I)
    s = re.sub(r"\b(?:eine\s+|einer\s+)?viertel\s*stunde\b", "15 Minuten", s, flags=re.I)
    s = re.sub(r"\b(?:eine\s+|einer\s+)?halbe[rn]?\s+stunde\b", "30 Minuten", s, flags=re.I)
    s = re.sub(r"\b(?:eine\s+|einer\s+)?halbe[rn]?\s+minute\b", "30 Sekunden", s, flags=re.I)
    s = re.sub(r"\b(?:eineinhalb|anderthalb)\s+stunden?\b", "1 Stunde 30 Minuten", s, flags=re.I)
    s = re.sub(r"\b(?:eineinhalb|anderthalb)\s+minuten?\b", "1 Minute 30 Sekunden", s, flags=re.I)
    s = re.sub(r"\bhalf\s+an\s+hour\b", "30 minutes", s, flags=re.I)
    s = re.sub(r"\ba\s+quarter\s+(?:of\s+an\s+)?hour\b", "15 minutes", s, flags=re.I)
    return _NUM_BEFORE_UNIT_RE.sub(
        lambda m: f"{_SIMPLE_NUMS[m.group(1).lower()]} {m.group(2)}",
        s,
    )


def _parse_utterance_duration(text: str) -> dict[str, int] | None:
    """Extract an explicit h/m/s duration from a spoken utterance (EN + DE).

    Returns any of {'hours','minutes','seconds'}, else None. Used to
    override model slot-filling, which sometimes assigns the wrong unit
    (e.g. '10 seconds' -> minutes=10), and for deterministic timer shortcuts.
    """
    if not text:
        return None
    norm = _normalize_duration_words(text)
    phrases = _DURATION_PHRASE_RE.findall(norm)
    if len(phrases) != 1:
        return None
    out: dict[str, int] = {}
    for num, unit in _DURATION_UNIT_RE.findall(phrases[0]):
        u = unit.lower()
        if u.startswith(("hour", "hr", "stunde")):
            key = "hours"
        elif u.startswith("min"):
            key = "minutes"
        else:
            key = "seconds"
        try:
            out[key] = out.get(key, 0) + int(num)
        except ValueError:
            continue
    return out or None


_TIMER_WORD_SUFFIX_RE = re.compile(r"[\s\-]*(?:timer|wecker|countdown)$", re.IGNORECASE)

# "Cancel all my timers", "Alle Timer abbrechen". Narrower than the lights' own
# said-all check: "komplett" there would end two timers on "den Timer komplett
# abbrechen", where the question of which one still has to be asked.
_ALL_TIMERS_RE = re.compile(
    r"\b(?:all|every(?:thing)?|alle[srnm]?|s[äa]mtliche[srn]?|tous|toutes|"
    r"todos|todas|wszystkie|wszystkich)\b",
    re.IGNORECASE,
)


def _timer_name_key(name: object) -> str:
    """Comparable timer name: 'Nudel-Timer', 'nudel timer', 'Nudel' -> 'nudel'.

    A bare 'timer' reduces to '' — the model passes it for "the timer",
    and it then means no name at all.
    """
    return _TIMER_WORD_SUFFIX_RE.sub("", _s(name).strip().casefold()).strip()


def _clean_spoken_timer_name(raw: str) -> str:
    """Clean extracted spoken timer label while preserving capitalization."""
    name = (raw or "").strip(" \t\n.,!?:;\"'-")
    for _ in range(3):
        prev = name
        name = re.sub(
            r"^(?:einen|eine|ein|den|die|das|der|dem|des|mein\w*|my|the|a|an|"
            r"timer|countdown|für|fuer|for|namens|called|named)\s+",
            "",
            name,
            flags=re.IGNORECASE,
        ).strip(" \t\n.,!?:;\"'-")
        if name == prev:
            break
    name = re.sub(r"[\s\-]*(?:timer|countdown)$", "", name, flags=re.IGNORECASE).strip(
        " \t\n.,!?:;\"'-"
    )
    if name.casefold() in _BARE_STOPWORDS:
        return ""
    return name


def _fmt_duration(total: int) -> str:
    """Seconds -> '1 h 5 min 3 s' (zero parts dropped)."""
    hours, rest = divmod(max(0, int(total)), 3600)
    minutes, seconds = divmod(rest, 60)
    parts = [f"{hours} h"] if hours else []
    if minutes:
        parts.append(f"{minutes} min")
    if seconds or not parts:
        parts.append(f"{seconds} s")
    return " ".join(parts)


def _fmt_spoken_duration(total_seconds: int, lang: str = "de") -> str:
    """Natural TTS-friendly duration ('5 Minuten', '1 Stunde und 15 Minuten')."""
    hours, rest = divmod(max(0, int(total_seconds)), 3600)
    minutes, seconds = divmod(rest, 60)
    is_de = (lang or "en").split("-")[0].lower() == "de"
    parts: list[str] = []
    if hours:
        unit = (
            ("Stunde" if hours == 1 else "Stunden")
            if is_de
            else ("hour" if hours == 1 else "hours")
        )
        parts.append(f"{hours} {unit}")
    if minutes:
        unit = (
            ("Minute" if minutes == 1 else "Minuten")
            if is_de
            else ("minute" if minutes == 1 else "minutes")
        )
        parts.append(f"{minutes} {unit}")
    if seconds or not parts:
        unit = (
            ("Sekunde" if seconds == 1 else "Sekunden")
            if is_de
            else ("second" if seconds == 1 else "seconds")
        )
        parts.append(f"{seconds} {unit}")
    if len(parts) == 1:
        return parts[0]
    conj = " und " if is_de else " and "
    if len(parts) == 2:
        return f"{parts[0]}{conj}{parts[1]}"
    return f"{', '.join(parts[:-1])}{conj}{parts[-1]}"


def _parse_timer_shortcut(message: str) -> tuple[str, dict[str, Any]] | None:
    """Parse a deterministic timer command. Returns (action, args) or None."""
    raw = (message or "").strip()
    if not raw or len(raw) > 120:
        return None
    if _TIMER_SHORTCUT_EXCLUDE_RE.search(raw) or _COMPOUND_CLAUSE_RE.search(raw):
        return None

    text = _LEADING_POLITE_RE.sub("", raw)
    text = _TRAILING_POLITE_RE.sub("", text).strip(" \t\n.!?")
    if not text:
        return None
    norm = _normalize_duration_words(text)
    lower = norm.casefold()

    # 1. Status queries
    if re.search(
        r"^(?:wie\s+lange\s+(?:läuft|laeuft|geht|dauert|hat)?.*(?:timer|countdown)(?:\s+noch)?|"
        r"wie\s+lange\s+noch\b.*(?:timer|countdown)|"
        r"welche\s+timer\s+(?:laufen|sind\s+aktiv)|"
        r"laufen\s+(?:noch\s+)?timer|"
        r"timer[\s\-]*status|"
        r"how\s+(?:long|much\s+time)\s+(?:is\s+)?left\b.*timer|"
        r"what\s+timers?\s+are\s+running|"
        r"list\s+(?:my\s+)?timers?)$",
        lower,
    ):
        cleaned = re.sub(
            r"\b(?:wie|lange|läuft|laeuft|geht|dauert|hat|noch|welche|laufen|sind|aktiv|status|"
            r"how|long|much|time|is|left|what|timers?|are|running|list|my|on|beim|für|fuer|for)\b",
            " ",
            norm,
            flags=re.I,
        )
        name = _clean_spoken_timer_name(re.sub(r"\s+", " ", cleaned).strip())
        return ("timer_status", {"name": name} if name else {})

    # 2. Cancel timer(s)
    if re.search(r"\b(?:timer|countdown)\b", lower) and re.search(
        r"\b(?:abbrechen|abbrich|brich\w*|stopp\w*|beend\w*|lösch\w*|loesch\w*|ausmach\w*|cancel\w*|delete|clear|stop)\b",
        lower,
    ):
        if _ALL_TIMERS_RE.search(lower):
            return ("cancel_all", {})
        cleaned = re.sub(
            r"\b(?:abbrechen|abbrich|brich\w*|stopp\w*|beend\w*|lösch\w*|loesch\w*|ausmach\w*|aus|ab|cancel\w*|delete|clear|stop|bitte|please|mal)\b",
            " ",
            norm,
            flags=re.I,
        )
        name = _clean_spoken_timer_name(re.sub(r"\s+", " ", cleaned).strip())
        return ("cancel_timer", {"name": name} if name else {})

    # 3. Pause timer
    if re.search(r"\b(?:timer|countdown)\b", lower) and re.search(
        r"\b(?:pausier\w*|anhalten|halt\w*\s+.*an|pause)\b",
        lower,
    ):
        cleaned = re.sub(
            r"\b(?:pausier\w*|anhalten|halt\w*|an|pause|bitte|please|mal)\b",
            " ",
            norm,
            flags=re.I,
        )
        name = _clean_spoken_timer_name(re.sub(r"\s+", " ", cleaned).strip())
        return ("pause_timer", {"name": name} if name else {})

    # 4. Resume timer
    if re.search(r"\b(?:timer|countdown)\b", lower) and re.search(
        r"\b(?:fortsetz\w*|weiter\w*|resume|unpause|continue)\b",
        lower,
    ):
        cleaned = re.sub(
            r"\b(?:fortsetz\w*|weiterlauf\w*|weiter|lassen|lass|resume|unpause|continue|bitte|please|mal)\b",
            " ",
            norm,
            flags=re.I,
        )
        name = _clean_spoken_timer_name(re.sub(r"\s+", " ", cleaned).strip())
        return ("unpause_timer", {"name": name} if name else {})

    # 5. Extend / shorten timer
    dur = _parse_utterance_duration(norm)
    if dur and re.search(r"\b(?:timer|countdown)\b", lower):
        if re.search(
            r"\b(?:verlänger\w*|verlaenger\w*|hinzufüg\w*|hinzufueg\w*|dazu|plus|add|extend|increase)\b",
            lower,
        ):
            rem = _DURATION_PHRASE_RE.sub(" ", norm)
            rem = re.sub(
                r"\b(?:verlänger\w*|verlaenger\w*|hinzufüg\w*|hinzufueg\w*|dazu|plus|um|zu|zum|zur|add|extend|increase|to|by|more|noch|bitte|please|mal)\b",
                " ",
                rem,
                flags=re.I,
            )
            name = _clean_spoken_timer_name(re.sub(r"\s+", " ", rem).strip())
            return ("increase_timer", {**dur, **({"name": name} if name else {})})
        if re.search(
            r"\b(?:verkürz\w*|verkuerz\w*|abzieh\w*|zieh\w*|weniger|minus|remove|subtract|decrease|shorten)\b",
            lower,
        ):
            rem = _DURATION_PHRASE_RE.sub(" ", norm)
            rem = re.sub(
                r"\b(?:verkürz\w*|verkuerz\w*|abzieh\w*|zieh\w*|ab|weniger|minus|um|von|vom|remove|subtract|decrease|shorten|from|by|bitte|please|mal)\b",
                " ",
                rem,
                flags=re.I,
            )
            name = _clean_spoken_timer_name(re.sub(r"\s+", " ", rem).strip())
            return ("decrease_timer", {**dur, **({"name": name} if name else {})})

    # 6. Start timer
    if not dur or raw.endswith("?"):
        return None

    if re.search(r"\b(?:timer|countdown)\b", lower):
        body = re.sub(
            r"^(?:(?:stell(?:e)?|setz(?:e)?|start(?:e)?|mach(?:e)?|set|start)\s+(?:mir\s+|uns\s+)?)",
            "",
            norm,
            flags=re.IGNORECASE,
        ).strip()
        rem = _DURATION_PHRASE_RE.sub(" ", body)
        rem = re.sub(
            r"\b(?:auf|für|fuer|for|mit|von|in|über|ueber|namens|called|named|bitte|please|mal)\b",
            " ",
            rem,
            flags=re.I,
        )
        name = _clean_spoken_timer_name(re.sub(r"\s+", " ", rem).strip())
        if len(name.split()) > 3:
            return None
        return ("start_timer", {**dur, **({"name": name} if name else {})})

    m_short = re.match(
        rf"^(?:(?:stell(?:e)?|setz(?:e)?|start(?:e)?|mach(?:e)?)\s+(?:mir\s+)?)?"
        rf"(?P<name>[\wäöüß][\wäöüß\s\-]{{0,28}}?)\s+(?:auf|für|fuer)\s+"
        rf"(?P<dur>{_DURATION_PHRASE_RE.pattern})$",
        norm,
        re.IGNORECASE,
    )
    if m_short:
        name = _clean_spoken_timer_name(m_short.group("name"))
        if name and len(name.split()) <= 2:
            return ("start_timer", {**dur, "name": name})

    return None


def _describe_timer(timer: Any) -> str:
    """One status line for the model: name, time left, set duration, paused."""
    label = f"timer {timer.name!r}" if timer.name else "unnamed timer"
    line = f"{label}: {_fmt_duration(timer.seconds_left)} left"
    set_for = (
        3600 * (timer.start_hours or 0)
        + 60 * (timer.start_minutes or 0)
        + (timer.start_seconds or 0)
    )
    if set_for:
        line += f" (set for {_fmt_duration(set_for)})"
    if not timer.is_active:
        line += ", paused"
    return line


class TimerToolsMixin:
    """start / cancel / pause / resume / extend / status + fast shortcut."""

    async def async_try_timer_shortcut(
        self,
        message: str,
        *,
        device_id: str | None = None,
        language: str | None = None,
        announce_agent_id: str | None = None,
    ) -> str | None:
        """Handle unambiguous timer commands directly without invoking the LLM.

        Returns a short localized confirmation string when handled, or None to
        fall back to normal LLM processing.
        """
        parsed = _parse_timer_shortcut(message)
        if parsed is None:
            return None

        action, args = parsed
        lang = (language or getattr(self._hass.config, "language", "en") or "en").split("-")[0].lower()

        if action == "start_timer":
            result = await self._call_timer_intent(
                "start_timer",
                args,
                device_id=device_id,
                language=language,
                user_message=message,
                announce_agent_id=announce_agent_id,
            )
            if result.startswith("["):
                return None
            total_s = (
                3600 * int(args.get("hours", 0))
                + 60 * int(args.get("minutes", 0))
                + int(args.get("seconds", 0))
            )
            dur_text = _fmt_spoken_duration(total_s, lang)
            label = str(args.get("name") or "").strip()
            if label:
                return L.template("timer_started_named", lang, label=label, duration=dur_text)
            return L.template("timer_started", lang, duration=dur_text)

        manager = self._hass.data.get(_TIMER_DATA_KEY)
        if manager is None:
            return None

        timers = self._visible_timers(manager)
        timers.sort(key=lambda t: t.device_id != device_id)
        if not timers:
            return L.template("timer_none", lang)

        if action == "cancel_all":
            res = self._cancel_all_timers(message)
            if res.startswith("["):
                return None
            return L.template("timer_cancelled_all", lang)

        name = str(args.get("name") or "").strip()
        name_norm = _timer_name_key(name)

        def named(pool: list[Any]) -> list[Any]:
            return [t for t in pool if _timer_name_key(t.name) == name_norm]

        if action == "timer_status":
            shown = (named(timers) if name_norm else []) or timers
            lines: list[str] = []
            for t in shown:
                dur_text = _fmt_spoken_duration(int(round(t.seconds_left)), lang)
                lbl = _clean_spoken_timer_name(t.name or "")
                if t.is_active:
                    lines.append(
                        L.template("timer_status_one", lang, label=lbl, duration=dur_text)
                        if lbl
                        else L.template("timer_status_unnamed", lang, duration=dur_text)
                    )
                else:
                    lines.append(
                        L.template("timer_status_paused", lang, label=lbl, duration=dur_text)
                        if lbl
                        else L.template("timer_status_unnamed_paused", lang, duration=dur_text)
                    )
            return " ".join(lines)

        if action == "pause_timer":
            pool = [t for t in timers if t.is_active]
        elif action == "unpause_timer":
            pool = [t for t in timers if not t.is_active]
        else:
            pool = timers

        matches = named(pool) if name_norm else pool
        if name_norm and not matches and len(pool) == 1:
            matches = pool
        if len(matches) > 1 and device_id:
            own = [t for t in matches if t.device_id == device_id]
            if len(own) == 1:
                matches = own
        # Ambiguous or no match: let the LLM clarify conversationally.
        if len(matches) != 1:
            return None

        timer = matches[0]
        lbl = _clean_spoken_timer_name(timer.name or "")
        try:
            if action == "cancel_timer":
                manager.cancel_timer(timer.id)
                sync_timer_states(self._hass)
                return (
                    L.template("timer_cancelled_named", lang, label=lbl)
                    if lbl
                    else L.template("timer_cancelled", lang)
                )
            if action == "pause_timer":
                manager.pause_timer(timer.id)
                sync_timer_states(self._hass)
                return (
                    L.template("timer_paused_named", lang, label=lbl)
                    if lbl
                    else L.template("timer_paused", lang)
                )
            if action == "unpause_timer":
                manager.unpause_timer(timer.id)
                sync_timer_states(self._hass)
                return (
                    L.template("timer_resumed_named", lang, label=lbl)
                    if lbl
                    else L.template("timer_resumed", lang)
                )

            delta = (
                3600 * int(args.get("hours", 0))
                + 60 * int(args.get("minutes", 0))
                + int(args.get("seconds", 0))
            )
            if delta <= 0:
                return None
            if action == "increase_timer":
                manager.add_time(timer.id, delta)
                key_named, key_plain = "timer_added_named", "timer_added"
            else:
                manager.remove_time(timer.id, delta)
                key_named, key_plain = "timer_removed_named", "timer_removed"
            sync_timer_states(self._hass)
            delta_text = _fmt_spoken_duration(delta, lang)
            left_text = _fmt_spoken_duration(int(round(timer.seconds_left)), lang)
            return (
                L.template(key_named, lang, label=lbl, delta=delta_text, duration=left_text)
                if lbl
                else L.template(key_plain, lang, delta=delta_text, duration=left_text)
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("AI Plugin: timer shortcut failed, falling back to LLM", exc_info=True)
            return None

    async def _call_timer_intent(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        device_id: str | None = None,
        language: str | None = None,
        user_message: str = "",
        announce_agent_id: str | None = None,
    ) -> str:
        """Start a voice timer via HA's intent, or manage an existing one.

        Only ``start_timer`` goes through HA's intent. It is bound to the
        calling assist_satellite via ``device_id``; without one HA cannot ring
        the timer on the right satellite, so we refuse early with a useful
        message rather than letting the intent return an opaque error.
        Every other timer tool is handled by ``_manage_timer``.
        """
        intent_type = _TIMER_INTENTS.get(tool_name)
        if intent_type is None:
            return f"[Unknown timer tool: {tool_name!r}]"

        if tool_name == "start_timer" and not device_id and announce_agent_id:
            from ...satellite_speech import announcing_satellites  # noqa: PLC0415

            sats = announcing_satellites(self._hass)
            if sats:
                device_id = sats[0]

        if tool_name == "start_timer" and not device_id:
            return (
                "[Timer requires a satellite device — this tool is only available "
                "when the request comes from a voice pipeline. Try again from the "
                "voice satellite.]"
            )

        slots: dict[str, Any] = {}
        for key in ("name", "hours", "minutes", "seconds"):
            if key not in arguments:
                continue
            val = arguments[key]
            if val is None:
                continue
            if key == "name":
                name_val = _s(val).strip()
                if name_val:
                    slots["name"] = {"value": name_val}
            else:
                try:
                    int_val = int(val)
                except (TypeError, ValueError):
                    continue
                slots[key] = {"value": int_val}

        # Deterministic override: the spoken utterance is ground truth for the
        # duration. Small models sometimes mis-fill the unit (e.g. "10 seconds"
        # -> minutes=10), so when the utterance carries an explicit duration we
        # replace the model's h/m/s slots with the parsed value (name is kept).
        if tool_name in ("start_timer", "increase_timer", "decrease_timer"):
            parsed = _parse_utterance_duration(user_message)
            if parsed:
                for k in ("hours", "minutes", "seconds"):
                    slots.pop(k, None)
                for k, v in parsed.items():
                    slots[k] = {"value": v}
                _LOGGER.debug(
                    "AI Plugin timer: duration from utterance %r -> %s "
                    "(overrode model slots)",
                    (user_message or "")[:80], parsed,
                )

        # The words spoken decide, not a tool argument: offered "all", the model
        # sent it for a plain "Cancel the timer." too (4 of 4 replays).
        spoken_name = _s((slots.get("name") or {}).get("value"))
        if (
            tool_name == "cancel_timer"
            and _ALL_TIMERS_RE.search(user_message or "")
            and (
                not spoken_name
                or _ALL_TIMERS_RE.search(spoken_name)
                # "... in the kitchen" arrives as name="kitchen" (4 of 4 replays).
                or self._resolve_area(spoken_name) is not None
            )
        ):
            return self._cancel_all_timers(user_message)
        if tool_name != "start_timer":
            return self._manage_timer(tool_name, slots, device_id)

        if tool_name == "start_timer":
            has_duration = any(k in slots for k in ("hours", "minutes", "seconds"))
            if not has_duration:
                return (
                    "[start_timer needs a duration — provide at least one of "
                    "hours / minutes / seconds.]"
                )
            # Timer-announce mode: instead of ringing the satellite at
            # expiry, HA's TimerManager calls this agent back with the
            # sentinel command and the plugin plays the announcement via
            # mic_to_mediaplayer / assist_satellite.announce. The device
            # is never notified (no on-device LEDs/ring), and the
            # "device supports timers" requirement is bypassed.
            if announce_agent_id:
                from ...const import TIMER_DONE_SENTINEL  # noqa: PLC0415

                timer_name = (slots.get("name") or {}).get("value", "")
                slots["conversation_command"] = {
                    "value": f"{TIMER_DONE_SENTINEL} {timer_name}".strip()
                }

        handle_kwargs: dict[str, Any] = {}
        if announce_agent_id and tool_name == "start_timer":
            handle_kwargs["conversation_agent_id"] = announce_agent_id
        try:
            try:
                response = await intent.async_handle(
                    self._hass,
                    "ai_plugin",
                    intent_type,
                    slots=slots,
                    text_input=None,
                    context=Context(),
                    language=language or self._hass.config.language,
                    assistant="conversation",
                    device_id=device_id,
                    **handle_kwargs,
                )
            except TypeError:
                # Older HA cores without the conversation_agent_id kwarg —
                # the command then runs on the default agent instead.
                if not handle_kwargs:
                    raise
                response = await intent.async_handle(
                    self._hass,
                    "ai_plugin",
                    intent_type,
                    slots=slots,
                    text_input=None,
                    context=Context(),
                    language=language or self._hass.config.language,
                    assistant="conversation",
                    device_id=device_id,
                )
        except intent.IntentHandleError as exc:
            return f"[{intent_type} failed: {exc}]"
        except intent.UnknownIntent:
            return (
                f"[{intent_type} is not registered in this Home Assistant version. "
                "Voice timers require HA 2024.7 or newer.]"
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("AI Plugin timer intent %s failed", intent_type)
            return f"[{intent_type} error: {exc}]"

        sync_timer_states(self._hass)

        speech = ""
        try:
            speech_block = response.speech.get("plain", {}) if response.speech else {}
            speech = _s(speech_block.get("speech", "")).strip()
        except Exception:  # noqa: BLE001
            pass
        return speech or f"OK — {intent_type} dispatched."

    def _visible_timers(self, manager: Any) -> list[Any]:
        """This plugin's timers and plain ones, without other integrations'
        delayed commands ("turn off the lights in 10 minutes"), as in HA."""
        from ...const import TIMER_DONE_SENTINEL  # noqa: PLC0415

        return [
            t for t in list(manager.timers.values())
            if not t.conversation_command
            or t.conversation_command.startswith(TIMER_DONE_SENTINEL)
        ]

    def _cancel_all_timers(self, user_message: str) -> str:
        """Cancel every timer, or every timer in a room the user named.

        The room comes from the words spoken, because "cancel all timers in
        the kitchen" must not reach the bedroom when the model leaves it out.
        """
        manager = self._hass.data.get(_TIMER_DATA_KEY)
        if manager is None:
            return "[Voice timers are unavailable — HA's intent integration is not loaded.]"
        timers = self._visible_timers(manager)
        spoken_room = self._area_named_in_message(user_message)
        area = self._resolve_area(spoken_room) if spoken_room else None
        where = ""
        if area is not None:
            timers = [t for t in timers if t.area_id == area.id]
            where = f" in {area.name}"
        if not timers:
            return f"No timers are running{where}."
        try:
            for timer in timers:
                manager.cancel_timer(timer.id)
            sync_timer_states(self._hass)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("AI Plugin cancel all timers failed")
            return f"[cancel_timer error: {exc}]"
        count = len(timers)
        return f"Cancelled {count} timer{'' if count == 1 else 's'}{where}."

    def _manage_timer(
        self, tool_name: str, slots: dict[str, Any], device_id: str | None
    ) -> str:
        """Status / cancel / pause / unpause / add / remove on HA's TimerManager.

        HA's own timer intents cannot see the timers this plugin starts: their
        lookup (``_find_timer`` / ``_find_timers`` in intent/timers.py) skips
        every timer carrying a ``conversation_command``, and announce mode
        plants one on each. HassTimerStatus answered "no timers" and
        HassCancelTimer "not found" while the timer was running. So the timers
        are read from the manager and changed by id — which also returns the
        remaining time as text, where the intent's response carries no speech.
        """
        manager = self._hass.data.get(_TIMER_DATA_KEY)
        if manager is None:
            return "[Voice timers are unavailable — HA's intent integration is not loaded.]"

        timers = self._visible_timers(manager)
        # The calling satellite's own timers first.
        timers.sort(key=lambda t: t.device_id != device_id)
        if not timers:
            return "No timers are running."

        name = _s((slots.get("name") or {}).get("value")).strip()
        name_norm = _timer_name_key(name)

        def named(pool: list[Any]) -> list[Any]:
            return [t for t in pool if _timer_name_key(t.name) == name_norm]

        if tool_name == "timer_status":
            shown = (named(timers) if name_norm else []) or timers
            head = (
                f"No timer is named {name!r}; all timers:\n"
                if name_norm and not named(timers) else ""
            )
            return head + "\n".join(_describe_timer(t) for t in shown)

        if tool_name == "pause_timer":
            pool = [t for t in timers if t.is_active]
        elif tool_name == "unpause_timer":
            pool = [t for t in timers if not t.is_active]
        else:
            pool = timers

        matches = named(pool) if name_norm else pool
        if name_norm and not matches and len(pool) == 1:
            # "cancel the timer" with one running: the model often passes
            # a generic word as the name.
            matches = pool
        if len(matches) > 1 and device_id:
            own = [t for t in matches if t.device_id == device_id]
            if len(own) == 1:
                matches = own
        listing = "; ".join(_describe_timer(t) for t in timers)
        if not matches:
            return f"[{tool_name}: no matching timer. Timers: {listing}]"
        if len(matches) > 1:
            return (
                f"[{tool_name}: several timers match — ask the user which one. "
                f"Timers: {listing}]"
            )

        timer = matches[0]
        label = f"timer {timer.name!r}" if timer.name else "the timer"
        try:
            if tool_name == "cancel_timer":
                left = timer.seconds_left
                manager.cancel_timer(timer.id)
                sync_timer_states(self._hass)
                return f"Cancelled {label} ({_fmt_duration(left)} were left)."
            if tool_name == "pause_timer":
                manager.pause_timer(timer.id)
                sync_timer_states(self._hass)
                return f"Paused {label} with {_fmt_duration(timer.seconds_left)} left."
            if tool_name == "unpause_timer":
                manager.unpause_timer(timer.id)
                sync_timer_states(self._hass)
                return f"Resumed {label}, {_fmt_duration(timer.seconds_left)} left."

            delta = (
                3600 * int((slots.get("hours") or {}).get("value", 0))
                + 60 * int((slots.get("minutes") or {}).get("value", 0))
                + int((slots.get("seconds") or {}).get("value", 0))
            )
            if delta <= 0:
                return (
                    f"[{tool_name} needs a duration — provide at least one of "
                    "hours / minutes / seconds.]"
                )
            if tool_name == "increase_timer":
                manager.add_time(timer.id, delta)
                verb = "Added"
            else:
                manager.remove_time(timer.id, delta)
                verb = "Removed"
            sync_timer_states(self._hass)
            return (
                f"{verb} {_fmt_duration(delta)}; {label} now has "
                f"{_fmt_duration(timer.seconds_left)} left."
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("AI Plugin timer %s failed", tool_name)
            return f"[{tool_name} error: {exc}]"