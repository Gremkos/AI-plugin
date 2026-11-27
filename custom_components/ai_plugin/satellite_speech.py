"""Speak a short sentence on a satellite outside the normal reply.

Used for the voice-enrollment questions, the "one second" line while a
slow tool runs, and announce-mode timers. The sentence is rendered like
HA's own satellite announcements: with the satellite's Assist pipeline
TTS engine, language and voice, so it sounds like every other reply there.

Where it plays: satellites whose replies Mic to MediaPlayer routes to
another speaker get it through that integration (an announcement from the
satellite itself would go to its own speaker, which is not the one people
hear there); any other satellite plays it on its own media player.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_IDLE_STATES = {"idle", "paused", "off", "standby", "unknown", "unavailable"}
_POLL_S = 0.2
# Wait at most this long for the sentence to start, then to finish.
_START_WAIT_S = 4.0
_PLAY_WAIT_S = 10.0
_CHIME_WAIT_S = 3.0
# A blocking play call that took at least this long has waited for the
# playback itself (Music Assistant does). Its call returns about 2 s after
# the sentence ended (measured 2026-10-26: playing 48.97-51.74, call back at
# 53.66), so a speaker seen playing and then quiet again releases the
# reply first; a speaker never seen playing falls back on the call.
_BLOCKED_THROUGH_S = 1.0
_AUDIO_SUFFIXES = frozenset({".flac", ".mp3", ".wav", ".ogg", ".m4a", ".aac"})
_MIME_BY_SUFFIX = {
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}


def discover_timer_chimes(hass) -> list[dict[str, str]]:
    """Scan HA's local media folders for short audio files usable as chimes."""
    roots: list[tuple[str, Path]] = []
    media_dirs = getattr(getattr(hass, "config", None), "media_dirs", None) or {}
    if isinstance(media_dirs, dict) and media_dirs:
        for source_name, dir_path in media_dirs.items():
            roots.append((str(source_name), Path(dir_path)))
    else:
        default_media = Path("/media")
        if default_media.is_dir():
            roots.append(("local", default_media))

    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for source_name, root in roots:
        if not root.is_dir():
            continue
        try:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in _AUDIO_SUFFIXES:
                    continue
                rel = path.relative_to(root).as_posix()
                media_id = f"media-source://media_source/{source_name}/{rel}"
                if media_id in seen:
                    continue
                seen.add(media_id)
                found.append({"value": media_id, "label": rel})
                if len(found) >= 100:
                    return found
        except Exception:  # noqa: BLE001
            _LOGGER.debug("AI Plugin: scanning media dir %s failed", root, exc_info=True)
    return found


def _chime_content_type(media_id: str) -> str:
    suffix = Path(media_id.split("?", 1)[0]).suffix.lower()
    return _MIME_BY_SUFFIX.get(suffix, "music")


def satellite_entity(hass, device_id: str | None) -> str | None:
    """The assist_satellite entity of a device, or None."""
    if not device_id:
        return None
    try:
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        for entry in er.async_entries_for_device(er.async_get(hass), device_id):
            if entry.entity_id.startswith("assist_satellite."):
                return entry.entity_id
    except Exception:  # noqa: BLE001
        _LOGGER.debug("AI Plugin: satellite lookup failed", exc_info=True)
    return None


def satellite_in_area(hass, raw: str) -> str | None:
    """The device of a satellite in the named room, or None."""
    from homeassistant.helpers import device_registry as dr  # noqa: PLC0415
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    from .shortcuts.registry import _entry_area_id, _resolve_area  # noqa: PLC0415

    area = _resolve_area(hass, raw)
    if area is None:
        return None
    dev_reg = dr.async_get(hass)
    for entry in er.async_get(hass).entities.values():
        if entry.entity_id.startswith("assist_satellite.") and (
            _entry_area_id(hass, entry, dev_reg) == area.id
        ):
            return entry.device_id
    return None


def announcing_satellites(hass, *, besides: str | None = None) -> list[str]:
    """The device of every satellite that can announce, except ``besides``.

    The same choice HA's HassBroadcast makes, phones on a VoIP line included:
    those would have to be rung."""
    from homeassistant.components.assist_satellite import (  # noqa: PLC0415
        AssistSatelliteEntityFeature,
    )
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    devices = []
    for entry in er.async_get(hass).entities.values():
        if (
            not entry.entity_id.startswith("assist_satellite.")
            or not entry.device_id
            or entry.device_id == besides
            or not (entry.supported_features or 0) & AssistSatelliteEntityFeature.ANNOUNCE
        ):
            continue
        config = (
            hass.config_entries.async_get_entry(entry.config_entry_id)
            if entry.config_entry_id else None
        )
        if config is not None and config.domain == "voip":
            continue
        devices.append(entry.device_id)
    return devices


