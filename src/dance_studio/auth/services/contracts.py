from __future__ import annotations


DEFAULT_FALLBACK_AUTH_METHODS = ["telegram", "vk", "phone"]


def auth_error_payload(
    error: str,
    *,
    message: str | None = None,
    action: str | None = None,
    fallback_auth_methods: list[str] | None = None,
    **extra,
) -> dict:
    payload = {"error": error}
    if message is not None:
        payload["message"] = message
    if action is not None:
        payload["action"] = action
    if fallback_auth_methods is not None:
        payload["fallback_auth_methods"] = fallback_auth_methods
    payload.update(extra)
    return payload


def link_success_payload(
    *,
    provider: str,
    user_id: int,
    identities: dict | None = None,
    linked: bool = True,
    **extra,
) -> dict:
    payload = {
        "ok": True,
        "linked": linked,
        "provider": provider,
        "user_id": user_id,
    }
    if identities is not None:
        payload["identities"] = identities
    payload.update(extra)
    return payload
