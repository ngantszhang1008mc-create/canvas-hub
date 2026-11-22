"""ChatGPT registrations bound to saved Assistant setups."""
from __future__ import annotations

import time
from uuid import uuid4

from ..util import InvalidInput
from . import chatgpt_auth as auth
from .providers import ProviderError


class ChatGPTSettings:
    @staticmethod
    def _chatgpt_profile(store, profile_id):
        return next((p for p in store["profiles"] if p["id"] == profile_id and p["provider"] == "chatgpt"), None)

    @staticmethod
    def _chatgpt_public(profile):
        record = profile.get("chatgpt_registration") or {}
        if profile.get("provider") != "chatgpt" or not record:
            return {}
        return {
            "chatgpt_account": record.get("email") or record.get("name") or "ChatGPT account",
            "chatgpt_connected": auth.connected(record),
            "chatgpt_needs_consent": bool(record.get("reconsent")),
        }

    def chatgpt_start(self, profile_id, redirect_uri):
        with self._cfg_lock:
            store = self._load_store()
            profile = self._chatgpt_profile(store, profile_id)
            if profile is None:
                raise InvalidInput("Save a ChatGPT setup before connecting your account.")
            host_id = store.get("chatgpt_host")
            if not host_id:
                host_id = store["chatgpt_host"] = uuid4().urn
                self._save_store(store)
            self._chatgpt_pending = {
                key: attempt for key, attempt in self._chatgpt_pending.items()
                if attempt["deadline"] > time.monotonic() and attempt["profile_id"] != profile_id
            }
            url, attempt = auth.authorization(profile.get("chatgpt_registration") or {}, host_id, redirect_uri)
            self._chatgpt_pending[attempt["state"]] = {**attempt, "profile_id": profile_id}
            return {"url": url}

    def chatgpt_callback(self, params):
        with self._cfg_lock:
            attempt = self._chatgpt_pending.pop(params.get("state"), None)
            if not attempt or attempt["deadline"] <= time.monotonic():
                return None, "This ChatGPT sign-in request expired or has already been used. Start again in Assistant settings."
            profile_id = attempt["profile_id"]
            store = self._load_store()
            profile = self._chatgpt_profile(store, profile_id)
            if profile is None:
                return None, "This ChatGPT setup is no longer available."
            try:
                record = auth.finish(attempt, params)
            except ProviderError as exc:
                return profile_id, str(exc)
            profile["chatgpt_registration"] = record
            self._save_store(store)
            if auth.connected(record):
                return profile_id, "ChatGPT connected. Return to Assistant settings to choose a model."
            return profile_id, "Account verified. Enable ChatGPT plan usage in Assistant settings to send requests."

    def _chatgpt_access(self, profile_id, client_id):
        with self._cfg_lock:
            store = self._load_store()
            profile = self._chatgpt_profile(store, profile_id)
            record = (profile or {}).get("chatgpt_registration") or {}
            if client_id != record.get("client_id") or not auth.connected(record):
                raise ProviderError("Connect this ChatGPT setup in Assistant settings to use your plan.")
            if record["expires_at"] > time.time() + 60:
                return record["access_token"]
            try:
                refreshed = auth.refresh(record)
            except auth.TokenError as exc:
                if exc.code not in auth.INVALID_REFRESH:
                    raise
                refreshed = {}
            for key in auth.SESSION_FIELDS - {"id_token"}:
                record.pop(key, None)
            record.update(refreshed)
            if not refreshed:
                record.pop("id_token", None)
            self._save_store(store)
            if not refreshed:
                raise ProviderError("The ChatGPT session expired or plan access changed. Reconnect in Assistant settings.")
            return refreshed["access_token"]

    def chatgpt_models(self, profile_id):
        from .chatgpt import list_models

        try:
            cfg = self._load(profile_id)
            if cfg["provider"] != "chatgpt":
                raise InvalidInput("Choose a ChatGPT setup to list account models.")
            return {"models": list_models(cfg["_chatgpt_access"]()), "error": None}
        except ProviderError as exc:
            return {"models": [], "error": str(exc)}

    def _cancel_chatgpt_sign_in(self, profile_id):
        for state, attempt in list(self._chatgpt_pending.items()):
            if attempt["profile_id"] == profile_id:
                self._chatgpt_pending.pop(state)

    def chatgpt_disconnect(self, profile_id):
        with self._cfg_lock:
            store = self._load_store()
            profile = self._chatgpt_profile(store, profile_id)
            if profile is None:
                raise InvalidInput("Choose a ChatGPT setup to disconnect.")
            self._cancel_chatgpt_sign_in(profile_id)
            record = profile.get("chatgpt_registration") or {}
            revoked = auth.revoke(record)
            for key in auth.SESSION_FIELDS:
                record.pop(key, None)
            self._save_store(store)
            return {"revoked": revoked}
