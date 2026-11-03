"""MaaS OpenAPI low-level client: token cache + signed POST helpers.

Stdlib-only (urllib + ssl + json) so it runs inside any NexAU sandbox without
extra dependencies.

Endpoints:
- POST {host}/platform/api/v2/auth/getSessionToken    → access_token (TTL 7200s)
- POST {host}/platform/api/v2/auth/refreshToken       → renew access_token
- POST {host}/interface/api/kb/...                    → vector KB search APIs

TLS / cert verification:
Certificates are verified by default for https hosts. MaaS gateways in
private deployments are often signed by a non-public CA or self-signed; in
that case set `MAAS_VERIFY_SSL=false` (also accepts 0 / no / off) in the
runtime environment to skip verification. Any other value, or leaving it
unset, keeps verification on. This is the same rule the agent client
(`_maas_agent_client.py`) uses.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# 服务端复用同一个非 JWT token、且本地估计的过期时刻已过时，下一次向服务端
# 复查（再调 getSessionToken）的间隔秒数。
_REUSED_RECHECK_SECONDS = 300


def _verify_ssl_enabled() -> bool:
    """Verify certificates unless MAAS_VERIFY_SSL is 0/false/no/off."""
    return os.environ.get("MAAS_VERIFY_SSL", "").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _make_ssl_context(host: str) -> ssl.SSLContext | None:
    parsed = urllib.parse.urlparse(host)
    if parsed.scheme != "https":
        return None
    ctx = ssl.create_default_context()
    if not _verify_ssl_enabled():
        # Operator explicitly opted out (non-public CA / self-signed cert).
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _jwt_exp(token: str) -> float | None:
    """Read `exp` (epoch seconds) out of a JWT without any third-party lib.

    MaaS 的 app_key session token 是 HS256 JWT，payload 里带 exp——
    那是**服务端认定的**到期时刻，比任何客户端计时都准。
    不是 JWT、或没有 exp 时返回 None，由调用方回落。
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(seg).decode("utf-8"))
        exp = data.get("exp")
        if exp is None:
            return None
        exp = float(exp)
        if exp > 1e11:  # 毫秒级时间戳
            exp /= 1000.0
        return exp
    except Exception:
        return None


def _log(msg: str) -> None:
    """把鉴权链路的关键事件打到 stdout（沙箱 stdout 会进 pod 日志）。

    ⚠️ 这不是可选的调试便利，是**判断修复有没有生效的唯一手段**：
    这条链路上的失败此前全部塌缩成同一句报错——refreshToken 试没试、
    走的哪条计时分支、服务端是不是又把同一个 token 给回来了，
    从调用方看完全无法分辨。症状复发时，没有这几行就只能把整轮排查重来。
    """
    try:
        print(f"[maas-auth] {msg}", flush=True)
    except Exception:
        pass


class MaasApiError(RuntimeError):
    """Raised when the MaaS API responds with success=false or non-2xx."""


# Process-level cache: (host, app_key, user_id) → {access_token, refresh_token, exp_at}
_TOKEN_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}
_TOKEN_LOCK = threading.Lock()

# --- single-flight: one in-flight auth attempt per key; followers share its
#     result AND its failure (sharing the failure is what keeps a slow/timing-out
#     token endpoint from being paid N times in series). ---
_INFLIGHT: dict[Any, dict[str, Any]] = {}


def _single_flight(slot, fn):
    with _TOKEN_LOCK:
        cell = _INFLIGHT.get(slot)
        leader = cell is None
        if leader:
            cell = {"ev": threading.Event(), "val": None, "exc": None}
            _INFLIGHT[slot] = cell
    if not leader:
        cell["ev"].wait()
        if cell["exc"] is not None:
            raise cell["exc"]
        return cell["val"]
    try:
        cell["val"] = fn()
    except BaseException as exc:          # noqa: BLE001 - shared with followers
        cell["exc"] = exc
        raise
    finally:
        with _TOKEN_LOCK:
            _INFLIGHT.pop(slot, None)
        cell["ev"].set()
    return cell["val"]


