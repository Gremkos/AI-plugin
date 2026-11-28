<p align="center">
  <img src="https://raw.githubusercontent.com/Gremkos/AI-plugin/main/custom_components/ai_plugin/brand/icon@2x.png" alt="AI Plugin" width="128" height="128">
</p>

<h1 align="center">AI Plugin for Home Assistant (Gremkos Fork)</h1>

<p align="center">
  <b>A voice-first conversation agent for Home Assistant, built for local LLMs — extended with Admin-MCP access control, camera vision, Jinja2 To-do prompt injection, offline Assist fallback, ephemeral announcement filtering, and dashboard-ready live timers.</b>
</p>

<p align="center">
  <a href="https://github.com/Gremkos/AI-plugin/commits/main"><img src="https://img.shields.io/github/last-commit/Gremkos/AI-plugin?style=flat-square&label=last%20commit" alt="Last commit"></a>
  <a href="https://hacs.xyz"><img src="https://img.shields.io/badge/HACS-custom-41BDF5?style=flat-square" alt="HACS custom repository"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-2025.7%2B-18BCF2?style=flat-square&logo=home-assistant&logoColor=white" alt="Home Assistant 2025.7 or newer">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue?style=flat-square" alt="License: PolyForm Noncommercial 1.0.0"></a>
</p>

<p align="center">
  <a href="#whats-new-in-this-fork">What's new</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#fork-features-in-detail">Fork features</a> ·
  <a href="#entities--services-added-in-this-fork">Entities &amp; Services</a> ·
  <a href="#original-features--documentation-upstream">Original docs</a> ·
  <a href="#configuration-reference">Configuration</a> ·
  <a href="#troubleshooting">Troubleshooting</a>
</p>

---

