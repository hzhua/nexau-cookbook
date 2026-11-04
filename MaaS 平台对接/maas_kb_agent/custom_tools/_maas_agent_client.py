"""Secure, stdlib-only MAAS transport and authentication client.

MaaS 客户端与认证安全层

This module provides bounded JSON, SSE, and multipart transports, a
thread-safe Session Token cache, operation-aware retries, normalized errors,
TLS-safe defaults, and secret redaction. Business-specific response shaping
belongs to the tool layer.
"""

from __future__ import annotations

import codecs
import itertools
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any, BinaryIO, Callable, ClassVar, Iterable, Iterator, Mapping

# Token management is shared with the retrieval client: one process-level cache,
# server-truthful expiry, refreshToken re-issue after a rejection.
# NOTE: this file therefore needs custom_tools/_maas_client.py next to it (copy both).
try:
    from custom_tools import _maas_client as _shared_auth
except ImportError:  # reported as a configuration error when a client is constructed
    _shared_auth = None


TOKEN_PATH = "/platform/api/v2/auth/getSessionToken"
_AUTH_HEADER_NAMES = ("accessToken", "X-Secret-Key", "Authorization")
_SENSITIVE_LIST_ASSIGNMENT = re.compile(
    r"(?i)(MAAS_AGENT_NAMES|MAAS_PROJECT_IDS|MAAS_APPLICATION_IDS|"
    r"MAAS_AGENT_SECRET_KEYS|agent_names|project_ids|application_ids|agent_secret_keys)"
    r"\s*([:=])\s*([^\r\n;]+)"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(accessToken|access_token|refresh_token|X-Secret-Key|Authorization|"
    r"app_secret|appSecret|agent_secret_key|agent_secret_keys|MAAS_AGENT_SECRET_KEYS|"
    r"agent_names|project_ids|application_ids)"
    r"\s*([:=])\s*([^\r\n,;]+)"
)
_AGENT_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_AGENT_PROFILES = 64


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so authentication headers never cross an origin."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: BinaryIO,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


class Operation(str, Enum):
    """Classify request replay safety.
    按操作语义执行重试矩阵。
    """

    TOKEN = "token"
    CREATE = "create"
    CHAT = "chat"
    STOP = "stop"
    DELETE = "delete"
    UPLOAD = "upload"
    SIMILARITY = "similarity"


@dataclass(frozen=True)
class AgentProfile:
    """Resolved runtime-owned MAAS agent identity.

    Public aliases select protected project, application, and
    Chat secret values without exposing those values to Tool input schemas.
    """

    project_id: str
    application_id: str
    agent_secret_key: str


def _agent_configuration_error(message: str) -> MaasClientError:
    return MaasClientError("configuration_error", message, retryable=False)


def _configuration_list(value: str, name: str) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        raise _agent_configuration_error(f"{name} is required")
    items = [item.strip() for item in value.split(",")]
    if any(not item for item in items):
        raise _agent_configuration_error(f"{name} contains an empty item")
    return items


def resolve_agent_profile(
    agent_names: str,
    project_ids: str,
    application_ids: str,
    agent_secret_keys: str,
    default_agent: str,
    agent_alias: str | None,
    *,
    use_default: bool = False,
) -> tuple[str, AgentProfile]:
    """Resolve a public alias to one protected MAAS profile.

    Configuration defects are deployment errors; an explicit
    unknown alias is a caller parameter error and never falls back silently.
    """
    names = _configuration_list(agent_names, "MAAS_AGENT_NAMES")
    projects = _configuration_list(project_ids, "MAAS_PROJECT_IDS")
    applications = _configuration_list(application_ids, "MAAS_APPLICATION_IDS")
    secrets_by_agent = _configuration_list(agent_secret_keys, "MAAS_AGENT_SECRET_KEYS")
    if len(names) > _MAX_AGENT_PROFILES:
        raise _agent_configuration_error(
            f"MAAS_AGENT_NAMES must contain 1 to {_MAX_AGENT_PROFILES} names"
        )
    if len({len(names), len(projects), len(applications), len(secrets_by_agent)}) != 1:
        raise _agent_configuration_error(
            "MAAS agent configuration lists must have matching item counts"
        )
    if len(set(names)) != len(names):
        raise _agent_configuration_error("MAAS_AGENT_NAMES contains a duplicate name")
    if any(_AGENT_ALIAS.fullmatch(name) is None for name in names):
        raise _agent_configuration_error("MAAS_AGENT_NAMES contains an invalid alias")

    parsed = {
        name: AgentProfile(
            project_id=project,
            application_id=application,
            agent_secret_key=secret,
        )
        for name, project, application, secret in zip(
            names,
            projects,
            applications,
            secrets_by_agent,
            strict=True,
        )
    }

    if not isinstance(default_agent, str) or _AGENT_ALIAS.fullmatch(default_agent) is None:
        raise _agent_configuration_error("MAAS_DEFAULT_AGENT must be a valid alias")
    if default_agent not in parsed:
        raise _agent_configuration_error("MAAS_DEFAULT_AGENT is not configured")
    if agent_alias is None and use_default:
        selected = default_agent
    elif agent_alias is None:
        raise MaasClientError("invalid_parameter", "agent_alias is required", retryable=False)
    elif not isinstance(agent_alias, str) or _AGENT_ALIAS.fullmatch(agent_alias) is None:
        raise MaasClientError("invalid_parameter", "agent_alias is invalid", retryable=False)
    else:
        selected = agent_alias
    if selected not in parsed:
        raise MaasClientError("invalid_parameter", "agent_alias is not configured", retryable=False)
    return selected, parsed[selected]


class RequestPhase(str, Enum):
    """Describe how far a failed request progressed.
    只有可证明请求体未发送时才允许重放非幂等请求。
    """

    BEFORE_SEND = "before_send"
    BODY_STARTED = "body_started"
    RESPONSE_STARTED = "response_started"


@dataclass(frozen=True)
class ClientLimits:
    """Finite runtime limits with non-disableable hard caps.
    为 HTTP、SSE、Chat 数据与上传提供统一资源边界。
    """

    request_timeout: float = 60.0
    sse_idle_timeout: float = 30.0
    max_response_bytes: int = 8 * 1024 * 1024
    max_sse_bytes: int = 8 * 1024 * 1024
    max_sse_event_bytes: int = 1024 * 1024
    max_sse_events: int = 10_000
    max_answer_chars: int = 500_000
    max_thinking_chars: int = 500_000
    max_docs: int = 1_000
    max_node_statuses: int = 5_000
    max_data_url_bytes: int = 5 * 1024 * 1024
    max_upload_files: int = 20
    max_upload_file_bytes: int = 1024 * 1024 * 1024
    max_upload_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_multipart_fields: int = 100
    max_multipart_field_bytes: int = 1024 * 1024

    _HARD_CAPS: ClassVar[dict[str, int | float]] = {
        "request_timeout": 300.0,
        "sse_idle_timeout": 120.0,
        "max_response_bytes": 32 * 1024 * 1024,
        "max_sse_bytes": 32 * 1024 * 1024,
        "max_sse_event_bytes": 4 * 1024 * 1024,
        "max_sse_events": 50_000,
        "max_answer_chars": 2_000_000,
        "max_thinking_chars": 2_000_000,
        "max_docs": 5_000,
        "max_node_statuses": 20_000,
        "max_data_url_bytes": 20 * 1024 * 1024,
        "max_upload_files": 100,
        "max_upload_file_bytes": 2 * 1024 * 1024 * 1024,
        "max_upload_total_bytes": 4 * 1024 * 1024 * 1024,
        "max_multipart_fields": 500,
        "max_multipart_field_bytes": 4 * 1024 * 1024,
    }

    def __post_init__(self) -> None:
        for name, hard_cap in self._HARD_CAPS.items():
            value = getattr(self, name)
            if name in {"request_timeout", "sse_idle_timeout"}:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{name} must be a finite number")
            elif isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value <= 0 or value > hard_cap:
                raise ValueError(f"{name} must be greater than zero and no more than {hard_cap}")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "ClientLimits":
        """Load optional bounded limits from environment variables.
        空值、非正值和超过硬上限的配置均拒绝启动。
        """
        source = os.environ if environ is None else environ
        defaults = cls()
        values: dict[str, int | float] = {}
        for field_name in cls._HARD_CAPS:
            env_name = f"MAAS_{field_name.upper()}"
            raw = source.get(env_name)
            if raw is None:
                continue
            if not raw.strip():
                raise ValueError(f"{env_name} must not be empty")
            default = getattr(defaults, field_name)
            try:
                values[field_name] = float(raw) if isinstance(default, float) else int(raw)
            except ValueError as error:
                raise ValueError(f"{env_name} must be numeric") from error
        return cls(**values)


@dataclass(frozen=True)
class RetryPolicy:
    """Bound retry count and delay.
    重试次数和总等待时间始终有限。
    """

    max_attempts: int = 3
    backoff_seconds: float = 0.2
    total_timeout: float = 120.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("max_attempts must be an integer")
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if (
            isinstance(self.backoff_seconds, bool)
            or not isinstance(self.backoff_seconds, (int, float))
            or not math.isfinite(self.backoff_seconds)
        ):
            raise ValueError("backoff_seconds must be a finite number")
        if not 0 <= self.backoff_seconds <= 2:
            raise ValueError("backoff_seconds must be between 0 and 2")
        if (
            isinstance(self.total_timeout, bool)
            or not isinstance(self.total_timeout, (int, float))
            or not math.isfinite(self.total_timeout)
            or not 0 < self.total_timeout <= 300
        ):
            raise ValueError("total_timeout must be a finite number between 0 and 300")


@dataclass(frozen=True)
class PreparedRequest:
    """Transport-neutral HTTP request."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | Iterable[bytes] | None


@dataclass
class HttpResponse:
    """Bounded non-streaming HTTP response."""

    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass
class StreamingHttpResponse:
    """Open streaming HTTP response owned by the caller."""

    status: int
    headers: Mapping[str, str]
    stream: BinaryIO

    def close(self) -> None:
        self.stream.close()


@dataclass(frozen=True)
class SSEEvent:
    """One parsed Server-Sent Event frame."""

    data: str
    event: str | None = None
    event_id: str | None = None
    retry: int | None = None


@dataclass(frozen=True)
class MultipartPart:
    """An already-open multipart file part.
    传输层只读取调用者已安全打开并复核的句柄。
    """

    file: BinaryIO
    filename: str
    size: int
    field_name: str = "files"
    content_type: str | None = None


class MaasClientError(RuntimeError):
    """Normalized and sanitized client failure.
    错误对象不携带凭据或完整认证 Header。
    """

    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        upstream_code: str | int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.retryable = retryable
        self.http_status = http_status
        self.upstream_code = upstream_code

    def as_error(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": self.error_type,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.http_status is not None:
            result["http_status"] = self.http_status
        if self.upstream_code is not None:
            result["upstream_code"] = self.upstream_code
        return result


class TransportFailure(RuntimeError):
    """Transport failure carrying the last provable request phase."""

    def __init__(self, message: str, phase: RequestPhase) -> None:
        super().__init__(message)
        self.phase = phase


class StreamInterrupted(MaasClientError):
    """Report an SSE interruption after response processing started."""

    def __init__(self, message: str, event_count: int) -> None:
        super().__init__("stream_interrupted", message, retryable=False)
        self.event_count = event_count


Transport = Callable[
    [PreparedRequest, float, ssl.SSLContext | None, bool, int],
    HttpResponse | StreamingHttpResponse,
]

def clear_token_cache() -> None:
    """Clear process-local tokens, primarily for isolated runtime shutdown/tests.
    token 缓存与检索工具共用，这里同时清掉那一份。
    """
    if _shared_auth is None:
        return
    with _shared_auth._TOKEN_LOCK:
        _shared_auth._TOKEN_CACHE.clear()


def success_result(data: Any, warnings: Iterable[str] = ()) -> dict[str, Any]:
    """Build the common success envelope."""
    return {"ok": True, "data": data, "error": None, "warnings": list(warnings)}


def error_result(
    error: MaasClientError,
    *,
    partial: Any = None,
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the common failure envelope."""
    data = {"partial": partial} if partial is not None else None
    return {"ok": False, "data": data, "error": error.as_error(), "warnings": list(warnings)}


class MaasClient:
    """Secure MAAS HTTP client shared by all Tool Resources.
    统一认证、有限传输、重试矩阵与脱敏边界。
    """

    def __init__(
        self,
        *,
        host: str,
        app_key: str,
        app_secret: str,
        user_id: str,
        limits: ClientLimits | None = None,
        retry_policy: RetryPolicy | None = None,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.host = self._validate_host(host)
        self.app_key = self._required("app_key", app_key)
        self.app_secret = self._required("app_secret", app_secret)
        self.user_id = self._required("user_id", user_id)
        self.limits = limits or ClientLimits.from_environment()
        self.retry_policy = retry_policy or RetryPolicy()
        self._transport = transport or self._stdlib_transport
        self._clock = clock
        self._sleep = sleep
        self._logger = logger or logging.getLogger(__name__)
        self._secrets = {self.app_key, self.app_secret}
        self._secrets_lock = threading.Lock()
        self._ssl_context = self._make_ssl_context()
        # Token cache / renewal live in the retrieval client; this is only a handle to it.
        if _shared_auth is None:
            raise MaasClientError(
                "configuration_error",
                "custom_tools/_maas_client.py is missing: the agent tools share its token management; copy it together with _maas_agent_client.py",
            )
        try:
            self._auth = _shared_auth.MaasClient(
                host=self.host,
                app_key=self.app_key,
                app_secret=self.app_secret,
                user_id=self.user_id,
                timeout=self.limits.request_timeout,
                token_post=self._token_transport,
            )
        except TypeError:  # an older custom_tools/_maas_client.py of a different layout
            raise MaasClientError(
                "configuration_error",
                "custom_tools/_maas_client.py is an incompatible (older) version: overwrite it with the one shipped next to _maas_agent_client.py",
            ) from None

    @staticmethod
    def _required(name: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required and must not be empty")
        return value.strip()

    @staticmethod
    def _validate_host(host: str) -> str:
        value = MaasClient._required("host", host).rstrip("/")
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("host must be an absolute HTTP or HTTPS URL")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("host must not contain credentials, query, or fragment")
        return value

    def _make_ssl_context(self) -> ssl.SSLContext | None:
        if urllib.parse.urlsplit(self.host).scheme != "https":
            return None
        context = ssl.create_default_context()
        # 可选：部署方用 MAAS_VERIFY_SSL=false 关闭证书校验（MaaS 内网证书由非公开 CA 签发）；缺省照常校验
        if os.environ.get("MAAS_VERIFY_SSL", "true").strip().lower() in {"0", "false", "no", "off"}:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _remember_secret(self, value: str | None) -> None:
        if value:
            with self._secrets_lock:
                self._secrets.add(value)

    def redact_text(self, value: Any) -> str:
        """Remove configured credentials, tokens, and auth header values from text.
        日志与返回错误使用同一脱敏边界。
        """
        text = str(value)
        with self._secrets_lock:
            known_secrets = tuple(self._secrets)
        for secret_value in sorted(known_secrets, key=len, reverse=True):
            if secret_value:
                text = text.replace(secret_value, "[REDACTED]")
        text = _SENSITIVE_LIST_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
        return _SENSITIVE_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)

    def log(self, level: int, message: str, *args: Any) -> None:
        """Write a sanitized log message without forwarding unsafe format args."""
        rendered = message % args if args else message
        self._logger.log(level, self.redact_text(rendered))

    def _token_transport(self, path: str, body: Mapping[str, Any]) -> tuple[int, bytes, dict[str, str]]:
        """Token-endpoint POST for the shared token manager.
        走本 client 自己的传输层（有界重试 / 禁止重定向 / Accept: application/json），
        校验沿用原有规则；成功时把响应规整成共享管理器读取的形状，失败直接抛本 client 的错误包络。
        """
        request = self._json_request(path, body, headers={})
        response = self._send_with_retry(Operation.TOKEN, request, stream=False)
        assert isinstance(response, HttpResponse)
        payload = self._decode_response_payload(response)
        if response.status < 200 or response.status >= 300 or not self._payload_is_success(payload):
            raise self._response_error(response.status, payload, default_type="authentication_error")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        access_token = data.get("access_token") or data.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            raise MaasClientError("invalid_upstream_response", "Session Token response omitted access_token")
        self._remember_secret(access_token)
        refresh_token = data.get("refresh_token")
        if isinstance(refresh_token, str):
            self._remember_secret(refresh_token)
        expires_value = data.get("expires_in")
        if expires_value is None:
            expires_value = data.get("expiresIn")
        if expires_value is None:
            expires_value = 7200
        if isinstance(expires_value, bool):
            raise MaasClientError("invalid_upstream_response", "Session Token expiry is invalid")
        try:
            expires_in = float(expires_value)
        except (TypeError, ValueError) as error:
            raise MaasClientError("invalid_upstream_response", "Session Token expiry is invalid") from error
        if not math.isfinite(expires_in) or expires_in <= 0 or expires_in > 86_400:
            raise MaasClientError("invalid_upstream_response", "Session Token expiry is outside allowed bounds")
        normalized = {
            "success": True,
            "data": {
                "access_token": access_token,
                "refresh_token": refresh_token if isinstance(refresh_token, str) else None,
                "expires_in": max(1, int(expires_in)),
            },
        }
        return 200, json.dumps(normalized).encode("utf-8"), {}

    def _shared_token_call(self, fn: Callable[..., str], *args: Any) -> str:
        """Run a shared token operation; keep this client's error envelope."""
        try:
            token = fn(*args)
        except MaasClientError:
            raise  # already built (and redacted) by _token_transport
        except _shared_auth.MaasApiError:
            # fixed text on purpose: the shared client's messages may embed raw upstream payloads
            raise MaasClientError("authentication_error", "MAAS token request failed") from None
        except Exception as error:  # network / TLS / timeout while talking to the token endpoint
            raise MaasClientError(
                "transport_error",
                self.redact_text(f"MAAS token request failed: {type(error).__name__}: {error}"),
                retryable=True,
            ) from None
        self._remember_secret(token)
        with _shared_auth._TOKEN_LOCK:
            cached = _shared_auth._TOKEN_CACHE.get(self._auth._cache_key())
            self._remember_secret((cached or {}).get("refresh_token"))
        return token

    def get_access_token(self) -> str:
        """Return the shared Session Token (cached, or fetched once by the shared manager)."""
        return self._shared_token_call(self._auth.get_access_token)

    def renew_after_rejection(self, rejected_token: str) -> str:
        """Get a token other than the rejected one (re-fetch, else refreshToken)."""
        return self._shared_token_call(self._auth.renew_after_rejection, rejected_token)

    def invalidate_token(self, failed_token: str | None = None) -> None:
        """Invalidate only the token that actually received an auth rejection."""
        self._auth.invalidate_token(failed_token)

    def auth_headers(self, access_token: str, extra_headers: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build MAAS authentication headers without logging their values."""
        headers = {"accessToken": access_token, "userId": self.user_id}
        if extra_headers:
            for key, value in extra_headers.items():
                if key.lower() in {name.lower() for name in _AUTH_HEADER_NAMES}:
                    self._remember_secret(value)
                headers[key] = value
        return headers

    def request_json(
        self,
        operation: Operation,
        path: str,
        payload: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST JSON with Session Token auth and one explicit auth refresh.
        仅明确鉴权拒绝可使 Token 失效并刷新一次。
        """
        auth_refreshes = 0
        while True:
            token = self.get_access_token()
            request = self._json_request(path, payload, self.auth_headers(token, extra_headers))
            response = self._send_with_retry(operation, request, stream=False)
            assert isinstance(response, HttpResponse)
            parsed = self._decode_response_payload(response)
            auth_rejected = self._is_explicit_auth_rejection(
                response.status, parsed
            ) or self._is_stream_auth_rejection(response.status, response.headers) or self._is_token_invalid_shape(response.status, parsed)
            if auth_rejected:
                if auth_refreshes == 0:
                    auth_refreshes += 1
                    self.renew_after_rejection(token)
                    continue
                self.invalidate_token(token)
            if response.status < 200 or response.status >= 300 or not self._payload_is_success(parsed):
                raise self._response_error(response.status, parsed)
            return parsed

    def request_sse(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Iterator[SSEEvent]:
        """Open and parse one bounded SSE response without reconnecting.
        Chat 收到响应或事件后绝不自动重连或重放。
        """
        auth_refreshes = 0
        while True:
            token = self.get_access_token()
            headers = self.auth_headers(token, extra_headers)
            headers["Accept"] = "text/event-stream"
            request = self._json_request(path, payload, headers)
            response = self._send_with_retry(Operation.CHAT, request, stream=True)
            assert isinstance(response, StreamingHttpResponse)
            if 200 <= response.status < 300:
                content_type = self._header(response.headers, "Content-Type").split(";", 1)[0].strip().lower()
                if content_type == "text/event-stream":
                    return self.iter_sse(response)
            try:
                raw = self._read_bounded(response.stream, self.limits.max_response_bytes)
            finally:
                response.close()
            try:
                payload_error = self._decode_json_bytes(raw)
            except MaasClientError:
                payload_error = {}
            if 200 <= response.status < 300:
                if payload_error and not self._payload_is_success(payload_error):
                    if auth_refreshes == 0 and (
                        self._is_explicit_auth_rejection(response.status, payload_error)
                        or self._is_token_invalid_shape(response.status, payload_error)
                    ):
                        auth_refreshes += 1
                        self.renew_after_rejection(token)
                        continue
                    if auth_refreshes > 0 and (
                        self._is_explicit_auth_rejection(response.status, payload_error)
                        or self._is_token_invalid_shape(response.status, payload_error)
                    ):
                        self.invalidate_token(token)
                    raise self._response_error(response.status, payload_error)
                raise MaasClientError(
                    "invalid_upstream_response",
                    "MAAS Chat returned a non-SSE response",
                    http_status=response.status,
                )
            auth_rejected = self._is_stream_auth_rejection(response.status, response.headers)
            auth_rejected = auth_rejected or self._is_explicit_auth_rejection(
                response.status, payload_error
            ) or self._is_token_invalid_shape(response.status, payload_error)
            if auth_rejected:
                if auth_refreshes == 0:
                    auth_refreshes += 1
                    self.renew_after_rejection(token)
                    continue
                self.invalidate_token(token)
            raise self._response_error(response.status, payload_error)

    def upload_multipart(
        self,
        path: str,
        parts: Iterable[MultipartPart],
        *,
        fields: Mapping[str, str] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Upload bounded multipart content from already-open file handles.
        非幂等 multipart 在请求体开始发送后不自动重放。
        """
        part_list = list(itertools.islice(parts, self.limits.max_upload_files + 1))
        self._validate_multipart_parts(part_list)
        field_values = dict(fields or {})
        self._validate_multipart_fields(field_values)
        boundary = f"maas-{secrets.token_hex(16)}"
        body, content_length = self._multipart_body(boundary, part_list, field_values)
        if content_length > self.limits.max_upload_total_bytes:
            raise MaasClientError("response_limit_exceeded", "Multipart request body limit exceeded")
        token = self.get_access_token()
        headers = self.auth_headers(token, extra_headers)
        headers.update(
            {
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(content_length),
            }
        )
        request = PreparedRequest("POST", self._url(path), headers, body)
        response = self._send_with_retry(Operation.UPLOAD, request, stream=False)
        assert isinstance(response, HttpResponse)
        parsed = self._decode_response_payload(response)
        if (
            self._is_explicit_auth_rejection(response.status, parsed)
            or self._is_stream_auth_rejection(response.status, response.headers)
            or self._is_token_invalid_shape(response.status, parsed)
        ):
            try:
                # body is not replayable: renew now so the caller's next attempt has a valid token
                self.renew_after_rejection(token)
            except MaasClientError:
                self.invalidate_token(token)
            raise MaasClientError(
                "authentication_error",
                "Upload authentication was rejected; the non-replayable body was not retried",
                http_status=response.status,
                retryable=True,
            )
        if response.status < 200 or response.status >= 300 or not self._payload_is_success(parsed):
            raise self._response_error(response.status, parsed)
        return parsed

    def iter_sse(self, response: StreamingHttpResponse, chunk_size: int = 64 * 1024) -> Iterator[SSEEvent]:
        """Parse SSE frames across arbitrary byte chunks with strict limits.
        限制总字节、单事件大小、事件数和 UTF-8 解码。
        """
        total_bytes = 0
        event_count = 0
        buffer = b""
        try:
            while True:
                chunk = response.stream.read(chunk_size)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > self.limits.max_sse_bytes:
                    raise MaasClientError("response_limit_exceeded", "SSE byte limit exceeded")
                buffer += chunk
                while True:
                    match = re.search(br"\r?\n\r?\n", buffer)
                    if match is None:
                        if len(buffer) > self.limits.max_sse_event_bytes:
                            raise MaasClientError("response_limit_exceeded", "SSE event size limit exceeded")
                        break
                    frame = buffer[: match.start()]
                    buffer = buffer[match.end() :]
                    if len(frame) > self.limits.max_sse_event_bytes:
                        raise MaasClientError("response_limit_exceeded", "SSE event size limit exceeded")
                    if not frame:
                        continue
                    event = self._parse_sse_frame(frame)
                    if event is None:
                        continue
                    event_count += 1
                    if event_count > self.limits.max_sse_events:
                        raise MaasClientError("response_limit_exceeded", "SSE event count limit exceeded")
                    yield event
            if buffer.strip():
                if len(buffer) > self.limits.max_sse_event_bytes:
                    raise MaasClientError("response_limit_exceeded", "SSE event size limit exceeded")
                event = self._parse_sse_frame(buffer.rstrip(b"\r\n"))
                if event is not None:
                    event_count += 1
                    if event_count > self.limits.max_sse_events:
                        raise MaasClientError("response_limit_exceeded", "SSE event count limit exceeded")
                    yield event
        except MaasClientError:
            raise
        except Exception as error:
            raise StreamInterrupted(
                self.redact_text(f"SSE stream interrupted: {error}"), event_count
            ) from None
        finally:
            response.close()

    def _parse_sse_frame(self, frame: bytes) -> SSEEvent | None:
        try:
            text = codecs.decode(frame, "utf-8", "strict")
        except UnicodeDecodeError as error:
            raise MaasClientError("invalid_upstream_response", "SSE event is not valid UTF-8") from error
        data_lines: list[str] = []
        event_name: str | None = None
        event_id: str | None = None
        retry: int | None = None
        saw_data = False
        for line in text.splitlines():
            if not line or line.startswith(":"):
                continue
            field, separator, raw_value = line.partition(":")
            value = raw_value[1:] if separator and raw_value.startswith(" ") else raw_value
            if field == "data":
                saw_data = True
                data_lines.append(value)
            elif field == "event":
                event_name = value
            elif field == "id" and "\x00" not in value:
                event_id = value
            elif field == "retry" and value.isdigit():
                retry = int(value)
        if not saw_data:
            return None
        return SSEEvent(data="\n".join(data_lines), event=event_name, event_id=event_id, retry=retry)

    def _send_with_retry(
        self,
        operation: Operation,
        request: PreparedRequest,
        *,
        stream: bool,
    ) -> HttpResponse | StreamingHttpResponse:
        deadline = self._clock() + self.retry_policy.total_timeout
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise MaasClientError("timeout", "MAAS request exceeded its total retry deadline", retryable=True)
            operation_timeout = self.limits.sse_idle_timeout if stream else self.limits.request_timeout
            try:
                response = self._transport(
                    request,
                    min(operation_timeout, remaining),
                    self._ssl_context,
                    stream,
                    self.limits.max_response_bytes,
                )
            except TransportFailure as error:
                if attempt < self.retry_policy.max_attempts and self._may_retry_transport(operation, error.phase):
                    self._backoff(attempt, deadline)
                    continue
                error_type = (
                    "outcome_unknown"
                    if operation
                    in {Operation.CREATE, Operation.CHAT, Operation.STOP, Operation.DELETE, Operation.UPLOAD}
                    and error.phase is not RequestPhase.BEFORE_SEND
                    else "transport_error"
                )
                raise MaasClientError(
                    error_type,
                    self.redact_text(f"MAAS transport failed during {error.phase.value}: {error}"),
                    retryable=error_type == "transport_error",
                ) from None
            if (
                isinstance(response, HttpResponse)
                and self._may_retry_response(operation, response.status)
                and attempt < self.retry_policy.max_attempts
            ):
                self._backoff(attempt, deadline)
                continue
            return response
        raise MaasClientError("transport_error", "MAAS request exhausted its retry budget", retryable=True)

    @staticmethod
    def _may_retry_transport(operation: Operation, phase: RequestPhase) -> bool:
        if operation is Operation.STOP:
            return False
        if operation in {Operation.TOKEN, Operation.SIMILARITY}:
            return True
        return phase is RequestPhase.BEFORE_SEND

    @staticmethod
    def _may_retry_response(operation: Operation, status: int) -> bool:
        return operation in {Operation.TOKEN, Operation.SIMILARITY} and status in {408, 425, 429, 500, 502, 503, 504}

    def _backoff(self, attempt: int, deadline: float) -> None:
        delay = min(self.retry_policy.backoff_seconds * attempt, max(0.0, deadline - self._clock()))
        if delay:
            self._sleep(delay)

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str:
        return next((str(value) for key, value in headers.items() if key.lower() == name.lower()), "")

    def _json_request(
        self,
        path: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> PreparedRequest:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        request_headers.update(headers)
        return PreparedRequest("POST", self._url(path), request_headers, encoded)

    def _url(self, path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("MAAS API path must be an absolute path without a host")
        return f"{self.host}{path}"

    @staticmethod
    def _payload_is_success(payload: Mapping[str, Any]) -> bool:
        if "detail" in payload and payload.get("detail") not in (None, "", [], {}):
            return False
        flags = [payload[key] for key in ("success", "result", "succeed") if key in payload]
        if flags:
            return all(value is True or value == 1 for value in flags)
        for key in ("code", "state"):
            if key in payload and str(payload[key]).strip().lower() not in {"0", "200", "ok", "success"}:
                return False
        return True

    @staticmethod
    def _upstream_code(payload: Mapping[str, Any]) -> str | int | None:
        for key in ("code", "state", "status"):
            value = payload.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                return value
        detail = payload.get("detail")
        if isinstance(detail, Mapping):
            for key in ("code", "state", "status"):
                value = detail.get(key)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    return value
        return None

    def _response_message(self, payload: Mapping[str, Any], status: int) -> str:
        detail = payload.get("detail")
        candidates = [payload.get("message"), payload.get("msg"), payload.get("error")]
        if isinstance(detail, Mapping):
            candidates.extend([detail.get("message"), detail.get("msg"), detail.get("error")])
        else:
            candidates.append(detail)
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return self.redact_text(candidate.strip())[:1000]
        return f"MAAS returned HTTP {status}" if status else "MAAS returned an unsuccessful response"

    def _response_error(
        self,
        status: int,
        payload: Mapping[str, Any],
        default_type: str = "upstream_error",
    ) -> MaasClientError:
        error_type = "authentication_error" if self._is_explicit_auth_rejection(status, payload) else default_type
        upstream_code = self._upstream_code(payload)
        if isinstance(upstream_code, str):
            upstream_code = self.redact_text(upstream_code)[:256]
        return MaasClientError(
            error_type,
            self._response_message(payload, status),
            retryable=status in {408, 425, 429, 500, 502, 503, 504},
            http_status=status,
            upstream_code=upstream_code,
        )

    @classmethod
    def _is_explicit_auth_rejection(cls, status: int, payload: Mapping[str, Any]) -> bool:
        if status == 401:
            return True
        code = str(cls._upstream_code(payload) or "").lower()
        message_parts = [payload.get("message"), payload.get("msg"), payload.get("error")]
        detail = payload.get("detail")
        if isinstance(detail, Mapping):
            message_parts.extend([detail.get("message"), detail.get("msg"), detail.get("error")])
        elif isinstance(detail, str):
            message_parts.append(detail)
        message = " ".join(str(item) for item in message_parts if item).lower()
        known_codes = {"401", "40101", "token_expired", "invalid_token", "unauthorized"}
        token_terms = ("token expired", "token invalid", "invalid token", "unauthorized", "未登录", "token过期", "token失效")
        return code in known_codes or (status == 403 and any(term in message for term in token_terms))

    @classmethod
    def _is_token_invalid_shape(cls, status: int, payload: Mapping[str, Any]) -> bool:
        """MaaS reports an expired token as HTTP 500 / HTTP 200+success=false with
        state 28201 or 'token无效' (same shapes the retrieval client recognises).
        Only used to trigger renewal; error classification is unchanged."""
        if not payload or cls._payload_is_success(payload):
            return False
        codes = {str(payload.get(key) or "") for key in ("state", "code", "resultCode")}
        if "28201" in codes:
            return True
        message = "".join(str(payload.get("message") or payload.get("msg") or "").lower().split())
        return "token无效" in message or "invalidtoken" in message

    @staticmethod
    def _is_stream_auth_rejection(status: int, headers: Mapping[str, str]) -> bool:
        marker = next((value for key, value in headers.items() if key.lower() == "x-maas-auth-error"), "")
        return status == 401 or (status == 403 and marker.lower() in {"invalid_token", "token_expired"})

    def _decode_json(self, response: HttpResponse) -> dict[str, Any]:
        return self._decode_json_bytes(response.body)

    def _decode_response_payload(self, response: HttpResponse) -> dict[str, Any]:
        try:
            return self._decode_json(response)
        except MaasClientError:
            if response.status < 200 or response.status >= 300:
                return {}
            raise

    @staticmethod
    def _decode_json_bytes(raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MaasClientError("invalid_upstream_response", "MAAS returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise MaasClientError("invalid_upstream_response", "MAAS JSON response must be an object")
        return payload

    def _validate_multipart_parts(self, parts: list[MultipartPart]) -> None:
        if not parts or len(parts) > self.limits.max_upload_files:
            raise MaasClientError("invalid_parameter", "Multipart file count is outside allowed bounds")
        total = 0
        for part in parts:
            if not isinstance(part.size, int) or isinstance(part.size, bool) or part.size < 0:
                raise MaasClientError("invalid_parameter", "Multipart file size must be a non-negative integer")
            if part.size > self.limits.max_upload_file_bytes:
                raise MaasClientError("response_limit_exceeded", "Multipart file size limit exceeded")
            self._validate_multipart_metadata("filename", part.filename, 512, "\r\n\x00")
            self._validate_multipart_metadata("field name", part.field_name, 128, "\r\n\x00\"")
            if part.content_type is not None:
                self._validate_multipart_metadata("content type", part.content_type, 256, "\r\n\x00")
            total += part.size
            if total > self.limits.max_upload_total_bytes:
                raise MaasClientError("response_limit_exceeded", "Multipart total size limit exceeded")

    @staticmethod
    def _validate_multipart_metadata(label: str, value: Any, max_bytes: int, forbidden: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or any(character in value for character in forbidden)
            or len(value.encode("utf-8")) > max_bytes
        ):
            raise MaasClientError("invalid_parameter", f"Multipart {label} is invalid")

    def _validate_multipart_fields(self, fields: Mapping[str, str]) -> None:
        if len(fields) > self.limits.max_multipart_fields:
            raise MaasClientError("invalid_parameter", "Multipart field count exceeds allowed bounds")
        for name, value in fields.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise MaasClientError("invalid_parameter", "Multipart fields must contain strings")
            if not name or len(name.encode("utf-8")) > 256:
                raise MaasClientError("invalid_parameter", "Multipart field name is outside allowed bounds")
            if len(value.encode("utf-8")) > self.limits.max_multipart_field_bytes:
                raise MaasClientError("response_limit_exceeded", "Multipart field value limit exceeded")

    @staticmethod
    def _quoted_header_value(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _multipart_body(
        self,
        boundary: str,
        parts: list[MultipartPart],
        fields: Mapping[str, str],
    ) -> tuple[Iterable[bytes], int]:
        chunks: list[bytes | tuple[BinaryIO, int]] = []
        for name, value in fields.items():
            if any(character in name for character in "\r\n\x00\""):
                raise MaasClientError("invalid_parameter", "Multipart field name is invalid")
            chunks.append(
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{self._quoted_header_value(name)}\""
                    f"\r\n\r\n{value}\r\n"
                ).encode("utf-8")
            )
        for part in parts:
            content_type = part.content_type or mimetypes.guess_type(part.filename)[0] or "application/octet-stream"
            header = (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"{self._quoted_header_value(part.field_name)}\"; "
                f"filename=\"{self._quoted_header_value(part.filename)}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
            chunks.extend((header, (part.file, part.size), b"\r\n"))
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        content_length = sum(len(chunk) if isinstance(chunk, bytes) else chunk[1] for chunk in chunks)

        def generate() -> Iterator[bytes]:
            for chunk in chunks:
                if isinstance(chunk, bytes):
                    yield chunk
                    continue
                handle, remaining = chunk
                while remaining:
                    data = handle.read(min(64 * 1024, remaining))
                    if not data:
                        raise TransportFailure("multipart file ended before declared size", RequestPhase.BODY_STARTED)
                    remaining -= len(data)
                    yield data

        return generate(), content_length

    @staticmethod
    def _read_bounded(stream: BinaryIO, limit: int, deadline: float | None = None) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise MaasClientError("timeout", "HTTP response exceeded its total deadline", retryable=True)
            chunk = stream.read(min(64 * 1024, limit + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise MaasClientError("response_limit_exceeded", "HTTP response byte limit exceeded")

    @classmethod
    def _stdlib_transport(
        cls,
        request: PreparedRequest,
        timeout: float,
        context: ssl.SSLContext | None,
        stream: bool,
        max_response_bytes: int,
    ) -> HttpResponse | StreamingHttpResponse:
        deadline = time.monotonic() + timeout
        urllib_request = urllib.request.Request(
            request.url,
            data=request.body,  # type: ignore[arg-type]
            headers=dict(request.headers),
            method=request.method,
        )
        handlers: list[Any] = [_NoRedirectHandler()]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        opener = urllib.request.build_opener(*handlers)
        try:
            response = opener.open(urllib_request, timeout=timeout)
            headers = dict(response.headers)
            if stream:
                return StreamingHttpResponse(response.status, headers, response)
            try:
                body = cls._read_bounded(response, max_response_bytes, deadline)
                return HttpResponse(response.status, headers, body)
            finally:
                response.close()
        except urllib.error.HTTPError as error:
            if stream:
                return StreamingHttpResponse(error.code, dict(error.headers or {}), error)
            try:
                body = cls._read_bounded(error, max_response_bytes, deadline)
            finally:
                error.close()
            return HttpResponse(error.code, dict(error.headers or {}), body)
        except MaasClientError:
            raise
        except Exception as error:
            phase = RequestPhase.BODY_STARTED if request.body is not None else RequestPhase.BEFORE_SEND
            raise TransportFailure(type(error).__name__, phase) from None


__all__ = [
    "ClientLimits",
    "HttpResponse",
    "MaasClient",
    "MaasClientError",
    "MultipartPart",
    "Operation",
    "PreparedRequest",
    "RequestPhase",
    "RetryPolicy",
    "SSEEvent",
    "StreamInterrupted",
    "StreamingHttpResponse",
    "TransportFailure",
    "clear_token_cache",
    "error_result",
    "success_result",
]