def satellite_pipeline(hass, device_id: str, lang: str | None = None):
    """The Assist pipeline this satellite runs (its "Assistant" select,
    "preferred" meaning HA's preferred pipeline), or None.

    With ``lang``, a pipeline of the satellite's in that language wins
    over its main one (a second wake word may run another language)."""
    try:
        from homeassistant.components import assist_pipeline  # noqa: PLC0415
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        reg = er.async_get(hass)
        selects = []
        for entry in reg.entities.values():
            if entry.device_id != device_id or not entry.entity_id.startswith("select."):
                continue
            state = hass.states.get(entry.entity_id)
            if state is None or "preferred" not in (state.attributes.get("options") or []):
                continue
            selects.append((entry, state.state))
        # Satellites with two wake words have a second pipeline select
        # ("…_2"); the first one is the main assistant.
        selects.sort(key=lambda s: str(s[0].unique_id).endswith("_2"))
        preferred = assist_pipeline.async_get_pipeline(hass, None)
        pipelines = list(assist_pipeline.async_get_pipelines(hass))
        candidates = [
            preferred if choice == "preferred"
            else next((p for p in pipelines if p.name == choice), preferred)
            for _, choice in selects
        ] or [preferred]
        if lang:
            for pipeline in candidates:
                if pipeline_lang(pipeline, "") == lang:
                    return pipeline
        return candidates[0]
    except Exception:  # noqa: BLE001
        _LOGGER.debug("AI Plugin: pipeline lookup failed", exc_info=True)
        return None


def pipeline_lang(pipeline, fallback: str = "en") -> str:
    """Two-letter language of a pipeline."""
    if pipeline is not None and getattr(pipeline, "language", None):
        return pipeline.language.split("-")[0].lower()
    return fallback


def mic2mp_for(hass, satellite: str):
    """The Mic to MediaPlayer instance routing ``satellite``'s speech, if any."""
    if not hass.services.has_service("mic_to_mediaplayer", "announce"):
        return None
    for ic in (hass.data.get("mic_to_mediaplayer") or {}).values():
        matches = getattr(ic, "matches_satellite_id", None)
        try:
            if matches is not None and matches(satellite):
                return ic
        except Exception:  # noqa: BLE001
            continue
    return None


def speech_url(hass, text: str, pipeline) -> str | None:
    """Render ``text`` with the pipeline's TTS engine, language and voice;
    returns the (relative) audio URL, or None if that is not possible."""
    if pipeline is None or not pipeline.tts_engine:
        return None
    try:
        from homeassistant.components import tts  # noqa: PLC0415

        engine = tts.async_resolve_engine(hass, pipeline.tts_engine)
        if engine is None:
            return None
        options = {"voice": pipeline.tts_voice} if pipeline.tts_voice else {}
        stream = tts.async_create_stream(
            hass, engine=engine, language=pipeline.tts_language, options=options
        )
        stream.async_set_message(text)
        return stream.url
    except Exception:  # noqa: BLE001
        _LOGGER.debug("AI Plugin: TTS rendering failed", exc_info=True)
        return None


def _device_media_player(hass, device_id: str) -> str | None:
    try:
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        for entry in er.async_entries_for_device(er.async_get(hass), device_id):
            if entry.entity_id.startswith("media_player."):
                return entry.entity_id
    except Exception:  # noqa: BLE001
        _LOGGER.debug("AI Plugin: media player lookup failed", exc_info=True)
    return None


async def _wait_until_played(hass, speaker: str, max_play_s: float = _PLAY_WAIT_S) -> None:
    """Return once ``speaker`` has played and gone quiet (bounded)."""

    def _busy() -> bool:
        state = hass.states.get(speaker)
        return state is not None and state.state not in _IDLE_STATES

    waited = 0.0
    while not _busy() and waited < _START_WAIT_S:
        await asyncio.sleep(_POLL_S)
        waited += _POLL_S
    waited = 0.0
    while _busy() and waited < max_play_s:
        await asyncio.sleep(_POLL_S)
        waited += _POLL_S


async def _wait_for_call_or_playback(
    hass,
    task: asyncio.Future,
    speaker: str | None,
    max_wait_s: float = _START_WAIT_S + _PLAY_WAIT_S,
) -> bool:
    """Wait (bounded) for the play call to return or, sooner, for ``speaker``
    to play and go quiet again. True when the speaker's own playback ended
    first; the call is then left to finish in the background."""

    def _busy() -> bool:
        state = hass.states.get(speaker) if speaker else None
        return state is not None and state.state not in _IDLE_STATES

    # Already playing (music in the room): the state cannot show when the
    # sentence ends, and a pause around it would look like the end.
    watch = not _busy()
    waited, seen_playing = 0.0, False
    while not task.done() and waited < max_wait_s:
        if watch:
            if _busy():
                seen_playing = True
            elif seen_playing:
                return True
        await asyncio.wait({task}, timeout=_POLL_S)
        waited += _POLL_S
    return False


