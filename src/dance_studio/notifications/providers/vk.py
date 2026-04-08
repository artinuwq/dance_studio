from __future__ import annotations

import json
import secrets

import requests

from dance_studio.core.config import VK_API_VERSION, VK_COMMUNITY_ACCESS_TOKEN


class VkNotificationProvider:
    channel_type = "vk"

    def send(self, target_ref: str, title: str, body: str, payload: dict | None = None) -> dict:
        if not target_ref:
            return {"ok": False, "error": "missing_target_ref", "is_permanent": True}
        if not VK_COMMUNITY_ACCESS_TOKEN:
            return {"ok": False, "error": "vk_not_configured"}

        try:
            user_id = int(str(target_ref).strip())
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_vk_target", "is_permanent": True}
        if user_id <= 0:
            return {"ok": False, "error": "invalid_vk_target", "is_permanent": True}

        text = str(body or "").strip() or str(title or "").strip()
        if not text:
            return {"ok": False, "error": "empty_message", "is_permanent": True}

        request_payload = {
            "access_token": VK_COMMUNITY_ACCESS_TOKEN,
            "v": VK_API_VERSION or "5.199",
            "user_id": user_id,
            "random_id": secrets.randbelow(2_147_483_647),
            "message": text,
        }
        if payload and payload.get("keyboard") is not None:
            request_payload["keyboard"] = json.dumps(payload.get("keyboard"), ensure_ascii=False, separators=(",", ":"))

        try:
            response = requests.post(
                "https://api.vk.com/method/messages.send",
                data=request_payload,
                timeout=10,
            )
        except Exception as exc:
            return {"ok": False, "error": f"vk_exception:{exc}"}

        if not response.ok:
            return {"ok": False, "error": f"vk_http_{response.status_code}:send_failed"}

        try:
            data = response.json() if response.content else {}
        except Exception:
            data = {}

        if isinstance(data, dict) and "response" in data:
            message_id = data.get("response")
            try:
                message_id = int(message_id)
            except (TypeError, ValueError):
                message_id = None
            provider_message_id = f"vk:{message_id}" if message_id is not None else None
            return {"ok": True, "provider_message_id": provider_message_id, "message_id": message_id}

        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            error = data["error"]
            error_code = error.get("error_code")
            error_msg = str(error.get("error_msg") or "send_failed").strip()
            return {
                "ok": False,
                "error": f"vk_api_{error_code}:{error_msg}",
                "is_permanent": int(error_code or 0) in {901, 902, 15},
            }

        return {"ok": False, "error": "vk_send_failed"}


def edit_vk_message(
    *,
    peer_id: int,
    message_id: int,
    message: str,
    payload: dict | None = None,
) -> dict:
    if not VK_COMMUNITY_ACCESS_TOKEN:
        return {"ok": False, "error": "vk_not_configured"}

    text = str(message or "").strip()
    if not text:
        return {"ok": False, "error": "empty_message", "is_permanent": True}

    try:
        normalized_peer_id = int(peer_id)
        normalized_message_id = int(message_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_vk_message_ref", "is_permanent": True}

    if normalized_peer_id <= 0 or normalized_message_id <= 0:
        return {"ok": False, "error": "invalid_vk_message_ref", "is_permanent": True}

    request_payload = {
        "access_token": VK_COMMUNITY_ACCESS_TOKEN,
        "v": VK_API_VERSION or "5.199",
        "peer_id": normalized_peer_id,
        "message_id": normalized_message_id,
        "message": text,
    }
    if payload and payload.get("keyboard") is not None:
        request_payload["keyboard"] = json.dumps(payload.get("keyboard"), ensure_ascii=False, separators=(",", ":"))

    try:
        response = requests.post(
            "https://api.vk.com/method/messages.edit",
            data=request_payload,
            timeout=10,
        )
    except Exception as exc:
        return {"ok": False, "error": f"vk_exception:{exc}"}

    if not response.ok:
        return {"ok": False, "error": f"vk_http_{response.status_code}:edit_failed"}

    try:
        data = response.json() if response.content else {}
    except Exception:
        data = {}

    if isinstance(data, dict) and "response" in data:
        return {"ok": True}

    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        error_code = error.get("error_code")
        error_msg = str(error.get("error_msg") or "edit_failed").strip()
        return {
            "ok": False,
            "error": f"vk_api_{error_code}:{error_msg}",
            "is_permanent": int(error_code or 0) in {15, 100},
        }

    return {"ok": False, "error": "vk_edit_failed"}


def send_vk_message_event_answer(
    *,
    event_id: str,
    user_id: int,
    peer_id: int,
    event_data: dict | None = None,
) -> dict:
    if not VK_COMMUNITY_ACCESS_TOKEN:
        return {"ok": False, "error": "vk_not_configured"}

    normalized_event_id = str(event_id or "").strip()
    if not normalized_event_id:
        return {"ok": False, "error": "vk_event_id_required", "is_permanent": True}

    request_payload = {
        "access_token": VK_COMMUNITY_ACCESS_TOKEN,
        "v": VK_API_VERSION or "5.199",
        "event_id": normalized_event_id,
        "user_id": int(user_id),
        "peer_id": int(peer_id),
        "event_data": json.dumps(event_data or {}, ensure_ascii=False, separators=(",", ":")),
    }

    try:
        response = requests.post(
            "https://api.vk.com/method/messages.sendMessageEventAnswer",
            data=request_payload,
            timeout=10,
        )
    except Exception as exc:
        return {"ok": False, "error": f"vk_exception:{exc}"}

    if not response.ok:
        return {"ok": False, "error": f"vk_http_{response.status_code}:event_answer_failed"}

    try:
        data = response.json() if response.content else {}
    except Exception:
        data = {}

    if isinstance(data, dict) and "response" in data:
        return {"ok": True}

    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        error_code = error.get("error_code")
        error_msg = str(error.get("error_msg") or "event_answer_failed").strip()
        return {
            "ok": False,
            "error": f"vk_api_{error_code}:{error_msg}",
            "is_permanent": int(error_code or 0) in {15},
        }

    return {"ok": False, "error": "vk_event_answer_failed"}
