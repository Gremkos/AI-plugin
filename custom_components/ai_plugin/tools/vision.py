"""Camera snapshot vision tool ("Alice's eyes") for AI Plugin."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from typing import Any

from homeassistant.components.camera import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from ..shortcuts.registry import _caller_area_id

_LOGGER = logging.getLogger(__name__)

VISION_TOOL_NAME = "look_at_camera"
_MAX_IMAGE_EDGE = 1280
_BLACK_FRAME_LUMA_THRESHOLD = 3.0

VISION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": VISION_TOOL_NAME,
        "description": (
            "Capture a live camera snapshot from a Home Assistant camera to see "
            "what is currently happening in a room or area. Use whenever the user "
            "asks you to look, check visually, see what is on a table/counter, "
            "who is in a room, or inspect a camera."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "camera": {
                    "type": "string",
                    "description": (
                        "Optional room/area name (e.g. 'Küche', 'kitchen'), "
                        "camera friendly name, or camera entity_id. Omit to use "
                        "the camera in the caller's current room."
                    ),
                },
                "question": {
                    "type": "string",
                    "description": "What to look for or check in the camera image.",
                },
            },
            "required": [],
        },
    },
}

_ROOM_ALIASES: dict[str, tuple[str, ...]] = {
    "kitchen": ("küche", "kueche"),
    "küche": ("kitchen", "kueche"),
    "living room": ("wohnzimmer", "stube"),
    "wohnzimmer": ("living room",),
    "bedroom": ("schlafzimmer",),
    "schlafzimmer": ("bedroom",),
    "bathroom": ("bad", "badezimmer"),
    "bad": ("bathroom", "badezimmer"),
    "hallway": ("flur", "diele", "gang"),
    "flur": ("hallway", "diele"),
    "office": ("büro", "buero", "arbeitszimmer"),
    "büro": ("office", "arbeitszimmer"),
    "garden": ("garten", "terrasse", "balkon"),
    "garten": ("garden", "terrasse"),
    "door": ("tür", "haustür", "eingang"),
}


def _expand_queries(query: str) -> list[str]:
    q_low = (query or "").strip().lower()
    if not q_low:
        return []
    candidates = [q_low]
    for key, aliases in _ROOM_ALIASES.items():
        if key in q_low:
            candidates.extend(aliases)
    return list(dict.fromkeys(candidates))


def _prepare_jpeg_and_luma(raw_bytes: bytes) -> tuple[bytes, float | None]:
    """Normalize image to RGB JPEG (max 1280px edge) and measure mean brightness."""
    try:
        from PIL import Image, ImageStat  # noqa: PLC0415

        with Image.open(io.BytesIO(raw_bytes)) as img:
            rgb = img.convert("RGB")
            luma = float(ImageStat.Stat(rgb.convert("L")).mean[0])
            w, h = rgb.size
            if max(w, h) > _MAX_IMAGE_EDGE:
                scale = _MAX_IMAGE_EDGE / float(max(w, h))
                rgb = rgb.resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    Image.Resampling.LANCZOS,
                )
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=85)
            return buf.getvalue(), luma
    except Exception:  # noqa: BLE001
        return raw_bytes, None


def _collect_cameras(hass: HomeAssistant) -> list[dict[str, str]]:
    """Return all available camera entities with friendly name and area name."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    cameras: list[dict[str, str]] = []
    for state in hass.states.async_all("camera"):
        if state.state in ("unavailable", "unknown"):
            continue
        eid = state.entity_id
        friendly = str(state.attributes.get("friendly_name") or eid)
        area_name = ""
        entry = ent_reg.async_get(eid)
        if entry is not None:
            area_id = entry.area_id
            if not area_id and entry.device_id:
                dev = dev_reg.async_get(entry.device_id)
                area_id = dev.area_id if dev else None
            if area_id:
                area = area_reg.async_get_area(area_id)
                if area is not None:
                    area_name = area.name
        cameras.append(
            {
                "entity_id": eid,
                "name": friendly,
                "area": area_name,
            }
        )
    return cameras


def _match_camera(
    hass: HomeAssistant,
    cameras: list[dict[str, str]],
    query: str,
    device_id: str | None,
) -> dict[str, str] | None:
    """Resolve the best matching camera by entity_id, area, name, or caller room."""
    candidates = _expand_queries(query)
    for q in candidates:
        for cam in cameras:
            if cam["entity_id"].lower() == q:
                return cam
        for cam in cameras:
            if cam["area"] and (cam["area"].lower() == q or q in cam["area"].lower()):
                return cam
        for cam in cameras:
            if q in cam["name"].lower() or q in cam["entity_id"].lower():
                return cam

    if device_id:
        area_id = _caller_area_id(hass, device_id)
        if area_id:
            area = ar.async_get(hass).async_get_area(area_id)
            if area is not None:
                for cam in cameras:
                    if cam["area"].lower() == area.name.lower():
                        return cam

    if len(cameras) == 1:
        return cameras[0]

    return None


async def async_capture_camera_snapshot(
    hass: HomeAssistant,
    arguments: dict[str, Any],
    device_id: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Capture a JPEG snapshot and return (tool_result_text, attachment_or_None)."""
    cameras = _collect_cameras(hass)
    if not cameras:
        return (
            "[No available cameras found in Home Assistant. Tell the user no camera is available.]",
            None,
        )

    query = str(arguments.get("camera") or "").strip()
    target = _match_camera(hass, cameras, query, device_id)
    if target is None:
        available = ", ".join(
            f"{c['name']} ({c['area'] or c['entity_id']})" for c in cameras
        )
        return (
            f"[Could not resolve camera {query!r}. Available cameras: {available}. "
            "Ask the user which camera they mean or retry with an available name.]",
            None,
        )

    entity_id = target["entity_id"]
    label = f"{target['name']} ({target['area']})" if target["area"] else target["name"]

    jpeg_bytes: bytes | None = None
    luma: float | None = None
    for attempt in range(2):
        try:
            image = await async_get_image(hass, entity_id, timeout=10)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("AI Plugin: failed to capture snapshot from %s: %s", entity_id, exc)
            return (
                f"[Could not capture image from {label}: {exc}. Tell the user the camera did not respond.]",
                None,
            )

        if not image or not image.content:
            return (
                f"[Camera {label} returned an empty image. Tell the user.]",
                None,
            )

        jpeg_bytes, luma = await hass.async_add_executor_job(
            _prepare_jpeg_and_luma, image.content
        )
        if luma is None or luma >= _BLACK_FRAME_LUMA_THRESHOLD or attempt == 1:
            break
        _LOGGER.info(
            "AI Plugin vision: %s returned a black frame (luma=%.1f), retrying...",
            entity_id,
            luma,
        )
        await asyncio.sleep(0.7)

    assert jpeg_bytes is not None
    _LOGGER.info(
        "AI Plugin vision: %s snapshot ready (%d bytes, luma=%s)",
        entity_id,
        len(jpeg_bytes),
        f"{luma:.1f}" if luma is not None else "n/a",
    )

    if luma is not None and luma < _BLACK_FRAME_LUMA_THRESHOLD:
        return (
            f"[Camera {label} returned a completely black image even after retry. "
            "Tell the user the camera image is currently black (stream asleep, dark room, or privacy cover).]",
            None,
        )

    b64_data = base64.b64encode(jpeg_bytes).decode("ascii")
    attachment = {
        "type": "image",
        "data": b64_data,
        "mime_type": "image/jpeg",
        "source": label,
    }
    return (
        f"[Live snapshot captured from {label}. Inspect the attached image directly and answer the user's question.]",
        attachment,
    )