"""Post-install Configure: modular sub-menus for every setting."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from ..const import (
    BACKEND_SEARXNG,
    CONF_API_KEY,
    CONF_AUDIO_INPUT_ENABLED,
    CONF_AUTO_DUCKING,
    CONF_BASE_URL,
    CONF_CAMERA_VISION_ENABLED,
    CONF_CONTEXT_WINDOW,
    CONF_CONVERSATION_MEMORY,
    CONF_DIARY_DAYS,
    CONF_ENABLE_THINKING,
    CONF_FEEDBACK_LOOP_DEVICES,
    CONF_KEEP_ALIVE,
    CONF_LOCATION_ENTITY,
    CONF_MAX_RESULTS,
    CONF_MAX_TOKENS,
    CONF_MAX_TOOL_ITERATIONS,
    CONF_MCP_SERVERS,
    CONF_MODEL,
    CONF_RESPONSE_TIMEOUT,
    CONF_SEARXNG_URL,
    CONF_TEMPERATURE,
    CONF_TOP_P,
    CONF_WEB_SEARCH_BACKEND,
    CONF_WEB_SEARCH_ENABLED,
    CONF_WORKER_STATUS_ENTITY,
    DEFAULT_BASE_URL,
    DEFAULT_WEB_SEARCH_BACKEND,
    DOMAIN,
    ERROR_INVALID_URL,
    ERROR_MODEL_NO_TOOLS,
    ERROR_MODEL_REQUIRED,
    CONF_EPHEMERAL_FILTER_ENABLED,
    CONF_EPHEMERAL_PREFIXES,
    DEFAULT_EPHEMERAL_PREFIXES,
    CONF_TIMER_ANNOUNCE_CHIME,
)
from ..satellite_speech import discover_timer_chimes
from ..exceptions import CannotConnect
from ..option_values import merge_advanced_options
from ..providers.openai_compat import (
    async_fetch_models,
    async_show_model,
    model_capabilities,
    model_context_length,
    model_limit_errors,
)
from .mcp_steps import MCPServerStepsMixin
from .schemas import (
    _advanced_schema,
    _is_valid_url,
    _llm_settings_schema,
    _memory_schema,
    _system_fallback_schema,
    _tools_schema,
    _validate_advanced_input,
    _vision_audio_schema,
    _web_search_schema,
)
from .voice_id_steps import VoiceIDStepsMixin

_LOGGER = logging.getLogger(__name__)


class AIPluginOptionsFlow(MCPServerStepsMixin, VoiceIDStepsMixin, config_entries.OptionsFlow):
    """Options flow with clean topic sub-menus."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._options = dict(config_entry.options)
        self._models: list[str] | None = None
        self._pending_mcp: list[dict] = list(
            config_entry.options.get(CONF_MCP_SERVERS, [])
        )
        self._editing_idx: int | None = None
        self._preset_key: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Show the top-level categorized menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "provider_settings",
                "llm_settings",
                "tools",
                "vision_audio",
                "web_search",
                "memory",
                "voice_id",
                "mcp_servers",
                "system_fallback",
            ],
        )

    # ── 1. Provider settings ─────────────────────────────────────────────────

    async def async_step_provider_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Edit provider URL -> fetch models -> model selection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].strip().rstrip("/")
            if not _is_valid_url(base_url):
                errors[CONF_BASE_URL] = ERROR_INVALID_URL
            else:
                self._options[CONF_BASE_URL] = base_url
                api_key = str(user_input.get(CONF_API_KEY, "")).strip()
                if api_key:
                    self._options[CONF_API_KEY] = api_key
                elif CONF_API_KEY in self._options:
                    del self._options[CONF_API_KEY]
                try:
                    self._models = await async_fetch_models(
                        base_url, self._options.get(CONF_API_KEY)
                    )
                except CannotConnect:
                    self._models = None
                return await self.async_step_provider_model()

        return self.async_show_form(
            step_id="provider_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BASE_URL,
                        default=self._options.get(CONF_BASE_URL, DEFAULT_BASE_URL),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                    vol.Optional(
                        CONF_API_KEY,
                        default=self._options.get(CONF_API_KEY, ""),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_provider_model(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Edit model selection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            model = str(user_input.get(CONF_MODEL, "")).strip()
            if not model:
                errors[CONF_MODEL] = ERROR_MODEL_REQUIRED
            else:
                show = await async_show_model(
                    str(self._options.get(CONF_BASE_URL, "")),
                    model,
                    api_key=self._options.get(CONF_API_KEY),
                )
                caps = model_capabilities(show)
                if caps is not None and "tools" not in caps:
                    errors[CONF_MODEL] = ERROR_MODEL_NO_TOOLS
                else:
                    self._options[CONF_MODEL] = model
                    return self.async_create_entry(title="", data=self._options)

        if self._models:
            model_field: Any = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=self._models,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        else:
            model_field = selector.TextSelector()

        return self.async_show_form(
            step_id="provider_model",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MODEL,
                        default=self._options.get(CONF_MODEL, ""),
                    ): model_field,
                }
            ),
            errors=errors,
        )

    # ── 2. Model & response behavior ─────────────────────────────────────────

    async def async_step_llm_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Edit system prompt, context window, reasoning, and sampling."""
        if user_input is not None:
            errors = _validate_advanced_input(user_input)
            if not errors and (
                user_input.get(CONF_CONTEXT_WINDOW)
                or user_input.get(CONF_ENABLE_THINKING)
            ):
                _show = await async_show_model(
                    str(self._options.get(CONF_BASE_URL, "")),
                    str(self._options.get(CONF_MODEL, "")),
                    api_key=self._options.get(CONF_API_KEY),
                )
                errors = model_limit_errors(
                    user_input,
                    model_capabilities(_show),
                    model_context_length(_show),
                )
            if errors:
                return self.async_show_form(
                    step_id="llm_settings",
                    data_schema=_llm_settings_schema({**self._options, **user_input}),
                    errors=errors,
                )

            for key in (CONF_CONTEXT_WINDOW, CONF_RESPONSE_TIMEOUT):
                if key in user_input:
                    user_input[key] = int(user_input[key])

            if CONF_MAX_TOKENS in user_input and user_input[CONF_MAX_TOKENS] is not None:
                user_input[CONF_MAX_TOKENS] = int(user_input[CONF_MAX_TOKENS])
            else:
                self._options.pop(CONF_MAX_TOKENS, None)

            for key in (CONF_TEMPERATURE, CONF_TOP_P):
                if key in user_input and user_input[key] is not None:
                    user_input[key] = float(user_input[key])
                else:
                    self._options.pop(key, None)

            user_input[CONF_KEEP_ALIVE] = str(user_input.get(CONF_KEEP_ALIVE, "")).strip()

            self._options = {**self._options, **user_input}
            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="llm_settings",
            data_schema=_llm_settings_schema(self._options),
        )

    # ── 3. Tools & features ──────────────────────────────────────────────────

    async def async_step_tools(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """The built-in features, each one on or off."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if (
                user_input.get("tool_todo_injector")
                and self.hass.states.get("todo.ai_plugin_injektor") is None
            ):
                try:
                    flow = await self.hass.config_entries.flow.async_init(
                        "local_todo",
                        context={"source": config_entries.SOURCE_USER},
                    )
                    if flow.get("type") == "form":
                        field_name = "todo_list_name"
                        data_schema = flow.get("data_schema")
                        if data_schema is not None and hasattr(data_schema, "schema"):
                            for key in data_schema.schema:
                                if hasattr(key, "schema"):
                                    field_name = str(key.schema)
                                    break
                        await self.hass.config_entries.flow.async_configure(
                            flow["flow_id"],
                            {field_name: "AI Plugin - Injektor"},
                        )
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "AI Plugin: could not auto-create local_todo list",
                        exc_info=True,
                    )
                    errors["tool_todo_injector"] = "todo_injector_unavailable"

            if not errors:
                self._options = {**self._options, **user_input}
                return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="tools",
            data_schema=_tools_schema(self._options),
            errors=errors,
        )

    # ── 4. Voice, vision & audio ─────────────────────────────────────────────

    async def async_step_vision_audio(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Edit voice behavior, auto-ducking, camera vision, and audio input."""
        if user_input is not None:
            if CONF_FEEDBACK_LOOP_DEVICES not in user_input:
                self._options.pop(CONF_FEEDBACK_LOOP_DEVICES, None)
            self._options = {**self._options, **user_input}
            self._options[CONF_AUTO_DUCKING] = bool(user_input.get(CONF_AUTO_DUCKING, False))
            self._options[CONF_CAMERA_VISION_ENABLED] = bool(
                user_input.get(CONF_CAMERA_VISION_ENABLED, False)
            )
            self._options[CONF_AUDIO_INPUT_ENABLED] = bool(
                user_input.get(CONF_AUDIO_INPUT_ENABLED, False)
            )
            self._options[CONF_TIMER_ANNOUNCE_CHIME] = str(
                user_input.get(CONF_TIMER_ANNOUNCE_CHIME, "") or ""
            ).strip()
            self.hass.data.setdefault(DOMAIN, {})["auto_ducking"] = self._options[
                CONF_AUTO_DUCKING
            ]
            return self.async_create_entry(title="", data=self._options)

        chime_options = await self.hass.async_add_executor_job(
            discover_timer_chimes, self.hass
        )
        return self.async_show_form(
            step_id="vision_audio",
            data_schema=_vision_audio_schema(self._options, chime_options),
        )

    # ── 5. Web search ────────────────────────────────────────────────────────

    async def async_step_web_search(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Edit web search settings."""
        if user_input is not None:
            self._options.update(user_input)
            backend = user_input.get(CONF_WEB_SEARCH_BACKEND, DEFAULT_WEB_SEARCH_BACKEND)
            enabled = user_input.get(CONF_WEB_SEARCH_ENABLED, False)
            if enabled and backend == BACKEND_SEARXNG:
                return await self.async_step_searxng()
            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="web_search",
            data_schema=_web_search_schema(self._options),
        )

    async def async_step_searxng(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Edit SearXNG URL."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url = user_input[CONF_SEARXNG_URL].strip()
            if not _is_valid_url(url):
                errors[CONF_SEARXNG_URL] = ERROR_INVALID_URL
            else:
                self._options[CONF_SEARXNG_URL] = url
                return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="searxng",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SEARXNG_URL,
                        default=self._options.get(CONF_SEARXNG_URL, ""),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                }
            ),
            errors=errors,
        )

    # ── 6. Memory ────────────────────────────────────────────────────────────

    async def async_step_memory(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Saved facts, the conversation on a satellite, and the diary."""
        if user_input is not None:
            for key in (CONF_CONVERSATION_MEMORY, CONF_DIARY_DAYS):
                if key in user_input:
                    user_input[key] = int(user_input[key])
            user_input[CONF_EPHEMERAL_FILTER_ENABLED] = bool(
                user_input.get(CONF_EPHEMERAL_FILTER_ENABLED, False)
            )
            user_input[CONF_EPHEMERAL_PREFIXES] = str(
                user_input.get(CONF_EPHEMERAL_PREFIXES, DEFAULT_EPHEMERAL_PREFIXES)
            ).strip()
            self._options = {**self._options, **user_input}
            return self.async_create_entry(title="", data=self._options)
        return self.async_show_form(
            step_id="memory",
            data_schema=_memory_schema(self._options),
        )

    # ── 7. System, location & fallback ───────────────────────────────────────

    async def async_step_system_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Edit offline fallback, tool limits, prompt caching, and location."""
        if user_input is not None:
            if CONF_MAX_TOOL_ITERATIONS in user_input:
                user_input[CONF_MAX_TOOL_ITERATIONS] = int(user_input[CONF_MAX_TOOL_ITERATIONS])
            if CONF_WORKER_STATUS_ENTITY not in user_input:
                user_input[CONF_WORKER_STATUS_ENTITY] = None
            if CONF_LOCATION_ENTITY not in user_input:
                self._options.pop(CONF_LOCATION_ENTITY, None)
            self._options = {**self._options, **user_input}
            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="system_fallback",
            data_schema=_system_fallback_schema(self._options),
        )

    # ── Legacy fallback step ─────────────────────────────────────────────────

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Legacy combined advanced step (kept for compatibility)."""
        if user_input is not None:
            errors = _validate_advanced_input(user_input)
            if not errors and (
                user_input.get(CONF_CONTEXT_WINDOW)
                or user_input.get(CONF_ENABLE_THINKING)
            ):
                _show = await async_show_model(
                    str(self._options.get(CONF_BASE_URL, "")),
                    str(self._options.get(CONF_MODEL, "")),
                    api_key=self._options.get(CONF_API_KEY),
                )
                errors = model_limit_errors(
                    user_input,
                    model_capabilities(_show),
                    model_context_length(_show),
                )
            if errors:
                return self.async_show_form(
                    step_id="advanced",
                    data_schema=_advanced_schema(user_input, self.hass),
                    errors=errors,
                )
            for key in (
                CONF_CONTEXT_WINDOW,
                CONF_MAX_TOOL_ITERATIONS,
                CONF_RESPONSE_TIMEOUT,
                CONF_MAX_RESULTS,
                CONF_MAX_TOKENS,
            ):
                if key in user_input:
                    user_input[key] = int(user_input[key])
            for key in (CONF_TEMPERATURE, CONF_TOP_P):
                if key in user_input:
                    user_input[key] = float(user_input[key])
            self._options = merge_advanced_options(self._options, user_input)
            self._options[CONF_AUTO_DUCKING] = bool(user_input.get(CONF_AUTO_DUCKING, False))
            self.hass.data.setdefault(DOMAIN, {})["auto_ducking"] = self._options[
                CONF_AUTO_DUCKING
            ]
            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="advanced",
            data_schema=_advanced_schema(self._options, self.hass),
        )