> [!NOTE]
> **Based on the original project by [@bigbabol1](https://github.com/bigbabol1/AI-plugin):**
> This repository is an actively maintained custom fork of [**bigbabol1/AI-plugin**](https://github.com/bigbabol1/AI-plugin). Huge thanks to [@bigbabol1](https://github.com/bigbabol1) for creating the core architecture, voice pipeline, local Voice-ID, conversation diary, and benchmark suite. The full original documentation is preserved further down in collapsible sections so everything stays in one place.

AI Plugin makes any OpenAI-compatible or Ollama model the **Assist conversation agent** for your home — including Ollama, custom local FastAPI/memory proxies, llama.cpp, LM Studio, OpenAI, and xAI. It handles device control, sensor questions, timers, camera vision, web search, and memory by voice or in text.

It is designed around a constraint most agents ignore: **a 7–12B model on modest local hardware (8–16 GB VRAM)**. Common commands skip the LLM entirely, prompts stay prefix-cache stable, and strict guardrails prevent hallucinated actions.

## What's new in this fork

Everything added or refined in **`Gremkos/AI-plugin`** on top of upstream:

- **🔐 Admin-MCP mode & stricter Voice-ID**
  - Select authorized admin person(s) under **MCP Servers** so privileged smart-home tools and fast-path shortcuts only execute for verified admins while guests remain safely restricted.
  - Runs local Voice-ID on signed-in Home Assistant Companion App devices whenever an audio clip is buffered.
  - Clears the signed-in user fallback when Voice-ID does not match, preventing misattributed speaker identities.
- **📝 Dynamic To-Do Prompt Injector (with Jinja2)**
  - Configurable toggle under **Tools** to inject active items (including their `description` fields) from a Home Assistant to-do list directly into the prompt as live rules or context.
  - Supports **Jinja2 templates** inside to-do rules, plus automatic speaker room context and sensor metadata injection.
- **👁️ Multimodal Camera Vision & Audio Input**
  - Built-in **`look_at_camera`** vision tool and multimodal attachment pipeline so the assistant can inspect exposed Home Assistant cameras on demand.
  - Reorganized **Vision, Audio & Voice** options menu with dedicated toggles for camera vision and multimodal audio input.
- **⏱️ Fast-Path Timers, Dashboard Live Countdowns & Chimes**
  - **Deterministic timer shortcut (`async_try_timer_shortcut`):** Unambiguous German and English timer commands (*"Nudeln auf fünf Minuten"*, *"Pizza-Timer pausieren"*, *"Alle Timer abbrechen"*, including spoken number words like *"Viertelstunde"* or *"eineinhalb Stunden"*) execute in milliseconds without waking the LLM or cluttering chat history — while intentionally leaving alarms/reminders routed to the assistant.
  - **Dashboard timer mirroring (`timer.ai_plugin_1` .. `timer.ai_plugin_3`):** Mirrors active HA voice timers onto three stable `timer.*` entities with `finishes_at`, giving Mushroom, Tile, Entity, and `button-card` widgets **live second-by-second browser countdowns** with zero recorder spam, plus `sensor.ai_plugin_timers` and interactive services (`ai_plugin.toggle_timer`, `ai_plugin.cancel_timer`).
  - **Announcement chime picker:** Dropdown directly beneath *Announce timers* that auto-discovers audio files from Home Assistant's `/media` directory (or uses the satellite's native preannounce sound), strips duplicate *"Timer"* suffixes from spoken names, and adds a satellite fallback when starting announce-mode timers from browser/text chats.
- **🔇 Ephemeral Announcement Filtering**
  - Configurable **ephemeral prefixes** (e.g. `[Durchsage]`) so automated notifications, reminders, and announcements bypass conversation history and diary storage while still being spoken cleanly.
- **🛟 Offline Fallback to Home Assistant Assist**
  - Automatically falls back to Home Assistant's native Assist intent pipeline whenever the local LLM host is asleep or unreachable, keeping essential smart-home voice commands working 24/7.
- **📡 Live Assist Status Sensor & Satellite Audio Polish**
  - Publishes **`sensor.ai_plugin_assist_status`** (`idle` / `listening` / `processing` / `responding`) for tablet avatars, LED indicators, and automations — hardened with `try/finally` STT cleanup and a 25 s auto-idle safety reset so it never gets stuck on `listening` after cancelled or empty transcripts.
  - Optional **`auto_ducking`** toggle scoped strictly to voice satellite turns, keeping room music ducked across the full conversation with playback-end tracking.
  - Disables incremental delta streaming on external satellites for reliable TTS playback and wires **`CONF_SELF_ECHO_FILTER`** cleanly so follow-up listening stays active after dropping a self-echo.
  - Optional toggle for **background voice prompt warmup**.
- **🇩🇪 Full German UI Translation & Direct HACS Tracking**
  - Complete German options UI translation (`translations/de.json`) and localized fast-path timer confirmations (`i18n/de.yaml` & `i18n/en.yaml`).
  - Disabled `zip_release` in `hacs.json` for seamless tracking of the `main` branch via HACS custom repositories.

## Quick start

### Requirements

- Home Assistant **2025.7.0** or newer
- [HACS](https://hacs.xyz/docs/use/)
- A reachable OpenAI-compatible or Ollama LLM endpoint:

| Backend | Provider URL | API key |
|---------|--------------|---------|
| [Ollama](https://ollama.com) *(recommended)* | `http://<host>:11434/v1` | — |
| Custom Local Proxy (e.g. FastAPI + Ollama) | `http://<host>:<port>/v1` | — |
| LM Studio | `http://<host>:1234/v1` | — |
| llama.cpp server | `http://<host>:8080/v1` | — |
| OpenAI | `https://api.openai.com/v1` | required |
| xAI Grok | `https://api.x.ai/v1` | required |

### Install

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Gremkos&repository=AI-plugin&category=integration)

1. In **HACS → ⋮ → Custom repositories**, add `https://github.com/Gremkos/AI-plugin` with category **Integration** (or use the button above).
2. Install **AI Plugin**, then **restart Home Assistant**.
3. Add the integration:

   [![Open your Home Assistant instance and start setting up AI Plugin.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ai_plugin)

4. Go to **Settings → Voice assistants**, open your assistant, and set **Conversation agent** to *AI Plugin*.

Every setting can be changed later under **Settings → Devices & services → AI Plugin → Configure**.

## Fork features in detail

### Admin-MCP & speaker access control

Under **Configure → MCP Servers**, enable **Admin-MCP mode** and pick one or more authorized Home Assistant **Person** entities:
- Verified admins (identified via Companion App login or local Voice-ID) get full access to MCP servers, smart-home controls, and fast-path shortcuts.
- Unrecognized speakers or guests are kept out of privileged admin tools while still being able to chat normally.
- If Voice-ID is active and fails to match an enrolled voice, any stale signed-in user fallback is cleared automatically.

### Dynamic To-Do Prompt Injector (Jinja2)

Under **Configure → Tools**, enable the **To-do prompt injector** and choose a Home Assistant `todo.*` list:
- Every open item (summary **and** optional `description` field) is injected into the system prompt on each turn, allowing live rule changes from your dashboard or phone without reloading the integration.
- **Jinja2 templates** inside to-do items are rendered dynamically before injection, alongside live speaker room context and sensor metadata.

### Camera Vision (`look_at_camera`) & Multimodal Audio

Under **Configure → Vision, Audio & Voice**:
- **Camera vision** enables the `look_at_camera` tool and multimodal image attachment pipeline. Ask *"Who is at the front door?"* or *"Is the package here?"* and the plugin captures a live snapshot from the matching exposed `camera.*` entity for vision-capable models (such as Gemma 4).
- **Audio input** forwards multimodal audio attachments directly to compatible backends.

### Fast-Path Timers, Dashboard Countdowns & Chimes

1. **Instant voice control (LLM bypass):** Unambiguous commands such as *"Nudeln auf fünf Minuten"*, *"Pizza-Timer auf 10 Minuten"*, *"Timer pausieren"*, *"Pizza-Timer um 2 Minuten verlängern"*, *"Wie lange läuft der Timer noch?"*, or *"Alle Timer abbrechen"* are handled deterministically in `tools/ha_local/timers.py` without invoking the LLM or cluttering conversation history. Replies use concise localized templates in `i18n/de.yaml` and `i18n/en.yaml` (e.g. *"Nudeln auf 5 Minuten, läuft."*). Alarms and reminders (e.g. *"Weck mich in 1 Stunde"*) are intentionally skipped so they stay routed to your assistant or alarm setup.
2. **Announce mode + custom chime:** Under **Vision, Audio & Voice**, enable **Announce timers instead of on-device ring** and choose a sound in **Timer announcement chime** — the dropdown automatically scans Home Assistant's `/media` folder (`.flac`, `.mp3`, `.wav`, `.ogg`, `.m4a`, `.aac`) and also offers the satellite's built-in preannounce sound. Trailing *"Timer"* suffixes are cleaned so *"Pizza-Timer"* is spoken naturally without repetition.
3. **Live second-by-second dashboard cards:** Active voice timers are mirrored automatically onto `timer.ai_plugin_1` through `timer.ai_plugin_3` (with `friendly_name`, `duration`, `remaining`, and `finishes_at`) plus `sensor.ai_plugin_timers`. Because they use Home Assistant's native `timer.` domain, the browser renders a smooth live countdown with zero database spam.

<details>
<summary><b>Example Mushroom Dashboard Card (live countdown + tap to pause / hold to cancel)</b></summary>

<br>

```yaml
type: vertical-stack
cards:
  - type: custom:mushroom-entity-card
    entity: timer.ai_plugin_1
    icon_color: amber
    tap_action:
      action: perform-action
      perform_action: ai_plugin.toggle_timer
      data:
        entity_id: timer.ai_plugin_1
    hold_action:
      action: perform-action
      perform_action: ai_plugin.cancel_timer
      data:
        entity_id: timer.ai_plugin_1
    visibility:
      - condition: state
        entity: timer.ai_plugin_1
        state_not: idle
  - type: custom:mushroom-entity-card
    entity: timer.ai_plugin_2
    icon_color: amber
    tap_action:
      action: perform-action
      perform_action: ai_plugin.toggle_timer
      data:
        entity_id: timer.ai_plugin_2
    hold_action:
      action: perform-action
      perform_action: ai_plugin.cancel_timer
      data:
        entity_id: timer.ai_plugin_2
    visibility:
      - condition: state
        entity: timer.ai_plugin_2
        state_not: idle
  - type: custom:mushroom-entity-card
    entity: timer.ai_plugin_3
    icon_color: amber
    tap_action:
      action: perform-action
      perform_action: ai_plugin.toggle_timer
      data:
        entity_id: timer.ai_plugin_3
    hold_action:
      action: perform-action
      perform_action: ai_plugin.cancel_timer
      data:
        entity_id: timer.ai_plugin_3
    visibility:
      - condition: state
        entity: timer.ai_plugin_3
        state_not: idle
```

</details>

### Ephemeral Message Prefix Filtering

Configure one or more prefixes (comma-separated, e.g. `[Durchsage]`) so automated prompts sent via `conversation.process` (morning alarms, medication reminders, radio/DJ transitions) are processed and spoken normally but **never saved** to conversation history or the diary.

### Offline Fallback to Native Home Assistant Assist

If your local LLM host is shut down, hibernating, or unreachable, the plugin catches the connection failure and transparently hands the turn over to Home Assistant's built-in Assist agent so lights, switches, and basic intents keep working even while your GPU server sleeps.

## Entities & Services added in this fork

| Entity / Service | Purpose |
|------------------|---------|
| `sensor.ai_plugin_assist_status` | Live pipeline status (`idle`, `listening`, `processing`, `responding`) with `try/finally` STT cleanup and 25 s auto-idle watchdog. |
| `timer.ai_plugin_1` .. `timer.ai_plugin_3` | Stable dashboard timer slots mirroring active HA voice timers with live `finishes_at` countdowns. |
| `sensor.ai_plugin_timers` | Count of active/paused voice timers with structured timer details in attributes. |
| `ai_plugin.toggle_timer` | Pause or resume a mirrored voice timer from the dashboard (`entity_id`, `slot`, or `timer_id`). |
| `ai_plugin.cancel_timer` | Cancel a mirrored voice timer from the dashboard (`entity_id`, `slot`, or `timer_id`). |

---

## Original features & documentation (upstream)

All core features from [**bigbabol1/AI-plugin**](https://github.com/bigbabol1/AI-plugin) remain fully intact in this fork. Expand any section below for the complete original reference, benchmarks, and usage details.

<details>
<summary><b>✨ Core highlights (upstream overview)</b></summary>

<br>

**🎙️ Built for voice**
- Streaming replies: voice pipelines with streaming TTS start talking after the first sentence *(disabled automatically for external satellites in this fork for cleaner TTS playback)*.
- Follow-up listening: always, never, or **let the model decide** by asking a question.
- Self-echo filtering for satellites whose replies play on a separate speaker.
- First-class voice timers, with an optional announce mode for any speaker.
- **Alarms, reminders and things to do later**: *"Wake me up at 7"*, *"Remind me to take out the trash tomorrow at 6"*, *"Turn off the lights in 30 minutes"*. They survive a restart.
- **A conversation lasts 15 minutes** on a satellite (adjustable under Memory), so *"and the bedroom?"* still knows what it follows.
- **Calendar entries by voice**: *"Put the dentist on my calendar for Friday at 10"*, and *"Delete the dentist on Friday"*. It asks before removing anything you didn't name.
- **Doors, windows and blinds**: *"Are all windows closed?"* reads the sensors, *"Close all blinds except the bedroom"* leaves the bedroom and the awning alone.
- **Messages and moving music**: *"Tell Anna dinner is ready"*, *"Tell the kitchen…"*, *"Move the music to the bedroom"*.
- **Voice recognition** *(optional)*: knows who is speaking, so each person gets their own memory. Runs inside Home Assistant, with no add-on and no cloud.
- **Find my phone**: *"Where is my phone?"* rings it loudly, even on silent.
- **Where people are**: *"Where's my wife?"*, *"Is Anna home?"*, *"Who is home?"* are answered from Home Assistant's people.
- **Light colours**: *"Make the light warm white"* or *"turn the lights blue"* changes the lights in the room you are in. White shades become a colour temperature.
- A short *"One second."* while a slow web look-up runs, so silence never means the question got lost.

**⚡ Fast on local hardware**
- A deterministic shortcut layer answers common commands in **~0.01–0.05 s** without calling the LLM.
- A cache-stable prompt layout reuses Ollama's prefix cache on every turn.
- On-demand entity discovery tools instead of a YAML entity dump in the prompt.
- Summarization runs in the background, never on the turn that hits the limit.

**🛡️ Grounded, not imagined**
- State questions, actions and time-sensitive questions are checked against the tools the model actually called. A reply made up from nothing gets one corrective re-run.
- Promised actions without a tool call are replaced with an honest failure notice.
- The config flow rejects models that cannot call tools, and warns when the context window is larger than the model supports.

**🧩 Extensible**
- Web search via DuckDuckGo (zero config), Brave, Tavily or SearXNG, plus `browse_url` for reading one page.
- Home Assistant's own Assist tools built in: devices, sensors, exposed scripts, to-do lists and calendars work right after setup, with no MCP server or token to set up.
- More tools from MCP servers over HTTP and stdio.
- An `ai_task` entity so automations can use the same model.
- Per-user long-term memory (`remember` / `recall` / `forget`) and cross-device conversation diary.
- **7 languages**: English, German, French, Spanish, Portuguese, Polish and Dutch. A new language is a single YAML file.

</details>

<details>
<summary><b>🤖 Choosing a model, recommended settings &amp; upstream benchmarks</b></summary>

<br>

**Tool-calling quality matters more than any benchmark score.** A model that hedges or asks clarifying questions instead of acting is a poor fit for a home agent, however well it scores elsewhere.

### Recommended local models

| Model | VRAM | Why | Median turn |
|-------|------|-----|-------------|
| **`gemma4:12b-it-qat`** | 12–16 GB | Google's 4-bit QAT build of Gemma 4 12B — strong German/English tool calling, native multimodal camera vision, and no long-conversation stalls. | — |
| **`hf.co/unsloth/gemma-4-12B-it-GGUF:IQ4_XS`** | 8 GB | Same Gemma 4 12B at 4 bits. Holds up in long conversations where the 3-bit quant stalls, at the same generation speed. | — |
| **`hf.co/unsloth/gemma-4-12B-it-GGUF:UD-Q3_K_XL`** | 8 GB | 0.4 GB smaller and fully in VRAM, but in long voice conversations it can stall on a turn and answer nothing. | ~3.6 s |
| **`qwen3.5:9b`** | 8 GB | Faster, says so when it lacks data, validated against the voice suite. | ~2.9 s |

```bash
ollama pull gemma4:12b-it-qat
# or for 8 GB VRAM:
ollama pull hf.co/unsloth/gemma-4-12B-it-GGUF:IQ4_XS
```

Avoid `granite4.1:8b` and `lfm2.5:8b` on non-English installs. Check `ollama ps` at your real context window. If part of the model runs on the CPU, measure tokens per second before keeping it.

### Recommended settings

| Setting | Value |
|---------|-------|
| Model | `gemma4:12b-it-qat` (12–16 GB), `hf.co/unsloth/gemma-4-12B-it-GGUF:IQ4_XS`, or `qwen3.5:9b` (8 GB) |
| Temperature | `1.0` for Gemma 4 12B (`0.2` for `qwen3.5:9b`) |
| top_p | `0.4` |
| Context Window | **`16384`** minimum. Stay under ~24 000 on 8 GB with default KV cache; 32K fits on 12–16 GB or with `OLLAMA_KV_CACHE_TYPE=q8_0`. |
| Max Tokens (`num_predict`) | `512`. Raising it makes failing turns slower, not better. |
| Keep-alive | `30m`, or `-1` on a dedicated GPU |
| Web Search | DuckDuckGo (default), or Tavily/Brave/SearXNG |

### Upstream benchmark notes (RTX 3060 Ti 8 GB, Ollama)

- **Voice suite (July 2026, plugin v0.9.35):** 29 read-only prompts through HA Assist in voice mode. `qwen3.5:9b` scored 93 % with honest misses; `qwen3:8b` / `qwen3-abliterated:8b` reached 86–90 % but fabricated answers; `mistral:7b` 86 %; `gemma4:e2b` 69 %; cloud `qwen3.5:397b` 100 %.
- **Adversarial tool calling (August 2026):** 10 German edge cases × 3 runs. `gemma-4-12B UD-Q3_K_XL` scored 100 % with exact area names; `qwen3.5:9b` scored 70 %; `granite4.1:8b` and `lfm2.5:8b` corrupted non-English room names.
- **Multi-turn conversations (August 2026):** 11 German turns × 2 passes on `gemma-4-12B UD-Q3_K_XL`. Ambiguous names, ellipsis, pronouns, two devices in one turn, self-correction and area references all passed (median turn: 4.5 s).
- **Stalled turns in long conversations (October 2026, Ollama 0.40.4):** 11 captured cases × 3 runs: `UD-Q3_K_XL` produced 9/33 empty runs; `IQ4_XS` produced 0/33 empty runs at the same 39 tokens/s. Google's 4-bit QAT build (`gemma4:12b-it-qat`) did not stall either.

</details>

<details>
<summary><b>🎙️ Follow-up listening, Auto-Ducking &amp; Self-Echo Filter</b></summary>

<br>

After a spoken reply, the satellite can reopen its microphone so you can answer without the wake word. **Listen for a follow-up after voice replies** has three modes:

| Mode | Behaviour |
|------|-----------|
| **Always** *(default)* | The microphone reopens after every reply. |
| **Let the AI decide** | The microphone reopens only when the reply **ends with a question**. *"Which room do you mean?"* keeps listening. *"The kitchen light is on."* ends the turn. |
| **Never** | Every turn ends after the reply. |

In every mode, the conversation still ends on a close phrase (*"thanks"*, *"bye"*, *"das war's"*, …), on a detected self-echo, and after a run of rapid chained turns.

**Satellites playing replies on a separate speaker:**
- **Self-echo filter (`CONF_SELF_ECHO_FILTER`):** Remembers what was just spoken on each device and silently drops any incoming voice turn that replays it (70 % of word pairs in order within 30 s) while keeping follow-up listening active on external satellites. Recognizable commands and bare *"yes"* are never dropped.
- **Auto-ducking (`CONF_AUTO_DUCKING`):** Scoped strictly to voice satellite turns in this fork, keeping playing room speakers ducked across the entire conversation until playback ends.
- **Quiet gap before listening again:** Above 0 s, the plugin reopens the microphone itself once the reply has finished playing in the room.
- **Force-end conversations on these satellites:** Ends the session after one turn on listed devices if STT garbles echo too heavily for the filter to catch.

</details>

<details>
<summary><b>🗣️ Voice recognition (Local NVIDIA TitaNet-small)</b></summary>

<br>

The assistant can tell who is speaking on a voice turn, giving each person their own memory, deciding whose phone *"Where is my phone?"* rings, resolving *"my wife"*, and gating **Admin-MCP** permissions.

**Setup:** Switch it on under **Configure → Voice recognition → Settings → Recognise voices**. Enroll each voice either by saying *"Learn my voice, I am Anna"* to a satellite, or under **Voice recognition → Enroll a voice** (eight short questions; *"stop"* ends early). Add people under **Settings → People** first.

**How it works:**
- Runs entirely inside Home Assistant on NumPy — no add-on and no cloud.
- Downloads NVIDIA TitaNet-small (~13 MB, Apache-2.0, pinned SHA-256) once from [`voice_model/`](voice_model/).
- Switches this integration's Assist pipelines to an STT pass-through that buffers 30 s of audio in RAM; turning the feature off restores original pipelines.
- Computes fingerprints while you speak (zero added delay) and stores only voiceprints, never raw audio.
- **Fork improvements:** Also runs Voice-ID on signed-in Companion App devices when an audio clip is buffered, and clears the signed-in user fallback if Voice-ID does not match.

A person is named only when their score clears **Minimum match score** (default `0.35`) and beats the next person by **Minimum lead** (default `0.10`). Enroll on each type of satellite microphone you use for best accuracy.

> [!IMPORTANT]
> Voice recognition is a convenience, not a cryptographic security feature. A similar voice or a recording can be mistaken for someone else.

</details>

<details>
<summary><b>⏰ Alarms, reminders, scheduled actions &amp; voice timers</b></summary>

<br>

Timers count down a length of time. Alarms, reminders, and delayed commands target a clock time and are persisted across restarts.

| You say | What happens |
|---|---|
| *"Set an alarm for 6:30 on Friday"*, *"Wake me up at 7"*, *"…every weekday"* | Speaks on this satellite every 10 seconds for up to 3 minutes |
| *"Stop"*, *"Snooze"*, *"Snooze for 5 minutes"* (while it rings) | Ends it, or rings again in 9 or 5 minutes |
| *"Remind me to take out the trash tomorrow at 6"* | Spoken once here, and sent to your phone if the assistant knows who you are |
| *"Turn off the lights in 30 minutes"*, *"At 10 dim the lights"* | Runs then as if you had just said it, in the same room |
| *"What alarms do I have?"*, *"Cancel my alarm"* | Lists or cancels alarms, reminders and scheduled actions |

- Supports targeting another room's satellite (*"Set an alarm for 7 in the bedroom"*).
- Missed due items after downtime still run if under 10 min late (alarms/actions) or under 24 h late (reminders), and drop cleanly afterwards.
- Repeats support `daily`, `weekdays`, `weekends`, `weekly`, and `biweekly`.
- Voice timers support `start_timer`, `cancel_timer`, `pause_timer`, `unpause_timer`, `increase_timer`, `decrease_timer`, and `timer_status`, plus this fork's fast-path shortcut, live dashboard slots, and custom `/media` chimes.

</details>

<details>
<summary><b>💬 Messages, moving music, Calendar, Find my phone &amp; People locations</b></summary>

<br>

### Messages &amp; moving music

| You say | What happens |
|---|---|
| *"Tell Anna dinner is ready"*, *"Send Anna a message that I am late"* | Notification on Anna's phone, with your name if known |
| *"Tell the kitchen dinner is ready"* | Spoken on the satellite in that room |
| *"Tell everyone dinner is ready"* | Home Assistant's own broadcast to every satellite |
| *"Remind Anna to buy milk at 5"* | Reminder sent to Anna's phone, or spoken here if she has none |
| *"Move the music to the bedroom"*, *"Take this to the kitchen"* | Moves current playback via Music Assistant |

*"Play some music"* plays shuffled playlists matching your saved taste (*"Remember that I like jazz and soul"*). With voice recognition on, stray music lyrics waking a satellite in an unknown voice are suppressed.

### Calendar entries

*"Put the dentist on my calendar for Friday at 10"* adds a 1-hour event (or all-day without a time) to calendars **exposed to Assist** that accept new events (e.g. Local Calendar). Birthdays repeat yearly automatically; *"every week"* / *"every month"* are supported.

### Find my phone

*"Where is my phone?"*, *"Ring my phone"*, *"Wo ist mein Handy?"*, or *"Where is Anna's phone?"* rings via the Companion app (three times, 6 s apart):
- **Android:** Speaks on the alarm channel at full volume even on silent, then restores volume.
- **iPhone:** Sends a Critical Alert (enable **Critical Alerts** for the HA app in iOS notification settings).
- Uses Voice-ID, Companion App login, or the named person; reports the last known zone if away from home.

### Where people are

*"Where's my wife?"*, *"Is Anna home?"*, and *"Who is home?"* read **person** entities exposed to Assist (and resolve relationships like *"my wife"* via saved facts). Exposing the Companion app's **Geocoded location** sensor adds the last street address when away.

</details>

<details>
<summary><b>🧠 Long-term memory &amp; Conversation diary</b></summary>

<br>

### Long-term memory (`remember` / `recall` / `forget`)

- Saved facts load into a late, cache-stable `[USER FACTS]` block so *"What's my…"* questions answer without a tool round trip.
- Stored in plain text per HA user at `<config>/.ai_plugin_memory_<user_id>.json` (per person on satellites when Voice-ID is enabled; unknown voices cannot overwrite memory).
- **Bring your own memory:** Turn off *Long-term memory* under Memory if you use an external memory server or MCP backend — tools, facts block, and rules are cleanly removed while stored files remain untouched on disk.

### Remembering earlier conversations (Diary)

- Ten minutes after a conversation goes quiet, the model saves up to three short notes (skipping device commands and small talk).
- Uses a compact local semantic search model ([potion-multilingual-128M](https://huggingface.co/minishlab/potion-multilingual-128M), ~40 MB, MIT, in `memory_model/`) that searches across languages inside Home Assistant in under 1 ms per turn.
- Stored in `<config>/.storage/ai_plugin_diary.<entry>`, auto-pruned after *Keep diary notes for* days (default 30), private per person unless *Share conversations between people* is enabled, and erasable via *"Forget our conversations"*.

</details>

<details>
<summary><b>🔌 Switching built-in tools off, AI Tasks, Web search &amp; MCP servers</b></summary>

<br>

### Switching built-in features off

Under **Configure → Tools**, independently disable timers, alarms/reminders, calendar, messages, announcements, moving music, or find my phone if your home already handles them via custom scripts or MCP servers. Their tools, prompt rules, and shortcuts step aside cleanly.

### AI Tasks for automations

```yaml
action: ai_task.generate_data
data:
  task_name: morning summary
  instructions: Summarize today's calendar in two sentences.
  entity_id: ai_task.ai_plugin
```

Supports structured JSON output via `structure` schemas.

### Web search

| Backend | Cost | API key | Notes |
|---------|------|---------|-------|
| **DuckDuckGo** *(default)* | Free | none | Zero config. Disambiguates places with your city/country. |
| Brave Search | Free tier / paid | [api.search.brave.com](https://api.search.brave.com/app/) | Reliable |
| Tavily | Free tier / paid | [tavily.com](https://tavily.com/) | AI-optimized results |
| SearXNG | Free (self-hosted) | instance URL | Full control, no third-party traffic |

`browse_url` reads individual pages through **Jina Reader** (`r.jina.ai`).

### MCP servers

Home Assistant's own Assist tools (`HassTurnOn`, `HassClimateSetTemperature`, `GetLiveContext`, exposed scripts, to-do lists, calendar reads) are built in with no MCP server or long-lived token required. External HTTP and stdio MCP servers can be added with presets (time, fetch, SQLite, etc.), capped at a 15 s timeout per call, and optionally gated by this fork's **Admin-MCP mode**.

</details>

<details>
<summary><b>⚡ How it stays fast &amp; Ollama tuning notes</b></summary>

<br>

1. **Skip the LLM when possible:** Deterministic shortcuts handle clock time, sunrise/sunset, room sensors, single-device on/off, covers, media transport, volume, mute, and (in this fork) timers in **~0.01–0.05 s**. Ambiguous phrases always fall through to the LLM.
2. **Reuse Ollama's prompt prefix cache:** System prompt and tool schemas stay byte-stable across turns while volatile context (`[CURRENT TIME]`, `[USER FACTS]`, `[LAST ACTION]`) sits at the tail.
3. **Ollama tuning notes:**
   - **Context window:** Sent directly as `num_ctx` via Ollama's native `/api/chat` — no Modelfile edits required.
   - **Temperature:** Lower to `0.1–0.3` if a model asks unnecessary clarifying questions instead of calling tools.
   - **Reasoning models** (`qwen3`, `deepseek-r1`): Sends `think: false` by default (requires Ollama 0.20+).
   - **Keep-alive:** Set *Ollama keep-alive* (`30m`, `24h`, or `-1`) so the model isn't unloaded after 5 minutes of idle time.

</details>

---

## Configuration reference

### Fork-specific options

| Option | Location | What it does |
|--------|----------|--------------|
| Admin-MCP mode &amp; admin persons | MCP Servers | Restricts MCP/admin tools and shortcuts to selected Home Assistant persons |
| To-do prompt injector | Tools | Injects open items (summary + description, Jinja2-enabled) from a `todo.*` list |
| Camera vision (`look_at_camera`) | Vision, Audio &amp; Voice | Enables camera inspection tool &amp; multimodal image attachment pipeline |
| Multimodal audio input | Vision, Audio &amp; Voice | Enables multimodal audio input forwarding |
| Auto-ducking | Vision, Audio &amp; Voice | Ducks room media players during voice satellite turns until playback finishes |
| Timer announcement chime | Vision, Audio &amp; Voice | Optional `/media` audio file or satellite default preannounce chime before timer speech |
| Ephemeral prefixes | Memory / Advanced | Comma-separated prefixes (e.g. `[Durchsage]`) excluded from history &amp; diary |
| Offline Assist fallback | Advanced | Falls back to native HA Assist when the local LLM host is offline |
| Background voice prompt warmup | Advanced | Pre-warms Ollama's KV prefix cache in the background |

<details>
<summary><b>All upstream configuration options</b></summary>

<br>

| Option | Default | What it does |
|--------|---------|--------------|
| System prompt | persona template | Appended to the built-in prompt (never replaces it) |
| Context Window | `16384` | Token budget **and** Ollama `num_ctx` |
| Summarization | on | Condenses old turns in the background near the limit |
| Voice mode | off | Forces the compact voice prompt, even for text |
| Listen for a follow-up | Always | Always / Let the AI decide / Never |
| Quiet gap before listening again | `0` s | Above 0, the plugin reopens the microphone itself once the room is quiet |
| Enable thinking | off | Lets reasoning models emit chain-of-thought (slower; disables streaming) |
| Max tool iterations | `10` | Cap on LLM↔tool round trips per turn |
| Timeout | `30` s | Per LLM call. MCP tools have their own 15 s cap. |
| Temperature / top_p / Max tokens | unset | Sampling. `1.0` / `0.4` / `512` recommended for Gemma 4 12B. |
| Ollama keep-alive | unset | Keeps the model in VRAM between requests |
| Prune tool schemas | off | Per-message schema pruning (defeats prefix cache; leave off) |
| Announce timers | off | Timer completions play on a media player/satellite instead of on-device ring |
| Long-term memory | on | `remember` / `recall` / `forget` and saved facts |
| Remember a satellite's conversation | `15` min | How long a satellite keeps its conversation between turns (`0–360`) |
| Remember earlier conversations | on | Cross-device semantic diary |
| Keep diary notes for | `30` days | Older notes are deleted automatically |
| Share conversations between people | off | Off: each person sees only their own notes and unrecognised voices |
| Tools | all on | Toggle timers, alarms/reminders, calendar, messages, announcements, moving music, find my phone |
| Recognise voices | off | Local speaker recognition (TitaNet-small) |
| Allow starting enrollment by voice | on | *"Learn my voice, I am Anna"* starts enrollment on that satellite |
| Minimum match score / lead | `0.35` / `0.10` | Voice-ID matching thresholds |
| Ignore satellite self-echo | on | Self-echo filter (`CONF_SELF_ECHO_FILTER`) |
| Force-end conversations on satellites | none | Feedback-loop guard for setups with TTS on a separate speaker |
| Location bias / entity | on / unset | Scopes *"near me"* searches to home coordinates or a live tracker |

</details>

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Cannot connect" during setup | Ensure the Provider URL ends in `/v1` and is reachable from the HA host. |
| Local LLM PC is asleep / offline | Enable **Offline Assist fallback** so basic smart-home commands continue via native HA Assist. |
| `sensor.ai_plugin_assist_status` stays on `listening` | Update to latest `main` — STT `try/finally` cleanup and the 25 s auto-idle watchdog clear cancelled or empty turns automatically. |
| Dashboard timer card stays hidden | Ensure **Tools → Timers** is enabled and verify `timer.ai_plugin_1` under Developer Tools → States while a timer is running. |
| Chime dropdown is empty | Place `.flac`, `.mp3`, or `.wav` files inside Home Assistant's `/media` directory and reopen **Configure → Vision, Audio &amp; Voice**. |
| Automated announcements clutter chat history | Add your prefix (e.g. `[Durchsage]`) to **Ephemeral prefixes** and start automated prompts with that prefix. |
| Guest cannot control devices | Expected when **Admin-MCP mode** is active — add the person to the authorized admin list under **MCP Servers** if desired. |
| Assistant answers itself in a loop | Enable **Ignore satellite self-echo** or add the device to **Force-end conversations on these satellites**. |
| Slow first reply after a quiet period | Set **Ollama keep-alive** (`30m` or `-1`) and optionally enable **Background voice prompt warmup**. |
| Timer LEDs count down with no sound | Enable **Announce timers** (and pick an optional chime). |
| Model reports no tool-calling capability | Pick a tool-capable model checked via Ollama's `/api/show`. |
| Empty replies from reasoning models | Upgrade Ollama to 0.20+. |
| Voice is often not recognised | Re-enroll on that specific satellite type or lower **Minimum match score** slightly. |
| iPhone Find-my-phone alert makes no sound | Allow **Critical Alerts** for the Home Assistant app in iOS notification settings. |
| *"Where is Anna?"* has no information | Expose Anna's **person** entity (and optionally her phone's *Geocoded location* sensor) to Assist. |

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
```

The unit suite runs against stubbed Home Assistant modules with mocked LLM calls. **`tests/eval/`** runs against a live install through `/api/conversation/process` (see [`tests/eval/README.md`](tests/eval/README.md)).

**Adding or editing a language:** Copy `i18n/en.yaml`, keep all template placeholders within the allowed schema (`area`, `label`, `name`, `place`, `time`, `unit`, `val`), and check [`i18n/CONTRIBUTING.md`](custom_components/ai_plugin/i18n/CONTRIBUTING.md).

## License & Credits

- **Original project:** [@bigbabol1/AI-plugin](https://github.com/bigbabol1/AI-plugin) — full credit for the foundation, voice architecture, benchmarks, and upstream documentation.
- **Fork enhancements &amp; maintenance:** [@Gremkos/AI-plugin](https://github.com/Gremkos/AI-plugin).
- **License:** [PolyForm Noncommercial 1.0.0](LICENSE). Free to use, change, and share for any noncommercial purpose (home use, hobby projects, study, research, charities, schools, or public bodies). Commercial use requires a separate license.
- **Voice model:** The optional voice-recognition model in [`voice_model/`](voice_model/) is NVIDIA TitaNet-small under the Apache License 2.0 (see [`voice_model/README.md`](voice_model/README.md)).
- Release history is in [CHANGELOG.md](CHANGELOG.md).