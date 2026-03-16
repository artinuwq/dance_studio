from __future__ import annotations


class PasskeyAuthProvider:
    provider_name = "passkey"

    def register_begin(self, user_id: int):
        return {"status": "scaffold", "user_id": user_id, "challenge": "todo-passkey-register"}

    def register_complete(self, payload: dict):
        return {"status": "scaffold", "message": "Passkey register flow is scaffolded"}

    def login_begin(self):
        return {"status": "scaffold", "challenge": "todo-passkey-login"}

    def login_complete(self, payload: dict):
        return {"status": "scaffold", "message": "Passkey login flow is scaffolded"}
