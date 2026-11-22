"""Local Sign in with ChatGPT authorization and token renewal."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode

import httpx
import jwt

from .providers import ProviderError

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
DYNAMIC_CLIENT = "dynamic_agent_client"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPES = {"resource.invoke", "chatgpt.tokens.use.direct"}
INVALID_REFRESH = {
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused", "invalid_client",
}
SESSION_FIELDS = {"access_token", "refresh_token", "id_token", "expires_at", "scopes"}


def connected(record: dict) -> bool:
    return bool(record.get("access_token") and record.get("refresh_token")
                and PLAN_SCOPES.issubset(record.get("scopes", ())))


def authorization(record: dict, host_id: str, redirect_uri: str) -> tuple[str, dict]:
    verifier, state, nonce = (secrets.token_urlsafe(32) for _ in range(3))
    client_id = record.get("client_id") or DYNAMIC_CLIENT
    params = {
        "client_id": client_id, "ext_agent_host_id": host_id, "redirect_uri": redirect_uri,
        "scope": SCOPES, "resource": RESOURCE, "response_type": "code",
        "state": state, "nonce": nonce, "code_challenge_method": "S256",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="),
    }
    if client_id == DYNAMIC_CLIENT:
        params["agent_name_hint"] = "Canvas Hub"
    elif record.get("reconsent"):
        params["prompt"] = "consent"
    if record.get("email"):
        params["login_hint"] = record["email"]
    return ISSUER + "/api/accounts/authorize?" + urlencode(params), {
        "verifier": verifier, "nonce": nonce, "state": state, "redirect_uri": redirect_uri,
        "client_id": client_id, "subject": record.get("subject"), "deadline": time.monotonic() + 600,
    }


class TokenError(ProviderError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def _tokens(data: dict) -> dict:
    try:
        response = httpx.post(TOKEN_URL, data={**data, "resource": RESOURCE}, timeout=15)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise TokenError("Could not complete the OpenAI token request. Try again.") from exc
    if not isinstance(payload, dict):
        raise TokenError("OpenAI returned an unreadable token response.")
    if not response.is_success:
        code = payload.get("error")
        raise TokenError("OpenAI rejected the ChatGPT token request. Try signing in again.",
                         code if isinstance(code, str) else None)
    return payload


def _session(payload: dict, previous: dict) -> dict:
    scope = payload.get("scope")
    scopes = scope.split() if isinstance(scope, str) else previous.get("scopes", [])
    if not PLAN_SCOPES.issubset(scopes):
        return {}
    access = payload.get("access_token")
    refresh = payload.get("refresh_token", previous.get("refresh_token"))
    expires = payload.get("expires_in")
    if (not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh
            or type(expires) is not int or expires <= 0 or str(payload.get("token_type")).lower() != "bearer"):
        raise TokenError("OpenAI did not return usable ChatGPT plan credentials.")
    return {"access_token": access, "refresh_token": refresh, "scopes": scopes, "expires_at": time.time() + expires}


def finish(attempt: dict, params) -> dict:
    if params.get("error") or not params.get("code"):
        raise TokenError("ChatGPT sign-in was cancelled or declined.")
    client_id = attempt["client_id"]
    returned_id = params.get("client_id")
    if client_id == DYNAMIC_CLIENT:
        client_id = returned_id
    if not client_id or not client_id.startswith("oaiapp_") or (returned_id and returned_id != client_id):
        raise TokenError("OpenAI did not confirm the expected ChatGPT registration.")
    payload = _tokens({
        "grant_type": "authorization_code", "client_id": client_id, "code": params["code"],
        "code_verifier": attempt["verifier"], "redirect_uri": attempt["redirect_uri"],
    })
    id_token = payload.get("id_token")
    try:
        keys = jwt.PyJWKClient(ISSUER + "/.well-known/jwks.json", timeout=15)
        key = keys.get_signing_key_from_jwt(id_token)
        identity = jwt.decode(id_token, key.key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                              options={"require": ["exp", "iss", "aud", "sub", "nonce"]})
        if not hmac.compare_digest(str(identity["nonce"]).encode(), attempt["nonce"].encode()):
            raise jwt.InvalidTokenError("nonce")
        if attempt["subject"] and identity["sub"] != attempt["subject"]:
            raise jwt.InvalidTokenError("subject")
        if identity.get("azp", client_id) != client_id:
            raise jwt.InvalidTokenError("authorized party")
    except (jwt.PyJWTError, TypeError, ValueError) as exc:
        raise TokenError("Could not verify the ChatGPT account returned by OpenAI.") from exc
    session = _session(payload, {})
    return {"client_id": client_id, "subject": identity["sub"],
            "email": str(identity.get("email") or ""), "name": str(identity.get("name") or ""),
            **session, **({"id_token": id_token} if session else {"reconsent": True})}


def refresh(record: dict) -> dict:
    payload = _tokens({"grant_type": "refresh_token", "client_id": record["client_id"],
                       "refresh_token": record["refresh_token"]})
    return _session(payload, record)


def revoke(record: dict) -> bool:
    token = record.get("refresh_token")
    if not token:
        return True
    for delay in (0, 0.3, 1):
        if delay:
            time.sleep(delay)
        try:
            response = httpx.post(ISSUER + "/api/accounts/oauth/revoke", timeout=5, data={
                "token": token, "token_type_hint": "refresh_token", "client_id": record["client_id"],
            })
        except httpx.RequestError:
            continue
        if response.status_code < 500:
            return response.status_code == 200
    return False