async def _play_chime_on_speaker(hass, speaker: str, chime_media_id: str) -> None:
    """Play a short chime sound on a media_player before speaking."""
    try:
        call = (
            "media_player",
            "play_media",
            {
                "entity_id": speaker,
                "media_content_id": chime_media_id,
                "media_content_type": _chime_content_type(chime_media_id),
                "announce": True,
            },
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        task = asyncio.ensure_future(hass.services.async_call(*call, blocking=True))
        task.add_done_callback(_log_play_failure)
        played = await _wait_for_call_or_playback(
            hass, task, speaker, max_wait_s=_CHIME_WAIT_S
        )
        took = loop.time() - started
        if not played and took < 0.8:
            await asyncio.sleep(0.9)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("AI Plugin: chime playback on %s failed", speaker, exc_info=True)


async def async_announce(
    hass,
    device_id: str,
    template_key: str,
    lang: str,
    *,
    chime: str = "",
    **fmt: object,
) -> str | None:
    """Speak the i18n template ``template_key`` on the device's satellite
    without waiting for it to play; returns the sentence, or None when
    there is nowhere to play it. Never raises.

    ``chime`` may be:
      - ""         : no extra chime
      - "default"  : use the satellite's built-in preannounce sound
      - media-source://... or URL : play custom chime before speaking
    """
    try:
        from .i18n import L  # noqa: PLC0415

        satellite = satellite_entity(hass, device_id)
        if satellite is None:
            return None
        pipeline = satellite_pipeline(hass, device_id, lang)
        text = L.template(template_key, pipeline_lang(pipeline, lang), **fmt)
        url = speech_url(hass, text, pipeline)
        chime_val = (chime or "").strip()
        routed = mic2mp_for(hass, satellite)

        if routed is not None:
            speaker = getattr(routed, "media_player_entity_id", None)
            if chime_val and chime_val != "default" and speaker:
                await _play_chime_on_speaker(hass, speaker, chime_val)
            data = {"satellite_entity_id": satellite}
            if url:
                data["audio_url"] = url
            else:
                data["message"] = text
            await hass.services.async_call(
                "mic_to_mediaplayer", "announce", data, blocking=False
            )
            return text

        data: dict[str, object] = {"entity_id": satellite, "message": text}
        if url:
            from homeassistant.helpers.network import get_url  # noqa: PLC0415

            data["media_id"] = f"{get_url(hass)}{url}" if url.startswith("/") else url

        if chime_val == "default":
            data["preannounce"] = True
        elif chime_val:
            data["preannounce"] = True
            data["preannounce_media_id"] = chime_val

        try:
            await hass.services.async_call(
                "assist_satellite", "announce", data, blocking=False
            )
        except Exception:  # noqa: BLE001
            # Fallback if the satellite/HA version rejects preannounce_media_id.
            if "preannounce_media_id" not in data and "preannounce" not in data:
                raise
            data.pop("preannounce_media_id", None)
            data.pop("preannounce", None)
            if chime_val and chime_val != "default":
                speaker = _device_media_player(hass, device_id)
                if speaker:
                    await _play_chime_on_speaker(hass, speaker, chime_val)
            await hass.services.async_call(
                "assist_satellite", "announce", data, blocking=False
            )
        return text
    except Exception:  # noqa: BLE001
        _LOGGER.exception("AI Plugin: announcement on %s failed", device_id)
        return None


def _log_play_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        _LOGGER.debug("AI Plugin: playing the sentence failed", exc_info=task.exception())


async def async_say(hass, device_id: str, template_key: str) -> bool:
    """Speak the i18n template ``template_key`` on the device's satellite in
    its pipeline's language and voice, and return once it has been played.

    Returns False when there is nowhere to play it. Never raises.
    """
    try:
        from .i18n import L  # noqa: PLC0415

        satellite = satellite_entity(hass, device_id)
        if satellite is None:
            return False
        pipeline = satellite_pipeline(hass, device_id)
        text = L.template(template_key, pipeline_lang(pipeline))
        url = speech_url(hass, text, pipeline)
        routed = mic2mp_for(hass, satellite)
        if routed is not None:
            data = {"satellite_entity_id": satellite}
            if url:
                data["audio_url"] = url
            else:
                data["message"] = text
            speaker = getattr(routed, "media_player_entity_id", None)
            call = ("mic_to_mediaplayer", "announce", data)
        else:
            speaker = _device_media_player(hass, device_id)
            if speaker is None or not url:
                return False
            from homeassistant.helpers.network import get_url  # noqa: PLC0415

            call = (
                "media_player",
                "play_media",
                {
                    "entity_id": speaker,
                    "media_content_id": f"{get_url(hass)}{url}" if url.startswith("/") else url,
                    "media_content_type": "music",
                    "announce": True,
                },
            )
        loop = asyncio.get_running_loop()
        started = loop.time()
        # Blocking, but bounded: the task is left running past the bound
        # rather than cancelled, so a slow speaker still plays the line.
        task = asyncio.ensure_future(hass.services.async_call(*call, blocking=True))
        task.add_done_callback(_log_play_failure)
        played = await _wait_for_call_or_playback(hass, task, speaker)
        took = loop.time() - started
        _LOGGER.debug(
            "AI Plugin: said %r on %s (%.1f s, %s)", text, speaker or satellite, took,
            "speaker went quiet" if played else "call returned",
        )
        if speaker and not played and took < _BLOCKED_THROUGH_S:
            await _wait_until_played(hass, speaker)
        return True
    except Exception:  # noqa: BLE001
        _LOGGER.debug("AI Plugin: could not speak on %s", device_id, exc_info=True)
        return False