class MaasClient:
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
        token_post=None,
    ):
        # token_post: optional hook `(path, body) -> (status, raw_bytes, headers)` used ONLY
        # for the two token endpoints (getSessionToken / refreshToken). Default: this
        # client's own urllib POST. The agent client injects its own transport here so it
        # keeps its retry / no-redirect / header behaviour while sharing this token cache.
        if not host:
            raise MaasApiError("MAAS_HOST is required (no default host)")
        # normalise: strip trailing slash
        self.host = host.rstrip("/")
        self.app_key = app_key
        self.app_secret = app_secret
        self.user_id = user_id
        self.timeout = timeout
        self._ssl_ctx = _make_ssl_context(self.host)
        self._token_post = token_post or self._post_json
        # single-flight slots are per transport kind: a follower never receives a result/exception
        # produced by another tool's transport (different retry budget / error type).
        self._flight_tag = "default" if token_post is None else "custom"

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

    def _fetch_new_token(self, prev: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call getSessionToken and derive a *server-truthful* expiry.

        ⚠️ 这里的过期时间不能用 `time.time() + expires_in`。
        MaaS 在 token 尚未到期时**会返回同一个 token**（实测：连续两次调用
        getSessionToken 拿到完全相同的 access_token），而它那边始终从
        **首次签发**时刻计时。若客户端每次申请都重新计时，两边的到期时刻
        就会越差越远——本地以为还有效、服务端已判过期，表现为
        `retriever_search_docs` 返回 401「获取令牌失败」，且**清缓存重取也没用**
        （重取拿回的还是同一个已过期的 token）。

        取值优先级：
        1. token 自身携带的 `exp`（MaaS 的 app_key session token 是 HS256 JWT）——最准
        2. 服务端复用了同一个 token → **沿用上一次的到期时刻，不重新计时**
        3. 都不适用 → 回落到 now + expires_in
        """
        status, raw, _ = self._token_post(
            "/platform/api/v2/auth/getSessionToken",
            {
                "app_key": self.app_key,
                "app_secret": self.app_secret,
                "user_id": self.user_id,
            },
        )
        if status >= 400:
            raise MaasApiError(
                f"getSessionToken HTTP {status}: {raw[:300].decode('utf-8', 'replace')}"
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise MaasApiError(f"getSessionToken non-JSON response: {raw[:300]!r}") from exc
        if not payload.get("success"):
            raise MaasApiError(
                f"getSessionToken failed: state={payload.get('state')} "
                f"message={payload.get('message')}"
            )
        data = payload.get("data") or {}
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = int(data.get("expires_in") or 7200)
        if not access_token:
            raise MaasApiError(f"getSessionToken returned no access_token: {payload}")

        jwt_exp = _jwt_exp(access_token)
        if jwt_exp is not None:
            exp_at = jwt_exp - 60
            # 最后 60 秒：安全垫已经把 exp_at 推到过去，而服务端还在复用同一个
            # token —— 再问也只会拿回它。别让缓存"一出生就过期"，否则这 60 秒里
            # 每个请求都白搭一次 getSessionToken。用真到期时刻兜底，尾部交给
            # 401 → renew_after_rejection 处理。
            if exp_at <= time.time():
                exp_at = jwt_exp
            basis = "jwt"
        elif prev and prev.get("access_token") == access_token:
            # 服务端复用了同一个 token：不要重新计时。
            if prev["exp_at"] > time.time():
                exp_at = prev["exp_at"]
                basis = "reused"
                _log("服务端复用了同一 token（非 JWT），沿用原过期时刻、不重新计时")
            else:
                # 本地估计的过期时刻已过、服务端却仍把同一个 token 发回来。
                # 本地无法确认服务端是否仍认它（以业务请求是否被拒为准）。
                # 沿用已过去的 exp_at 会让缓存"一出生就过期"，
                # 之后每次调用都白搭一次 getSessionToken。给一个短的复查窗口；
                # 窗口内若被服务端拒绝，仍走 renew_after_rejection → refreshToken。
                exp_at = time.time() + _REUSED_RECHECK_SECONDS
                basis = "reused-recheck"
                if prev.get("basis") != "reused-recheck":  # 只在进入该状态时记一次
                    _log("服务端仍返回同一 token（非 JWT），本地估计已过期：沿用并每 "
                         f"{_REUSED_RECHECK_SECONDS} 秒复查一次；被拒则走 refreshToken")
        else:
            exp_at = time.time() + max(expires_in - 60, 60)
            basis = "expires_in"
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "exp_at": exp_at,
            "basis": basis,
        }

    def _refresh_token_call(self, refresh_token: str) -> dict[str, Any] | None:
        """Force a brand-new token via refreshToken. Returns None if unusable.

        仅在「重取拿回同一个被拒 token」时才走这条路——那种情况下
        getSessionToken 无论调多少次都给不出新 token，只有它能打破僵局。
        """
        if not refresh_token:
            _log("refreshToken 跳过：手上没有 refresh_token")
            return None
        try:
            status, raw, _ = self._token_post(
                "/platform/api/v2/auth/refreshToken",
                {"refresh_token": refresh_token, "user_id": self.user_id},
            )
            if status >= 400:
                _log(f"refreshToken 失败：HTTP {status}（端点可能不存在或参数不符）")
                return None
            payload = json.loads(raw.decode("utf-8"))
            if not payload.get("success"):
                _log(f"refreshToken 被拒：state={payload.get('state')} "
                     f"message={payload.get('message')}")
                return None
            data = payload.get("data") or {}
            access_token = data.get("access_token")
            if not access_token:
                _log("refreshToken 返回 success 但没有 access_token")
                return None
            jwt_exp = _jwt_exp(access_token)
            expires_in = int(data.get("expires_in") or 7200)
            return {
                "access_token": access_token,
                "refresh_token": data.get("refresh_token") or refresh_token,
                "exp_at": jwt_exp - 60 if jwt_exp is not None
                else time.time() + max(expires_in - 60, 60),
                "basis": "refresh",
            }
        except Exception as exc:
            _log(f"refreshToken 异常：{type(exc).__name__}: {exc}")
            return None

    def get_access_token(self) -> str:
        """Return a cached token, fetching a new one outside the lock if needed.

        ⚠️ 网络请求**必须在锁外**：batch 搜索用 8 个线程并发，
        若在锁内发请求，第一个线程慢/超时会把其余 7 个一起拖死，
        单次 30s 的超时预算会被放大成分钟级（实测见过 151s）。
        """
        key = self._cache_key()
        with _TOKEN_LOCK:
            cached = _TOKEN_CACHE.get(key)
            if cached and cached["exp_at"] > time.time():
                return cached["access_token"]
            prev = dict(cached) if cached else None

        def _go():
            fresh = self._fetch_new_token(prev)  # 锁外发请求
            with _TOKEN_LOCK:
                cur = _TOKEN_CACHE.get(key)
                if cur and cur["exp_at"] > fresh["exp_at"]:
                    return cur["access_token"]
                _TOKEN_CACHE[key] = fresh
                return fresh["access_token"]

        return _single_flight((key, "get", self._flight_tag), _go)

    def renew_after_rejection(self, rejected_token: str) -> str:
        """Get a token that is *not* the rejected one.

        先常规重取；若服务端把同一个 token 又给回来（MaaS 的实际行为），
        再走 refreshToken 强制换发。这是本次 401 循环的正解——
        单纯 invalidate + 重取在服务端复用 token 时是无效动作。
        """
        key = self._cache_key()

        def _go():
            with _TOKEN_LOCK:
                cached = _TOKEN_CACHE.get(key)
                if (cached and cached.get("access_token") != rejected_token
                        and cached["exp_at"] > time.time()):
                    return cached["access_token"]
                prev = dict(cached) if cached else None
                if cached and cached.get("access_token") == rejected_token:
                    _TOKEN_CACHE.pop(key, None)

            fresh = self._fetch_new_token(prev)
            if fresh["access_token"] == rejected_token:
                _log("重取拿回的仍是被拒的同一个 token —— 服务端在复用，"
                     "改走 refreshToken 强制换发")
                forced = self._refresh_token_call(
                    (prev or {}).get("refresh_token") or fresh.get("refresh_token") or ""
                )
                if forced is not None:
                    _log("refreshToken 换发成功，拿到新 token")
                    fresh = forced
                else:
                    _log("⚠️ refreshToken 未能换发 —— 这条逃生口没走通，"
                         "本次鉴权将失败（若症状复发，先查这一行）")
            with _TOKEN_LOCK:
                if fresh["access_token"] != rejected_token:
                    _TOKEN_CACHE[key] = fresh
                else:
                    _TOKEN_CACHE.pop(key, None)
            return fresh["access_token"]

        return _single_flight((key, "renew", rejected_token, self._flight_tag), _go)

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
            raise MaasApiError(
                f"{path} non-JSON response (HTTP {status}): {raw[:300]!r}"
            ) from exc
        if status >= 400:
            raise MaasApiError(
                f"{path} HTTP {status}: "
                f"state={payload.get('state')} resultCode={payload.get('resultCode')} "
                f"message={payload.get('message')}"
            )
        if not self._is_ok(payload):
            raise MaasApiError(
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

        # MaaS reports an expired/invalid access token as HTTP 500 with
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
                # ⚠️ 不能只 invalidate 了事：MaaS 在 token 未到期时会把同一个
                # token 再给回来，那样重试用的还是刚被拒的那个，必然再 401。
                # renew_after_rejection 会在发现"又是它"时走 refreshToken 强制换发。
                self.renew_after_rejection(headers["accessToken"])
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
