"""The voluptuous schemas both flows show, and the checks behind them.

Config flow and options flow ask the same questions in the same shape;
building the schema in one place is what keeps them from drifting apart.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.helpers import selector

from ..const import (
    BACKEND_BRAVE,
    BACKEND_DUCKDUCKGO,
    BACKEND_SEARXNG,
    BACKEND_TAVILY,
    CONF_AUDIO_INPUT_ENABLED,
    CONF_AUTO_DUCKING,
    CONF_BRAVE_API_KEY,
    CONF_CAMERA_VISION_ENABLED,
    CONF_CONTEXT_WINDOW,
    CONF_CONTINUE_CONVERSATION,
    CONF_CONVERSATION_MEMORY,
    CONF_DIARY_DAYS,
    CONF_DIARY_ENABLED,
    CONF_DIARY_SHARE,
    CONF_ENABLE_THINKING,
    CONF_FEEDBACK_LOOP_DEVICES,
    CONF_FOLLOW_UP_DELAY,
    CONF_KEEP_ALIVE,
    CONF_LOCATION_BIAS,
    CONF_LOCATION_ENTITY,
    CONF_MAX_RESULTS,
    CONF_MAX_TOKENS,
    CONF_MAX_TOOL_ITERATIONS,
    CONF_MEMORY_ENABLED,
    CONF_OFFLINE_FALLBACK,
    CONF_PROMPT_WARMUP,
    CONF_PRUNE_TOOL_SCHEMAS,
    CONF_RESPONSE_TIMEOUT,
    CONF_SELF_ECHO_FILTER,
    CONF_SUMMARIZATION_ENABLED,
    CONF_SYSTEM_PROMPT,
    CONF_TAVILY_API_KEY,
    CONF_TEMPERATURE,
    CONF_TIMER_ANNOUNCE,
    CONF_TOP_P,
    CONF_VOICE_MODE,
    CONF_WEB_SEARCH_BACKEND,
    CONF_WEB_SEARCH_ENABLED,
    CONF_WORKER_STATUS_ENTITY,
    CUSTOM_PROMPT_TEMPLATE,
    DEFAULT_AUDIO_INPUT_ENABLED,
    DEFAULT_AUTO_DUCKING,
    DEFAULT_CAMERA_VISION_ENABLED,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_CONVERSATION_MEMORY,
    DEFAULT_DIARY_DAYS,
    DEFAULT_DIARY_ENABLED,
    DEFAULT_DIARY_SHARE,
    DEFAULT_ENABLE_THINKING,
    DEFAULT_FOLLOW_UP_DELAY,
    DEFAULT_KEEP_ALIVE,
    DEFAULT_LOCATION_BIAS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_MAX_TOOL_ITERATIONS,
    DEFAULT_MEMORY_ENABLED,
    DEFAULT_OFFLINE_FALLBACK,
    DEFAULT_PROMPT_WARMUP,
    DEFAULT_PRUNE_TOOL_SCHEMAS,
    DEFAULT_RESPONSE_TIMEOUT,
    DEFAULT_SELF_ECHO_FILTER,
    DEFAULT_SUMMARIZATION_ENABLED,
    DEFAULT_TIMER_ANNOUNCE,
    DEFAULT_VOICE_MODE,
    DEFAULT_WEB_SEARCH_BACKEND,
    FEATURE_TOOLS,
    FOLLOW_UP_ALWAYS,
    FOLLOW_UP_AUTO,
    FOLLOW_UP_NEVER,
    MAX_CONVERSATION_MEMORY,
    MAX_DIARY_DAYS,
    follow_up_mode,
    CONF_EPHEMERAL_FILTER_ENABLED,
    CONF_EPHEMERAL_PREFIXES,
    DEFAULT_EPHEMERAL_FILTER_ENABLED,
    DEFAULT_EPHEMERAL_PREFIXES,
    CONF_TIMER_ANNOUNCE_CHIME,
    DEFAULT_TIMER_ANNOUNCE_CHIME,
    TIMER_CHIME_DEFAULT,
)

_LOGGER = logging.getLogger(__name__)


def _is_valid_url(url: str) -> bool:
    """Return True if url has a valid http/https scheme and netloc."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:  # noqa: BLE001
        return False


