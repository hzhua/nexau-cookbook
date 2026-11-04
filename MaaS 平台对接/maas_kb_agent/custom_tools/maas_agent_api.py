"""MaaS platform agent tools: create session / chat / delete session.

Three tool functions exposed to the LLM, all built on `_maas_agent_client.py`
(token cache, retries, SSE streaming, secret redaction):

    maas_agent_create_session  ->  POST /interface/api/ai/create_session
    maas_agent_chat            ->  POST /interface/api/ai/chat   (JSON or SSE v2)
    maas_agent_delete_session  ->  POST /interface/api/chatConversation/delete

Moved here unchanged from the standalone tool package; only the file layout
and import path differ.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import urllib.parse
from typing import Any

from custom_tools._maas_agent_client import (
    MaasClient,
    MaasClientError,
    Operation,
    SSEEvent,
    StreamInterrupted,
    error_result,
    resolve_agent_profile,
    success_result,
)


# ======================================================================
# maas_agent_create_session
# ======================================================================
_CREATE_SESSION_PATH = "/interface/api/ai/create_session"
_CREATE_SUCCESS_CODES = {"0", "200", "10000", "20000", "ok", "success"}


def _create_required(value: str, name: str, *, configuration: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        error_type = "configuration_error" if configuration else "invalid_parameter"
        raise MaasClientError(error_type, f"{name} is required and must not be empty")
    return value.strip()


def _conversation_id(payload: dict[str, Any]) -> str:
    """Extract a conversation ID from documented and compatible wrappers."""
    current: Any = payload
    for _ in range(4):
        if isinstance(current, str) and current.strip():
            return current.strip()
        if not isinstance(current, dict):
            break
        for key in ("conversation_id", "conversationId", "id"):
            value = current.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        current = current.get("data")
    raise MaasClientError(
        "invalid_upstream_response",
        "MAAS create-session response omitted conversation_id",
    )


def _create_validate_business_response(payload: dict[str, Any]) -> None:
    """Require positive endpoint evidence, including nested response wrappers."""
    evidence = False
    current: Any = payload
    for _ in range(3):
        if not isinstance(current, dict):
            break
        detail = current.get("detail")
        if detail not in (None, "", [], {}):
            raise MaasClientError("upstream_error", "MAAS create-session reported a business failure")
        for key in ("success", "result", "succeed"):
            if key in current:
                evidence = True
                if current[key] is not True and current[key] != 1:
                    raise MaasClientError("upstream_error", "MAAS create-session reported a business failure")
        for key in ("code", "state", "resultCode"):
            if key in current:
                evidence = True
                if str(current[key]).strip().lower() not in _CREATE_SUCCESS_CODES:
                    raise MaasClientError("upstream_error", "MAAS create-session reported a business failure")
        nested = current.get("data")
        if not isinstance(nested, dict):
            break
        current = nested
    if not evidence:
        raise MaasClientError(
            "invalid_upstream_response",
            "MAAS create-session response omitted a success indicator",
        )


def _create_serialize_error(error: MaasClientError) -> str:
    return json.dumps(error_result(error), ensure_ascii=False)


def maas_agent_create_session(
    session_name: str,
    agent_alias: str | None = None,
    *,
    host: str,
    app_key: str,
    app_secret: str,
    user_id: str,
    agent_names: str,
    project_ids: str,
    application_ids: str,
    agent_secret_keys: str,
    default_agent: str,
    **_: Any,
) -> str:
    """Create a MAAS agent conversation.
    创建会话并裁剪为稳定的 conversation_id 契约。
    """
    try:
        selected_alias, profile = resolve_agent_profile(
            agent_names,
            project_ids,
            application_ids,
            agent_secret_keys,
            default_agent,
            agent_alias,
            use_default=True,
        )
        name = _create_required(session_name, "session_name")
        client = MaasClient(host=host, app_key=app_key, app_secret=app_secret, user_id=user_id)
        payload = client.request_json(
            Operation.CREATE,
            _CREATE_SESSION_PATH,
            {"application_id": profile.application_id, "session_name": name},
        )
        _create_validate_business_response(payload)
        return json.dumps(
            success_result(
                {
                    "agent_alias": selected_alias,
                    "conversation_id": _conversation_id(payload),
                }
            ),
            ensure_ascii=False,
        )
    except MaasClientError as error:
        return _create_serialize_error(error)
    except ValueError as error:
        return _create_serialize_error(MaasClientError("configuration_error", str(error), retryable=False))


# ======================================================================
# maas_agent_chat
# ======================================================================
_CHAT_PATH = "/interface/api/ai/chat"
_CHAT_SUCCESS_CODES = {"0", "200", "10000", "20000", "ok", "success"}
_FINISH_EVENT_TYPES = {"done", "end", "finish", "finished", "complete", "completed"}
_MAX_IMAGE_URL_CHARS = 4 * 1024 * 1024
_IMAGE_DATA_URL_PREFIXES = (
    "data:image/png;base64,",
    "data:image/jpeg;base64,",
    "data:image/gif;base64,",
    "data:image/webp;base64,",
)
_NODE_FIELDS = (
    "application_id",
    "node_id",
    "node_name",
    "node_type",
    "status",
    "state",
    "timestamp",
    "start_time",
    "end_time",
    "created_at",
    "updated_at",
)


def _chat_required(value: str, name: str, *, configuration: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        error_type = "configuration_error" if configuration else "invalid_parameter"
        raise MaasClientError(error_type, f"{name} is required and must not be empty")
    return value.strip()


def _chat_data(mode: str, conversation_id: str, agent_alias: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "agent_alias": agent_alias,
        "conversation_id": conversation_id,
        "role": None,
        "stage": None,
        "answer": "",
        "thinking": "",
        "questions": [],
        "answer_id": None,
        "task_id": None,
        "docs": [],
        "node_statuses": [],
        "timing": {"thinking_elapsed_secs": None, "exec_elapsed_secs": None},
        "unknown_event_count": 0,
    }


def _response_data(payload: dict[str, Any]) -> dict[str, Any]:
    current: Any = payload.get("data", payload)
    for _ in range(2):
        if not isinstance(current, dict):
            break
        failure = _wrapper_error(current)
        if failure is not None:
            raise failure
        nested = current.get("data")
        wrapper_keys = {"code", "state", "status", "message", "msg", "result", "succeed", "data"}
        if isinstance(nested, dict) and set(current).issubset(wrapper_keys):
            current = nested
            continue
        return current
    if not isinstance(current, dict):
        raise MaasClientError("invalid_upstream_response", "MAAS Chat response data must be an object")
    return current


def _wrapper_error(payload: dict[str, Any]) -> MaasClientError | None:
    failed = payload.get("detail") not in (None, "", [], {})
    for key in ("success", "result", "succeed"):
        if key in payload and payload[key] is not True and payload[key] != 1:
            failed = True
    upstream_code: str | int | None = None
    for key in ("code", "state", "resultCode"):
        if key in payload:
            value = payload[key]
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                upstream_code = value
            if str(value).strip().lower() not in _CHAT_SUCCESS_CODES:
                failed = True
    if not failed:
        return None
    message_values = [payload.get(key) for key in ("message", "msg", "error")]
    detail = payload.get("detail")
    if isinstance(detail, dict):
        message_values.extend(detail.get(key) for key in ("message", "msg", "error"))
    elif isinstance(detail, str):
        message_values.append(detail)
    message = " ".join(value for value in message_values if isinstance(value, str)).lower()
    code = str(upstream_code or "").lower()
    if code in {"401", "40101", "invalid_token", "token_expired", "unauthorized"} or any(
        term in message for term in ("unauthorized", "token expired", "token invalid", "token过期", "token失效", "未登录")
    ):
        error_type = "authentication_error"
    elif any(term in message for term in ("invalid parameter", "parameter error", "参数错误", "参数不合法")):
        error_type = "invalid_parameter"
    elif any(term in message for term in ("outcome unknown", "结果未知", "执行结果未知")):
        error_type = "outcome_unknown"
    else:
        error_type = "upstream_error"
    safe_code = upstream_code if isinstance(upstream_code, int) and not isinstance(upstream_code, bool) else None
    return MaasClientError(
        error_type,
        "MAAS Chat reported a business failure",
        retryable=False,
        upstream_code=safe_code,
    )


def _business_error(payload: dict[str, Any]) -> MaasClientError | None:
    """Recognize documented business failures that may arrive under HTTP 200."""
    wrapper_failure = _wrapper_error(payload)
    error_candidates: list[str] = []
    answer_candidates: list[str] = []

    def collect(value: Any, depth: int = 0, *, answer: bool = False) -> None:
        if depth > 3:
            return
        if isinstance(value, str):
            (answer_candidates if answer else error_candidates).append(value)
        elif isinstance(value, dict):
            for key in ("message", "msg", "error", "detail"):
                if key in value:
                    collect(value[key], depth + 1)
            if "answer" in value:
                collect(value["answer"], depth + 1, answer=True)
            if "data" in value:
                collect(value["data"], depth + 1)

    collect(payload)
    error_text = " ".join(error_candidates).lower()
    known_answer_error = any(
        value.strip().startswith(("输入包含敏感词", "输入包含敏感内容", "应用获取失败", "智能体不存在"))
        or " 应用获取失败" in value
        for value in answer_candidates
    )
    combined = f"{error_text} {' '.join(answer_candidates) if known_answer_error else ''}".lower()
    if any(term in combined for term in ("敏感词", "敏感内容", "sensitive content")):
        return MaasClientError(
            "sensitive_content",
            "MAAS rejected the request because it contained sensitive content",
            retryable=False,
        )
    if any(
        term in combined
        for term in (
            "应用获取失败",
            "应用不存在",
            "智能体不存在",
            "agent not found",
            "application not found",
            "failed to get application",
        )
    ):
        return MaasClientError(
            "application_not_found",
            "The configured MAAS agent application was not found",
            retryable=False,
        )
    return wrapper_failure


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _last_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _doc_key(document: dict[str, Any]) -> str:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    identifiers = (
        document.get("id"),
        document.get("file_id"),
        document.get("doc_id"),
        metadata.get("file_id"),
        document.get("knowledge_base_id") or document.get("kb_id"),
        document.get("source") or metadata.get("source"),
        document.get("filename") or document.get("file_name"),
    )
    if any(value not in (None, "") for value in identifiers):
        return json.dumps(identifiers, ensure_ascii=False, sort_keys=True, default=str)
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _append_text(data: dict[str, Any], field: str, value: Any, limit: int) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str):
        raise MaasClientError("invalid_upstream_response", f"SSE {field} must be a string")
    if len(data[field]) + len(value) > limit:
        raise MaasClientError("response_limit_exceeded", f"Chat {field} character limit exceeded")
    data[field] += value


def _merge_docs(
    data: dict[str, Any],
    raw_docs: Any,
    seen_docs: set[str],
    limit: int,
) -> None:
    if raw_docs is None:
        return
    if not isinstance(raw_docs, list):
        raise MaasClientError("invalid_upstream_response", "Chat docs must be an array")
    for raw_document in raw_docs:
        if not isinstance(raw_document, dict):
            raise MaasClientError("invalid_upstream_response", "Chat docs must contain objects")
        key = _doc_key(raw_document)
        if key in seen_docs:
            continue
        if len(data["docs"]) >= limit:
            raise MaasClientError("response_limit_exceeded", "Chat document reference limit exceeded")
        seen_docs.add(key)
        data["docs"].append(raw_document)


def _update_common_fields(data: dict[str, Any], event_data: dict[str, Any]) -> None:
    answer_id = _last_string(event_data.get("id") or event_data.get("answer_id"))
    task_id = _last_string(event_data.get("task_id"))
    if answer_id is not None:
        data["answer_id"] = answer_id
    if task_id is not None:
        data["task_id"] = task_id
    if isinstance(event_data.get("role"), str):
        data["role"] = event_data["role"]
    stage = event_data.get("stage")
    if isinstance(stage, int) and not isinstance(stage, bool):
        data["stage"] = stage
    questions = event_data.get("questions")
    if isinstance(questions, list):
        data["questions"] = questions


def _update_timing(data: dict[str, Any], event_data: dict[str, Any]) -> bool:
    recognized = False
    for target, sources in (
        ("thinking_elapsed_secs", ("thinking_elapsed_secs", "thinking_time")),
        ("exec_elapsed_secs", ("exec_elapsed_secs", "execution_elapsed_secs", "exec_time")),
    ):
        for source in sources:
            if source in event_data:
                value = _finite_number(event_data[source])
                if value is not None:
                    data["timing"][target] = value
                recognized = True
                break
    return recognized


def _aggregate_event(
    data: dict[str, Any],
    event: SSEEvent,
    *,
    limits: Any,
    seen_docs: set[str],
) -> tuple[bool, bool]:
    """Aggregate one event and report ``(recognized, terminated)``.

    a clean transport EOF is not proof that the upstream finished
    producing the answer; only protocol-level completion evidence may succeed.
    """
    raw = event.data.strip()
    if not raw:
        return True, False
    if raw == "[DONE]":
        return True, True
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise MaasClientError("invalid_upstream_response", "MAAS SSE event contained invalid JSON") from error
    if not isinstance(payload, dict):
        raise MaasClientError("invalid_upstream_response", "MAAS SSE event must contain a JSON object")
    business_error = _business_error(payload)
    if business_error is not None:
        raise business_error
    event_data = _response_data(payload)
    _update_common_fields(data, event_data)
    recognized = _update_timing(data, event_data)
    # 真实 MaaS SSE v2 会在最后一个 ``type=answer`` 事件中附带总执行耗时，
    # 而不是发送独立的 ``type=exec_elapsed_secs`` 事件。有效总耗时即 PDF
    # 定义的终止序列证据；无该字段的普通 EOF 仍按流中断处理。
    terminated = _finite_number(event_data.get("exec_elapsed_secs")) is not None
    if "docs" in event_data:
        _merge_docs(data, event_data.get("docs"), seen_docs, limits.max_docs)
        recognized = True

    event_type = str(event_data.get("type") or event.event or "").strip().lower()
    if event_type == "answer":
        _append_text(data, "answer", event_data.get("answer"), limits.max_answer_chars)
        recognized = True
    elif event_type == "think":
        _append_text(data, "thinking", event_data.get("answer"), limits.max_thinking_chars)
        recognized = True
    elif event_type == "node_status":
        if len(data["node_statuses"]) >= limits.max_node_statuses:
            raise MaasClientError("response_limit_exceeded", "Chat node status limit exceeded")
        data["node_statuses"].append(
            {key: event_data[key] for key in _NODE_FIELDS if key in event_data}
        )
        recognized = True
    elif event_type in _FINISH_EVENT_TYPES:
        return True, True
    elif event_type in {
        "thinking_elapsed_secs",
        "thinking_time",
        "exec_elapsed_secs",
        "execution_elapsed_secs",
        "exec_time",
        "docs",
    }:
        recognized = True
    # The PDF's documented terminal sequence ends with total execution timing.
    terminated = terminated or event_type == "exec_elapsed_secs"
    return recognized, terminated


def _normalize_non_stream(
    payload: dict[str, Any], conversation_id: str, agent_alias: str, limits: Any
) -> dict[str, Any]:
    business_error = _business_error(payload)
    if business_error is not None:
        raise business_error
    source = _response_data(payload)
    data = _chat_data("non_stream", conversation_id, agent_alias)
    _update_common_fields(data, source)
    _append_text(data, "answer", source.get("answer", ""), limits.max_answer_chars)
    _append_text(data, "thinking", source.get("thinking", ""), limits.max_thinking_chars)
    _merge_docs(data, source.get("docs", []), set(), limits.max_docs)
    statuses = source.get("node_statuses", [])
    if not isinstance(statuses, list) or not all(isinstance(item, dict) for item in statuses):
        raise MaasClientError("invalid_upstream_response", "Chat node_statuses must be an array of objects")
    if len(statuses) > limits.max_node_statuses:
        raise MaasClientError("response_limit_exceeded", "Chat node status limit exceeded")
    data["node_statuses"] = statuses
    _update_timing(data, source)
    return data


def _redact_value(client: MaasClient, value: Any, explicit_secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        redactor = getattr(client, "redact_text", None)
        sanitized = redactor(value) if callable(redactor) else value
        for secret in explicit_secrets:
            if secret:
                sanitized = sanitized.replace(secret, "[REDACTED]")
        return sanitized
    if isinstance(value, list):
        return [_redact_value(client, item, explicit_secrets) for item in value]
    if isinstance(value, dict):
        return {
            key: _redact_value(client, item, explicit_secrets)
            for key, item in value.items()
        }
    return value


def _serialize_result(client: MaasClient, result: dict[str, Any], secrets: tuple[str, ...]) -> str:
    return json.dumps(_redact_value(client, result, secrets), ensure_ascii=False)


def _validate_image_reference(value: str, max_data_url_bytes: int) -> str:
    """Validate one bounded HTTPS image URL or supported image Data URL.
    Data URL 按严格 Base64 解码后的字节数实施硬上限。
    """
    if value.startswith("data:"):
        prefix = next((item for item in _IMAGE_DATA_URL_PREFIXES if value.startswith(item)), None)
        if prefix is None:
            raise MaasClientError(
                "invalid_parameter",
                "Chat image Data URL media type or encoding is not supported",
            )
        encoded = value[len(prefix) :]
        if not encoded:
            raise MaasClientError("invalid_parameter", "Chat image Data URL payload must not be empty")

        # 1. 先用编码长度拒绝必然超限的数据，避免为超限输入分配解码缓冲区。
        max_encoded_chars = ((max_data_url_bytes + 2) // 3) * 4
        if len(encoded) > max_encoded_chars:
            raise MaasClientError(
                "response_limit_exceeded",
                "Chat image Data URL decoded byte limit exceeded",
            )
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise MaasClientError(
                "invalid_parameter",
                "Chat image Data URL must contain valid Base64",
            ) from error
        if len(decoded) > max_data_url_bytes:
            raise MaasClientError(
                "response_limit_exceeded",
                "Chat image Data URL decoded byte limit exceeded",
            )
        return value

    if len(value) > _MAX_IMAGE_URL_CHARS:
        raise MaasClientError("invalid_parameter", "Chat HTTPS image URL is too long")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise MaasClientError(
            "invalid_parameter",
            "Chat image URL must be an absolute HTTPS URL without credentials",
        )
    return value


def _validate_files(files: list[Any] | None, limits: Any) -> list[Any] | None:
    if files is None:
        return None
    validated: list[Any] = []
    for item in files:
        if isinstance(item, str):
            validated.append(_validate_image_reference(item, limits.max_data_url_bytes))
        else:
            # 上传返回的结构化文件描述由 Tool Schema 和 T4 上传结果契约约束。
            validated.append(item)
    return validated


def _request_payload(
    *,
    conversation_id: str,
    text: str,
    stream: bool,
    using_context: bool,
    project_id: str,
    application_id: str,
    model_name: str | None,
    max_tokens: int | None,
    params: dict[str, Any] | None,
    knowledge_base_ids: list[str] | None,
    files: list[Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project_id": project_id,
        "application_id": application_id,
        "conversation_id": conversation_id,
        "text": text,
        "stream": stream,
        "using_context": using_context,
    }
    for key, value in (("model_name", model_name), ("max_tokens", max_tokens), ("params", params), ("files", files)):
        if value is not None:
            payload[key] = value
    if knowledge_base_ids is not None:
        payload["knowledge_base_params"] = {
            "knowledge_base_list": [
                {"knowledge_base_id": knowledge_base_id} for knowledge_base_id in knowledge_base_ids
            ]
        }
    if stream:
        payload["version"] = 2
    return payload


def _chat_serialize_error(
    error: MaasClientError,
    *,
    client: MaasClient | None = None,
    secrets: tuple[str, ...] = (),
    partial: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> str:
    result = error_result(error, partial=partial, warnings=warnings or ())
    if client is not None:
        return _serialize_result(client, result, secrets)
    return json.dumps(result, ensure_ascii=False)

def maas_agent_chat(
    conversation_id: str,
    text: str,
    agent_alias: str,
    stream: bool = True,
    using_context: bool = True,
    model_name: str | None = None,
    max_tokens: int | None = None,
    params: dict[str, Any] | None = None,
    knowledge_base_ids: list[str] | None = None,
    files: list[Any] | None = None,
    *,
    host: str,
    app_key: str,
    app_secret: str,
    user_id: str,
    agent_names: str,
    project_ids: str,
    application_ids: str,
    agent_secret_keys: str,
    default_agent: str,
    **_: Any,
) -> str:
    """Chat with a MAAS agent.
    统一非流式 JSON 与有界 SSE v2 聚合结果。
    """
    partial: dict[str, Any] | None = None
    warnings: list[str] = []
    iterator: Any = None
    client: MaasClient | None = None
    secrets = tuple(value for value in (app_key, app_secret) if isinstance(value, str))
    try:
        selected_alias, profile = resolve_agent_profile(
            agent_names,
            project_ids,
            application_ids,
            agent_secret_keys,
            default_agent,
            agent_alias,
        )
        secrets = (*secrets, profile.agent_secret_key)
        conversation = _chat_required(conversation_id, "conversation_id")
        prompt = _chat_required(text, "text")
        client = MaasClient(host=host, app_key=app_key, app_secret=app_secret, user_id=user_id)
        validated_files = _validate_files(files, client.limits)
        payload = _request_payload(
            conversation_id=conversation,
            text=prompt,
            stream=stream,
            using_context=using_context,
            project_id=profile.project_id,
            application_id=profile.application_id,
            model_name=model_name,
            max_tokens=max_tokens,
            params=params,
            knowledge_base_ids=knowledge_base_ids,
            files=validated_files,
        )
        headers = {"X-Secret-Key": profile.agent_secret_key}
        if not stream:
            response = client.request_json(
                Operation.CHAT,
                _CHAT_PATH,
                payload,
                extra_headers=headers,
            )
            return _serialize_result(
                client,
                success_result(
                    _normalize_non_stream(
                        response,
                        conversation,
                        selected_alias,
                        client.limits,
                    )
                ),
                secrets,
            )

        partial = _chat_data("stream", conversation, selected_alias)
        seen_docs: set[str] = set()
        terminated = False
        iterator = client.request_sse(_CHAT_PATH, payload, extra_headers=headers)
        for event in iterator:
            recognized, event_terminated = _aggregate_event(
                partial, event, limits=client.limits, seen_docs=seen_docs
            )
            terminated = terminated or event_terminated
            if not recognized:
                partial["unknown_event_count"] += 1
        if not terminated:
            raise StreamInterrupted(
                "MAAS SSE stream ended without a protocol completion event",
                0,
            )
        if partial["unknown_event_count"]:
            warnings.append(
                f"Ignored {partial['unknown_event_count']} unknown MAAS SSE event(s)."
            )
        return _serialize_result(client, success_result(partial, warnings), secrets)
    except StreamInterrupted as error:
        if partial is not None and partial["unknown_event_count"] and not warnings:
            warnings.append(
                f"Ignored {partial['unknown_event_count']} unknown MAAS SSE event(s)."
            )
        return _chat_serialize_error(
            error,
            client=client,
            secrets=secrets,
            partial=partial,
            warnings=warnings,
        )
    except MaasClientError as error:
        return _chat_serialize_error(error, client=client, secrets=secrets, warnings=warnings)
    except ValueError as error:
        return _chat_serialize_error(
            MaasClientError("configuration_error", str(error), retryable=False),
            client=client,
            secrets=secrets,
            warnings=warnings,
        )
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                # 关闭失败不得覆盖已归一化的主结果或把底层异常泄漏给宿主。
                pass


# ======================================================================
# maas_agent_delete_session
# ======================================================================
_DELETE_PATH = "/interface/api/chatConversation/delete"
_DELETE_SUCCESS_CODES = {"0", "200", "10000", "20000", "ok", "success"}


def _delete_required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MaasClientError("invalid_parameter", f"{name} is required and must not be empty")
    return value.strip()


def _delete_validate_business_response(payload: dict[str, Any]) -> None:
    evidence = False
    current: Any = payload
    for _ in range(3):
        if not isinstance(current, dict):
            break
        if current.get("detail") not in (None, "", [], {}):
            raise MaasClientError("upstream_error", "MAAS Delete reported a business failure")
        for key in ("success", "result", "succeed"):
            if key in current:
                evidence = True
                if current[key] is not True and current[key] != 1:
                    raise MaasClientError("upstream_error", "MAAS Delete reported a business failure")
        for key in ("code", "state", "resultCode"):
            if key in current:
                evidence = True
                if str(current[key]).strip().lower() not in _DELETE_SUCCESS_CODES:
                    raise MaasClientError("upstream_error", "MAAS Delete reported a business failure")
        nested = current.get("data")
        if not isinstance(nested, dict):
            break
        current = nested
    if not evidence:
        raise MaasClientError(
            "invalid_upstream_response",
            "MAAS Delete response omitted a success indicator",
        )


def _delete_serialize_error(error: MaasClientError) -> str:
    return json.dumps(error_result(error), ensure_ascii=False)


def maas_agent_delete_session(
    conversation_id: str,
    *,
    host: str,
    app_key: str,
    app_secret: str,
    user_id: str,
    **_: Any,
) -> str:
    """Delete a MAAS agent conversation.
    删除仅由显式 Tool 调用触发，不参与 Chat 自动清理。
    """
    try:
        conversation = _delete_required(conversation_id, "conversation_id")
        client = MaasClient(host=host, app_key=app_key, app_secret=app_secret, user_id=user_id)
        payload = client.request_json(Operation.DELETE, _DELETE_PATH, {"id": conversation})
        _delete_validate_business_response(payload)
        return json.dumps(
            success_result({"conversation_id": conversation, "deleted": True}),
            ensure_ascii=False,
        )
    except MaasClientError as error:
        return _delete_serialize_error(error)
    except ValueError as error:
        return _delete_serialize_error(MaasClientError("configuration_error", str(error), retryable=False))

