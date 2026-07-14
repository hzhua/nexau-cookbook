"""MAP OpenAPI low-level client: token cache + signed POST helpers.

Stdlib-only (urllib + ssl + json) so it runs inside any NexAU sandbox without
extra dependencies.

Endpoints:
- POST {host}/platform/api/v2/auth/getSessionToken    → access_token (TTL 7200s)
- POST {host}/platform/api/v2/auth/refreshToken       → renew access_token
- POST {host}/interface/api/kb/...                    → vector KB search APIs

TLS / cert verification:
MAP gateways in private-deployment scenarios typically expose HTTPS with a
self-signed certificate (e.g. the demo host 117.184.59.230:10002). curl
must use `-k`; Python's urllib must be given a context with CERT_NONE.
We therefore **skip cert verification by default** for any HTTPS MAP_HOST.
Users who run MAP behind a properly CA-signed gateway can opt back in by
setting `MAP_VERIFY_SSL=true` in the runtime environment.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_HOST = "https://117.184.59.230:10002"


def _verify_ssl_enabled() -> bool:
    """True only when the operator explicitly opts in via MAP_VERIFY_SSL."""
    return os.environ.get("MAP_VERIFY_SSL", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _make_ssl_context(host: str) -> ssl.SSLContext | None:
    parsed = urllib.parse.urlparse(host)
    if parsed.scheme != "https":
        return None
    ctx = ssl.create_default_context()
    if not _verify_ssl_enabled():
        # Self-signed cert is the norm for private MAP deployments.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class MapApiError(RuntimeError):
    """Raised when the MAP API responds with success=false or non-2xx."""


# Process-level cache: (host, app_key, user_id) → {access_token, refresh_token, exp_at}
_TOKEN_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}
_TOKEN_LOCK = threading.Lock()


class MapClient:
    """Stateless façade — token cache lives at module level so multiple tool
    invocations within the same Runtime process share the same access_token."""

    def __init__(
        self,
        *,
        host: str,
        app_key: str,
        app_secret: str,
        user_id: str,
        timeout: int = 30,
    ):
        if not host:
            host = DEFAULT_HOST
        # normalise: strip trailing slash
        self.host = host.rstrip("/")
        self.app_key = app_key
        self.app_secret = app_secret
        self.user_id = user_id
        self.timeout = timeout
        self._ssl_ctx = _make_ssl_context(self.host)

    # ------------------------------------------------------------------ HTTP

    def _post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        url = f"{self.host}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() if exc.fp else b"", dict(exc.headers or {})

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        qs = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{self.host}{path}" + (f"?{qs}" if qs else "")
        headers = {"Accept": "*/*"}
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() if exc.fp else b"", dict(exc.headers or {})

    # ------------------------------------------------------------------ auth

    def _cache_key(self) -> tuple[str, str, str]:
        return (self.host, self.app_key, self.user_id)

    def _fetch_new_token(self) -> dict[str, Any]:
        status, raw, _ = self._post_json(
            "/platform/api/v2/auth/getSessionToken",
            {
                "app_key": self.app_key,
                "app_secret": self.app_secret,
                "user_id": self.user_id,
            },
        )
        if status >= 400:
            raise MapApiError(
                f"getSessionToken HTTP {status}: {raw[:300].decode('utf-8', 'replace')}"
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise MapApiError(f"getSessionToken non-JSON response: {raw[:300]!r}") from exc
        if not payload.get("success"):
            raise MapApiError(
                f"getSessionToken failed: state={payload.get('state')} "
                f"message={payload.get('message')}"
            )
        data = payload.get("data") or {}
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = int(data.get("expires_in") or 7200)
        if not access_token:
            raise MapApiError(f"getSessionToken returned no access_token: {payload}")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            # Refresh 60s before expiry to be safe.
            "exp_at": time.time() + max(expires_in - 60, 60),
        }

    def get_access_token(self) -> str:
        key = self._cache_key()
        with _TOKEN_LOCK:
            cached = _TOKEN_CACHE.get(key)
            if cached and cached["exp_at"] > time.time():
                return cached["access_token"]
            fresh = self._fetch_new_token()
            _TOKEN_CACHE[key] = fresh
            return fresh["access_token"]

    def invalidate_token(self, rejected_token: str | None = None) -> None:
        """Remove a rejected token without discarding a concurrent refresh."""
        with _TOKEN_LOCK:
            key = self._cache_key()
            cached = _TOKEN_CACHE.get(key)
            if cached is None:
                return
            if rejected_token is None or cached.get("access_token") == rejected_token:
                _TOKEN_CACHE.pop(key, None)

    # ------------------------------------------------------------------ api

    def _auth_headers(self) -> dict[str, str]:
        return {
            "accessToken": self.get_access_token(),
            "userId": self.user_id,
        }

    @staticmethod
    def _is_ok(payload: dict[str, Any]) -> bool:
        # /interface/api/* uses `result`/`succeed`; auth uses `success`.
        return bool(
            payload.get("success") or payload.get("result") or payload.get("succeed")
        )

    def _parse_payload(self, path: str, status: int, raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise MapApiError(
                f"{path} non-JSON response (HTTP {status}): {raw[:300]!r}"
            ) from exc
        if status >= 400:
            raise MapApiError(
                f"{path} HTTP {status}: "
                f"state={payload.get('state')} resultCode={payload.get('resultCode')} "
                f"message={payload.get('message')}"
            )
        if not self._is_ok(payload):
            raise MapApiError(
                f"{path} failed: "
                f"state={payload.get('state')} resultCode={payload.get('resultCode')} "
                f"message={payload.get('message')}"
            )
        return payload

    def _looks_like_auth_failure(self, status: int, payload: dict[str, Any]) -> bool:
        if status in (401, 403):
            return True
        if self._is_ok(payload):
            return False

        # MAP reports an expired/invalid access token as HTTP 500 with
        # state=28201 instead of an HTTP authentication status.
        codes = (
            str(payload.get("state") or ""),
            str(payload.get("resultCode") or ""),
        )
        if any(code == "28201" or code.startswith(("401", "403")) for code in codes):
            return True

        message = "".join(str(payload.get("message") or "").lower().split())
        return "token无效" in message or "invalidtoken" in message

    def call_json(self, path: str, body: dict[str, Any]) -> Any:
        """POST and return `data`; refresh and retry once on auth failure."""
        return self._call(self._post_json, path, body=body, return_full=False)

    def call_full(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST and return the FULL parsed payload (for endpoints whose
        attributes / messages we also need)."""
        return self._call(self._post_json, path, body=body, return_full=True)

    def call_get(self, path: str, params: dict[str, Any]) -> Any:
        """GET and return the JSON `data` field."""
        return self._call(self._get, path, params=params, return_full=False)

    def call_full_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET and return the FULL parsed payload."""
        return self._call(self._get, path, params=params, return_full=True)

    def _call(
        self,
        verb,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        return_full: bool,
    ) -> Any:
        # Retry once after an auth failure (token invalidation), independently
        # of up to two transient retries. Keeping separate retry budgets ensures
        # that a late auth failure still gets one request with the fresh token.
        auth_refreshed = False
        transient_retries = 0
        while True:
            headers = self._auth_headers()
            if body is not None:
                status, raw, _ = verb(path, body, extra_headers=headers)
            else:
                status, raw, _ = verb(path, params, extra_headers=headers)
            try:
                preview = json.loads(raw.decode("utf-8"))
                preview_dict = preview if isinstance(preview, dict) else {}
            except Exception:
                preview_dict = {}
            auth_failure = self._looks_like_auth_failure(status, preview_dict)
            if auth_failure and not auth_refreshed:
                self.invalidate_token(headers["accessToken"])
                auth_refreshed = True
                continue
            if (
                not auth_failure
                and transient_retries < 2
                and self._looks_like_transient(status, preview_dict)
            ):
                transient_retries += 1
                time.sleep(0.6)
                continue
            payload = self._parse_payload(path, status, raw)
            return payload if return_full else payload.get("data")

    @staticmethod
    def _looks_like_transient(status: int, payload: dict[str, Any]) -> bool:
        # Demo's update/permission endpoints intermittently return state=20001
        # ("服务未知异常") on the first request after a cold path; the same
        # body succeeds on a quick retry. Treat that as transient.
        if status >= 500:
            return True
        if status == 200 and not (
            payload.get("success") or payload.get("result") or payload.get("succeed")
        ):
            state = str(payload.get("state", ""))
            if state == "20001":
                return True
        return False