def _web_search_schema(current: dict[str, Any]) -> vol.Schema:
    """Build the web search vol.Schema with current values as defaults."""
    return vol.Schema(
        {
            vol.Required(
                CONF_WEB_SEARCH_ENABLED,
                default=current.get(CONF_WEB_SEARCH_ENABLED, False),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_WEB_SEARCH_BACKEND,
                default=current.get(CONF_WEB_SEARCH_BACKEND, DEFAULT_WEB_SEARCH_BACKEND),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {
                            "value": BACKEND_BRAVE,
                            "label": "Brave Search (recommended, API key required)",
                        },
                        {
                            "value": BACKEND_TAVILY,
                            "label": "Tavily (API key required)",
                        },
                        {
                            "value": BACKEND_SEARXNG,
                            "label": "SearXNG (self-hosted)",
                        },
                        {
                            "value": BACKEND_DUCKDUCKGO,
                            "label": "DuckDuckGo (free, may be unreliable)",
                        },
                    ],
                )
            ),
            vol.Optional(
                CONF_BRAVE_API_KEY,
                default=current.get(CONF_BRAVE_API_KEY, ""),
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(
                CONF_TAVILY_API_KEY,
                default=current.get(CONF_TAVILY_API_KEY, ""),
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(
                CONF_MAX_RESULTS,
                default=current.get(CONF_MAX_RESULTS, DEFAULT_MAX_RESULTS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=20,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _memory_schema(current: dict[str, Any]) -> vol.Schema:
    """What the assistant remembers: saved facts, the conversation on a
    satellite, and the diary of earlier conversations."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_MEMORY_ENABLED,
                default=current.get(CONF_MEMORY_ENABLED, DEFAULT_MEMORY_ENABLED),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_CONVERSATION_MEMORY,
                default=current.get(CONF_CONVERSATION_MEMORY, DEFAULT_CONVERSATION_MEMORY),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=MAX_CONVERSATION_MEMORY,
                    step=1,
                    unit_of_measurement="min",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_DIARY_ENABLED,
                default=current.get(CONF_DIARY_ENABLED, DEFAULT_DIARY_ENABLED),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_DIARY_DAYS,
                default=current.get(CONF_DIARY_DAYS, DEFAULT_DIARY_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=MAX_DIARY_DAYS,
                    step=1,
                    unit_of_measurement="d",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_DIARY_SHARE,
                default=current.get(CONF_DIARY_SHARE, DEFAULT_DIARY_SHARE),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_EPHEMERAL_FILTER_ENABLED,
                default=current.get(
                    CONF_EPHEMERAL_FILTER_ENABLED, DEFAULT_EPHEMERAL_FILTER_ENABLED
                ),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_EPHEMERAL_PREFIXES,
                default=current.get(
                    CONF_EPHEMERAL_PREFIXES, DEFAULT_EPHEMERAL_PREFIXES
                ),
            ): selector.TextSelector(),
        }
    )


def _tools_schema(current: dict[str, Any]) -> vol.Schema:
    """The built-in features, each one on or off (Configure -> Tools)."""
    schema: dict[Any, Any] = {
        vol.Optional(key, default=current.get(key, True)): selector.BooleanSelector()
        for key in FEATURE_TOOLS
    }
    schema[
        vol.Optional(
            "tool_todo_injector",
            default=current.get("tool_todo_injector", False),
        )
    ] = selector.BooleanSelector()
    return vol.Schema(schema)


def _llm_settings_schema(current: dict[str, Any]) -> vol.Schema:
    """Model prompt, context window, reasoning, and sampling parameters."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_SYSTEM_PROMPT,
                default=current.get(CONF_SYSTEM_PROMPT, CUSTOM_PROMPT_TEMPLATE),
            ): selector.TemplateSelector(),
            vol.Optional(
                CONF_CONTEXT_WINDOW,
                default=current.get(CONF_CONTEXT_WINDOW, DEFAULT_CONTEXT_WINDOW),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=512,
                    max=131072,
                    step=512,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_SUMMARIZATION_ENABLED,
                default=current.get(CONF_SUMMARIZATION_ENABLED, DEFAULT_SUMMARIZATION_ENABLED),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_ENABLE_THINKING,
                default=current.get(CONF_ENABLE_THINKING, DEFAULT_ENABLE_THINKING),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_TEMPERATURE,
                description={"suggested_value": current.get(CONF_TEMPERATURE)},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0,
                    max=2.0,
                    step=0.1,
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Optional(
                CONF_TOP_P,
                description={"suggested_value": current.get(CONF_TOP_P)},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0,
                    max=1.0,
                    step=0.05,
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Optional(
                CONF_MAX_TOKENS,
                description={"suggested_value": current.get(CONF_MAX_TOKENS)},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=8192,
                    step=256,
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Optional(
                CONF_KEEP_ALIVE,
                description={"suggested_value": current.get(CONF_KEEP_ALIVE, DEFAULT_KEEP_ALIVE)},
            ): selector.TextSelector(),
            vol.Optional(
                CONF_RESPONSE_TIMEOUT,
                default=current.get(CONF_RESPONSE_TIMEOUT, DEFAULT_RESPONSE_TIMEOUT),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5,
                    max=120,
                    step=5,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _vision_audio_schema(
    current: dict[str, Any],
    chime_options: list[dict[str, str]] | None = None,
) -> vol.Schema:
    """Voice behavior, audio ducking, camera vision, and multimodal audio input."""
    chime_choices: list[dict[str, str]] = [
        {"value": "", "label": "No chime (speech only)"},
        {"value": TIMER_CHIME_DEFAULT, "label": "Satellite default chime (preannounce)"},
        *(chime_options or []),
    ]
    saved_chime = str(
        current.get(CONF_TIMER_ANNOUNCE_CHIME, DEFAULT_TIMER_ANNOUNCE_CHIME) or ""
    ).strip()
    if saved_chime and all(opt["value"] != saved_chime for opt in chime_choices):
        chime_choices.append({"value": saved_chime, "label": saved_chime})

    return vol.Schema(
        {
            vol.Optional(
                CONF_CAMERA_VISION_ENABLED,
                default=current.get(CONF_CAMERA_VISION_ENABLED, DEFAULT_CAMERA_VISION_ENABLED),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_AUDIO_INPUT_ENABLED,
                default=current.get(CONF_AUDIO_INPUT_ENABLED, DEFAULT_AUDIO_INPUT_ENABLED),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_VOICE_MODE,
                default=current.get(CONF_VOICE_MODE, DEFAULT_VOICE_MODE),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_CONTINUE_CONVERSATION,
                default=follow_up_mode(current),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": FOLLOW_UP_ALWAYS, "label": "Always"},
                        {"value": FOLLOW_UP_AUTO, "label": "Let the AI decide (when it asks a question)"},
                        {"value": FOLLOW_UP_NEVER, "label": "Never"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_FOLLOW_UP_DELAY,
                default=current.get(CONF_FOLLOW_UP_DELAY, DEFAULT_FOLLOW_UP_DELAY),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=10,
                    step=0.5,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_AUTO_DUCKING,
                default=current.get(CONF_AUTO_DUCKING, DEFAULT_AUTO_DUCKING),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_SELF_ECHO_FILTER,
                default=current.get(CONF_SELF_ECHO_FILTER, DEFAULT_SELF_ECHO_FILTER),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_TIMER_ANNOUNCE,
                default=current.get(CONF_TIMER_ANNOUNCE, DEFAULT_TIMER_ANNOUNCE),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_TIMER_ANNOUNCE_CHIME,
                default=saved_chime,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=chime_choices,
                    custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_FEEDBACK_LOOP_DEVICES,
                description={"suggested_value": current.get(CONF_FEEDBACK_LOOP_DEVICES)},
            ): selector.DeviceSelector(
                selector.DeviceSelectorConfig(multiple=True)
            ),
        }
    )


def _system_fallback_schema(current: dict[str, Any]) -> vol.Schema:
    """Offline fallback, tool limits, prompt caching, and location settings."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_OFFLINE_FALLBACK,
                default=current.get(CONF_OFFLINE_FALLBACK, DEFAULT_OFFLINE_FALLBACK),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_WORKER_STATUS_ENTITY,
                description={"suggested_value": current.get(CONF_WORKER_STATUS_ENTITY)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(
                CONF_MAX_TOOL_ITERATIONS,
                default=current.get(CONF_MAX_TOOL_ITERATIONS, DEFAULT_MAX_TOOL_ITERATIONS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=10,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_PROMPT_WARMUP,
                default=current.get(CONF_PROMPT_WARMUP, DEFAULT_PROMPT_WARMUP),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_PRUNE_TOOL_SCHEMAS,
                default=current.get(CONF_PRUNE_TOOL_SCHEMAS, DEFAULT_PRUNE_TOOL_SCHEMAS),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_LOCATION_BIAS,
                default=current.get(CONF_LOCATION_BIAS, DEFAULT_LOCATION_BIAS),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_LOCATION_ENTITY,
                description={"suggested_value": current.get(CONF_LOCATION_ENTITY)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["zone", "device_tracker", "person"],
                )
            ),
        }
    )


def _advanced_schema(current: dict[str, Any], hass: Any) -> vol.Schema:
    """Build the combined advanced settings vol.Schema (used by initial config flow)."""
    schema: dict[Any, Any] = {
        vol.Optional(
            CONF_SYSTEM_PROMPT,
            default=current.get(CONF_SYSTEM_PROMPT, CUSTOM_PROMPT_TEMPLATE),
        ): selector.TemplateSelector(),
        vol.Optional(
            CONF_CONTEXT_WINDOW,
            default=current.get(CONF_CONTEXT_WINDOW, DEFAULT_CONTEXT_WINDOW),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=512,
                max=131072,
                step=512,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_SUMMARIZATION_ENABLED,
            default=current.get(CONF_SUMMARIZATION_ENABLED, DEFAULT_SUMMARIZATION_ENABLED),
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_VOICE_MODE,
            default=current.get(CONF_VOICE_MODE, DEFAULT_VOICE_MODE),
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_CONTINUE_CONVERSATION,
            default=follow_up_mode(current),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    {"value": FOLLOW_UP_ALWAYS, "label": "Always"},
                    {"value": FOLLOW_UP_AUTO, "label": "Let the AI decide (when it asks a question)"},
                    {"value": FOLLOW_UP_NEVER, "label": "Never"},
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(
            CONF_FOLLOW_UP_DELAY,
            default=current.get(CONF_FOLLOW_UP_DELAY, DEFAULT_FOLLOW_UP_DELAY),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=10,
                step=0.5,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_AUTO_DUCKING,
            default=current.get(CONF_AUTO_DUCKING, DEFAULT_AUTO_DUCKING),
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_ENABLE_THINKING,
            default=current.get(CONF_ENABLE_THINKING, DEFAULT_ENABLE_THINKING),
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_MAX_TOOL_ITERATIONS,
            default=current.get(CONF_MAX_TOOL_ITERATIONS, DEFAULT_MAX_TOOL_ITERATIONS),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=10,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_RESPONSE_TIMEOUT,
            default=current.get(CONF_RESPONSE_TIMEOUT, DEFAULT_RESPONSE_TIMEOUT),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=5,
                max=120,
                step=5,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_KEEP_ALIVE,
            description={"suggested_value": current.get(CONF_KEEP_ALIVE, DEFAULT_KEEP_ALIVE)},
        ): selector.TextSelector(),
        vol.Optional(
            CONF_PROMPT_WARMUP,
            default=current.get(CONF_PROMPT_WARMUP, DEFAULT_PROMPT_WARMUP),
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_PRUNE_TOOL_SCHEMAS,
            default=current.get(CONF_PRUNE_TOOL_SCHEMAS, DEFAULT_PRUNE_TOOL_SCHEMAS),
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_TEMPERATURE,
            description={"suggested_value": current.get(CONF_TEMPERATURE)},
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0,
                max=2.0,
                step=0.1,
                mode=selector.NumberSelectorMode.SLIDER,
            )
        ),
        vol.Optional(
            CONF_TOP_P,
            description={"suggested_value": current.get(CONF_TOP_P)},
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0,
                max=1.0,
                step=0.05,
                mode=selector.NumberSelectorMode.SLIDER,
            )
        ),
        vol.Optional(
            CONF_MAX_TOKENS,
            description={"suggested_value": current.get(CONF_MAX_TOKENS)},
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=8192,
                step=256,
                mode=selector.NumberSelectorMode.SLIDER,
            )
        ),
        vol.Optional(
            CONF_SELF_ECHO_FILTER,
            default=current.get(CONF_SELF_ECHO_FILTER, DEFAULT_SELF_ECHO_FILTER),
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_TIMER_ANNOUNCE,
            default=current.get(CONF_TIMER_ANNOUNCE, DEFAULT_TIMER_ANNOUNCE),
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_FEEDBACK_LOOP_DEVICES,
            description={"suggested_value": current.get(CONF_FEEDBACK_LOOP_DEVICES)},
        ): selector.DeviceSelector(
            selector.DeviceSelectorConfig(multiple=True)
        ),
        vol.Optional(
            CONF_LOCATION_BIAS,
            default=current.get(CONF_LOCATION_BIAS, DEFAULT_LOCATION_BIAS),
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_LOCATION_ENTITY,
            description={"suggested_value": current.get(CONF_LOCATION_ENTITY)},
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["zone", "device_tracker", "person"],
            )
        ),
    }
    return vol.Schema(schema)


def _validate_advanced_input(user_input: dict[str, Any]) -> dict[str, str]:
    """Per-field validation for the advanced step."""
    return {}