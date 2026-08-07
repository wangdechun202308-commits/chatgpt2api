from __future__ import annotations

import time
import uuid
from typing import Any, Iterable, Iterator

from fastapi import HTTPException

from services.protocol.chat_completion_cache import cache_key, chat_completion_cache, normalize_text_messages
from services.protocol.conversation import (
    ConversationRequest,
    ImageOutput,
    collect_image_outputs,
    collect_text,
    count_message_image_tokens,
    count_message_text_tokens,
    count_text_tokens,
    encode_images,
    normalize_messages,
    stream_image_outputs_with_pool,
    stream_text_deltas,
    text_backend,
)
from services.protocol.reasoning import thinking_effort_from_body
from services.protocol.web_search_tool import (
    WEB_SEARCH_TOOL_TYPES,
    has_unsupported_tools,
    is_web_search_chat_request,
    run_web_search,
    search_query_from_messages,
    text_with_url_citations,
)
from utils.helper import build_chat_image_markdown_content, extract_chat_image, extract_chat_prompt, is_image_chat_request, parse_image_count
from utils.image_tokens import (
    chat_usage_from_image_usage,
    count_image_inputs_tokens,
    count_image_output_items_tokens,
    image_usage,
)

TOOL_UNAVAILABLE_SYSTEM_MESSAGE = (
    "This compatibility backend cannot execute local tools, shell commands, non-search tools, "
    "or file operations. Do not claim to have run tools or inspected external resources. "
    "If a user asks you to use a tool, say that tool execution is unavailable through this backend."
)


# JSON_TOOL_ROUND_LIMIT_20260806
MAX_TOOL_ROUNDS = 6

TOOL_ROUND_LIMIT_SYSTEM_MESSAGE = (
    "本次对话已经达到工具调用轮次上限。"
    "不得再输出任何 <tool_call> 标签或请求调用工具。"
    "请只根据用户消息和已经返回的真实工具结果完成回答；"
    "如现有信息不足，请明确说明工具调用轮次已用尽。"
)


def completion_chunk(model: str, delta: dict[str, Any], finish_reason: str | None = None, completion_id: str = "", created: int | None = None) -> dict[str, Any]:
    return {
        "id": completion_id or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def completion_response(
    model: str,
    content: str,
    created: int | None = None,
    messages: list[dict[str, Any]] | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prompt_text_tokens = count_message_text_tokens(messages, model) if messages else 0
    prompt_image_tokens = count_message_image_tokens(messages, model) if messages else 0
    prompt_tokens = prompt_text_tokens + prompt_image_tokens
    completion_tokens = count_text_tokens(content, model) if messages else 0
    message = {"role": "assistant", "content": content}
    if annotations:
        message["annotations"] = annotations
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {
                "text_tokens": prompt_text_tokens,
                "image_tokens": prompt_image_tokens,
                "cached_tokens": 0,
            },
            "completion_tokens_details": {
                "text_tokens": completion_tokens,
                "image_tokens": 0,
                "reasoning_tokens": 0,
            },
        },
    }


def _with_log_metadata(
    payload: dict[str, Any],
    account_email: str = "",
    conversation_id: str = "",
    image_urls: Iterable[str] | None = None,
    image_attempts: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if account_email:
        payload["_account_email"] = account_email
    if conversation_id:
        payload["_conversation_id"] = conversation_id
    urls = [str(url).strip() for url in image_urls or [] if str(url).strip()]
    if urls:
        payload["_image_urls"] = list(dict.fromkeys(urls))
    attempts = [dict(item) for item in image_attempts or [] if isinstance(item, dict)]
    if attempts:
        payload["_image_attempts"] = attempts
    return payload


def _backend_account_email(backend: object) -> str:
    return str(getattr(backend, "account_email", "") or "").strip()


# JSON_TOOL_BRIDGE_STREAM_WIRING_20260806
def stream_text_chat_completion(
    backend,
    messages: list[dict[str, Any]],
    model: str,
    thinking_effort: str = "",
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    attempt_messages = messages
    text = ""
    calls: list[tuple[str, dict[str, Any]]] = []
    tool_error = ""

    for attempt in range(
        1,
        FORCED_TOOL_MAX_ATTEMPTS + 1,
    ):
        request = ConversationRequest(
            model=model,
            messages=attempt_messages,
            thinking_effort=thinking_effort,
        )

        text_parts = []

        for delta_text in stream_text_deltas(
            backend,
            request,
        ):
            text_parts.append(delta_text)

        text = "".join(text_parts)

        calls, tool_error = parse_strict_json_tool_call(
            text,
            tools,
            tool_choice,
        )

        tool_error = _classify_tool_output_error(
            text,
            tool_error,
            messages,
            tools,
            tool_choice,
        )

        if not _should_retry_tool_output(
            tool_choice,
            tool_error,
            attempt,
        ):
            break

        attempt_messages = _tool_retry_messages(
            messages,
            tools,
            tool_choice,
            tool_error,
            attempt + 1,
        )

    # 模型试图输出工具包络，但格式、白名单或参数校验失败时，
    # 拒绝产生 tool_calls，也不把原始伪工具文本继续发给客户端。
    if tool_error not in {"", "not_tool_call"}:
        text = f"[JSON Tool Bridge 拒绝调用：{tool_error}]"

    if calls:
        yield _with_log_metadata(
            completion_chunk(
                model,
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": i,
                            "id": f"call_{uuid.uuid4().hex}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                        for i, (name, args) in enumerate(calls)
                    ],
                },
                None,
                completion_id,
                created,
            ),
            _backend_account_email(backend),
        )

        yield _with_log_metadata(
            completion_chunk(
                model,
                {},
                "tool_calls",
                completion_id,
                created,
            ),
            _backend_account_email(backend),
        )
        return

    yield _with_log_metadata(
        completion_chunk(
            model,
            {
                "role": "assistant",
                "content": text,
            },
            None,
            completion_id,
            created,
        ),
        _backend_account_email(backend),
    )

    yield _with_log_metadata(
        completion_chunk(
            model,
            {},
            "stop",
            completion_id,
            created,
        ),
        _backend_account_email(backend),
    )


def collect_chat_content(chunks: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for chunk in chunks:
        choices = chunk.get("choices")
        first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
        content = str(delta.get("content") or "")
        if content:
            parts.append(content)
    return "".join(parts)


def chat_messages_from_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        return [message for message in messages if isinstance(message, dict)]
    prompt = str(body.get("prompt") or "").strip()
    if prompt:
        return [{"role": "user", "content": prompt}]
    raise HTTPException(status_code=400, detail={"error": "messages or prompt is required"})


def chat_image_args(body: dict[str, Any]) -> tuple[str, str, int, list[tuple[bytes, str, str]], str | None]:
    model = str(body.get("model") or "gpt-image-2").strip() or "gpt-image-2"
    prompt = extract_chat_prompt(body)
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": "prompt is required"})
    images = [
        (data, f"image_{idx}.png", mime)
        for idx, (data, mime) in enumerate(extract_chat_image(body), start=1)
    ]
    base_url = str(body.get("base_url") or "").strip() or None
    return model, prompt, parse_image_count(body.get("n")), images, base_url


def completed_tool_rounds(
    messages: list[dict[str, Any]],
) -> int:
    """
    统计已经完成的工具轮次。

    M6_ORDERED_TOOL_ROUND_STATE_20260806

    一条 assistant 消息中的多个 tool_calls 属于同一轮；
    只有该轮全部调用都收到后续 role=tool 结果时，
    才计为完成。

    安全边界：
    1. 工具结果只能完成此前已经声明的调用；
    2. 每条工具结果最多消费一次；
    3. call_id 不得跨轮或在同一轮重复声明；
    4. 非法重复轮次不进入完成轮次统计。
    """
    pending_rounds: list[set[str]] = []
    seen_call_ids: set[str] = set()
    completed = 0

    for message in messages:
        if not isinstance(message, dict):
            continue

        role = str(message.get("role") or "")

        if (
            role == "assistant"
            and isinstance(message.get("tool_calls"), list)
        ):
            raw_call_ids = [
                str(call.get("id") or "").strip()
                for call in message["tool_calls"]
                if (
                    isinstance(call, dict)
                    and str(call.get("id") or "").strip()
                )
            ]

            call_ids = set(raw_call_ids)

            if not call_ids:
                continue

            duplicate_inside_round = (
                len(raw_call_ids) != len(call_ids)
            )

            reused_across_rounds = bool(
                call_ids & seen_call_ids
            )

            # 无论该轮是否合法，已经出现的 ID 都视为已声明，
            # 防止它稍后再次伪装成新的独立调用。
            seen_call_ids.update(call_ids)

            if (
                duplicate_inside_round
                or reused_across_rounds
            ):
                continue

            pending_rounds.append(call_ids)
            continue

        if role != "tool":
            continue

        call_id = str(
            message.get("tool_call_id") or ""
        ).strip()

        if not call_id:
            continue

        # 一条工具结果只能交付给最早等待该 ID 的一轮。
        # 匹配完成后立即停止，不得同时完成多个轮次。
        for index, pending_call_ids in enumerate(
            pending_rounds
        ):
            if call_id not in pending_call_ids:
                continue

            pending_call_ids.remove(call_id)

            if not pending_call_ids:
                completed += 1
                pending_rounds.pop(index)

            break

    return completed


def _tool_round_limit_reached(
    body: dict[str, Any],
) -> bool:
    raw_messages = chat_messages_from_body(body)

    return (
        completed_tool_rounds(raw_messages)
        >= MAX_TOOL_ROUNDS
    )


def _effective_tool_choice_from_body(
    body: dict[str, Any],
) -> str | dict[str, Any] | None:
    if _tool_round_limit_reached(body):
        return "none"

    return body.get("tool_choice")


def _bridge_openai_tool_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 OpenAI 工具历史转换为 ChatGPT Web 能理解的普通消息。"""
    converted: list[dict[str, Any]] = []
    pending_calls: dict[str, tuple[str, str]] = {}

    for message in messages:
        role = str(message.get("role") or "user")

        # Hermes 返回的 assistant.tool_calls：
        # 保存调用信息，但不把空 assistant 消息传给 Web 后端。
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                converted.append({
                    "role": "assistant",
                    "content": content,
                })

            for call in message["tool_calls"]:
                if not isinstance(call, dict):
                    continue

                function = call.get("function")
                if not isinstance(function, dict):
                    continue

                call_id = str(call.get("id") or "")
                name = str(function.get("name") or "unknown")
                arguments = function.get("arguments")

                if not isinstance(arguments, str):
                    arguments = str(arguments or "{}")

                if call_id:
                    pending_calls[call_id] = (name, arguments)

            continue

        # Hermes 已执行完成的 role=tool：
        # 转成普通 user 消息，让 ChatGPT Web 根据真实结果继续回答。
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            name, arguments = pending_calls.get(
                call_id,
                ("unknown", "{}"),
            )

            result = message.get("content", "")
            if not isinstance(result, str):
                result = str(result)

            converted.append({
                "role": "user",
                "content": (
                    "以下是 Hermes 已实际执行的工具结果，请根据它继续完成原任务。\n"
                    f"工具：{name}\n"
                    f"参数：{arguments}\n"
                    f"结果：\n{result}\n"
                    "仅在确有必要时才再次调用工具。"
                ),
            })
            continue

        converted.append(message)

    return converted


def text_chat_parts(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    model = str(body.get("model") or "auto").strip() or "auto"
    raw_messages = chat_messages_from_body(body)
    tool_rounds = completed_tool_rounds(raw_messages)
    tool_limit_reached = tool_rounds >= MAX_TOOL_ROUNDS

    bridged_messages = _bridge_openai_tool_history(raw_messages)
    messages = normalize_text_messages(
        normalize_messages(bridged_messages)
    )

    json_tool_prompt = ""

    if not tool_limit_reached:
        json_tool_prompt = build_json_tool_bridge_prompt(
            body.get("tools"),
            body.get("tool_choice"),
        )

    if (
        tool_limit_reached
        and normalized_function_tools(body.get("tools"))
    ):
        messages.insert(
            0,
            {
                "role": "system",
                "content": (
                    TOOL_ROUND_LIMIT_SYSTEM_MESSAGE
                    + f" 已完成工具轮次：{tool_rounds}；"
                    + f"最大允许轮次：{MAX_TOOL_ROUNDS}。"
                ),
            },
        )
    elif json_tool_prompt:
        bridge_message = {
            "role": "system",
            "content": json_tool_prompt,
        }

        # JSON Tool Bridge 协议必须紧邻本轮最新 user 消息。
        #
        # 工具结果会由 _bridge_openai_tool_history 转换成新的 user
        # 消息。如果 auto 模式仍把协议放在整个历史最前面，模型可能
        # 错误判断后续轮次已经没有可用工具。
        #
        # 是否必须调用工具仍由 tool_choice 和协议内容决定；这里只
        # 修正协议位置，不把 auto 改为强制模式。
        insert_at = len(messages)

        for index in range(
            len(messages) - 1,
            -1,
            -1,
        ):
            message = messages[index]

            if (
                isinstance(message, dict)
                and message.get("role") == "user"
            ):
                insert_at = index
                break

        messages.insert(
            insert_at,
            bridge_message,
        )
    elif has_unsupported_tools(body, WEB_SEARCH_TOOL_TYPES):
        messages.insert(
            0,
            {
                "role": "system",
                "content": TOOL_UNAVAILABLE_SYSTEM_MESSAGE,
            },
        )

    return model, messages


def chat_completion_annotations(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for item in annotations:
        if item.get("type") != "url_citation":
            continue
        output.append({
            "type": "url_citation",
            "url_citation": {
                "start_index": item.get("start_index", 0),
                "end_index": item.get("end_index", 0),
                "url": item.get("url", ""),
                "title": item.get("title", ""),
            },
        })
    return output


def web_search_chat_response(messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
    query = search_query_from_messages(messages)
    if not query:
        raise HTTPException(status_code=400, detail={"error": "messages or prompt is required for web search"})
    text, annotations = text_with_url_citations(run_web_search(query))
    return completion_response(
        model,
        text,
        messages=messages,
        annotations=chat_completion_annotations(annotations),
    )


def stream_web_search_chat_completion(messages: list[dict[str, Any]], model: str) -> Iterator[dict[str, Any]]:
    query = search_query_from_messages(messages)
    if not query:
        raise HTTPException(status_code=400, detail={"error": "messages or prompt is required for web search"})
    text, _annotations = text_with_url_citations(run_web_search(query))
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    yield completion_chunk(model, {"role": "assistant", "content": text}, None, completion_id, created)
    yield completion_chunk(model, {}, "stop", completion_id, created)


def image_result_content(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, list) and data:
        return build_chat_image_markdown_content(result)
    return str(result.get("message") or "Image generation completed.")


def image_chat_response(body: dict[str, Any]) -> dict[str, Any]:
    model, prompt, n, images, base_url = chat_image_args(body)
    result = collect_image_outputs(stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        response_format="b64_json",
        images=encode_images(images) or None,
        base_url=base_url,
        message_as_error=True,
        call_id=str(body.get("_call_id") or ""),
        trace_image_perf=bool(body.get("_trace_image_perf")),
    )))
    response = completion_response(model, image_result_content(result), int(result.get("created") or 0) or None)
    usage = image_usage(
        input_text_tokens=count_text_tokens(prompt, model),
        input_image_tokens=count_image_inputs_tokens(images, model),
        output_tokens=count_image_output_items_tokens(result.get("data")),
    )
    response["usage"] = chat_usage_from_image_usage(usage)
    _with_log_metadata(
        response,
        str(result.get("_account_email") or ""),
        str(result.get("_conversation_id") or ""),
        result.get("_image_urls") if isinstance(result.get("_image_urls"), list) else None,
        result.get("_image_attempts") if isinstance(result.get("_image_attempts"), list) else None,
    )
    return response


def image_chat_events(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    model, prompt, n, images, base_url = chat_image_args(body)
    image_outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        response_format="b64_json",
        images=encode_images(images) or None,
        base_url=base_url,
        message_as_error=True,
        call_id=str(body.get("_call_id") or ""),
        trace_image_perf=bool(body.get("_trace_image_perf")),
    ))
    yield from stream_image_chat_completion(image_outputs, model)


def stream_image_chat_completion(image_outputs: Iterable[ImageOutput], model: str) -> Iterator[dict[str, Any]]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    sent_role = False
    sent_text = ""
    for output in image_outputs:
        content = ""
        if output.kind == "progress":
            content = output.text
            sent_text += content
        elif output.kind == "result":
            content = build_chat_image_markdown_content({"data": output.data})
        elif output.kind == "message":
            content = output.text[len(sent_text):] if output.text.startswith(sent_text) else output.text
        if not content:
            continue
        if not sent_role:
            sent_role = True
            yield _with_log_metadata(
                completion_chunk(model, {"role": "assistant", "content": content}, None, completion_id, created),
                output.account_email,
                output.conversation_id,
                output.image_urls,
                output.image_attempts,
            )
        else:
            yield _with_log_metadata(
                completion_chunk(model, {"content": content}, None, completion_id, created),
                output.account_email,
                output.conversation_id,
                output.image_urls,
                output.image_attempts,
            )
    if not sent_role:
        yield completion_chunk(model, {"role": "assistant", "content": ""}, None, completion_id, created)
    yield completion_chunk(model, {}, "stop", completion_id, created)


# JSON_TOOL_BRIDGE_NONSTREAM_WIRING_20260806
def text_completion_response(
    model: str,
    messages: list[dict[str, Any]],
    thinking_effort: str,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    非流式严格 JSON Tool Bridge。

    只负责：
    1. 获取模型文本；
    2. 严格解析工具包络；
    3. 转成 OpenAI tool_calls。

    不执行任何工具，不自动写文件，不根据关键词伪造工具调用。
    """
    backend = text_backend()
    attempt_messages = messages
    text = ""
    calls: list[tuple[str, dict[str, Any]]] = []
    tool_error = ""

    for attempt in range(
        1,
        FORCED_TOOL_MAX_ATTEMPTS + 1,
    ):
        text = collect_text(
            backend,
            ConversationRequest(
                model=model,
                messages=attempt_messages,
                thinking_effort=thinking_effort,
            ),
        )

        calls, tool_error = parse_strict_json_tool_call(
            text,
            tools,
            tool_choice,
        )

        tool_error = _classify_tool_output_error(
            text,
            tool_error,
            messages,
            tools,
            tool_choice,
        )

        if not _should_retry_tool_output(
            tool_choice,
            tool_error,
            attempt,
        ):
            break

        attempt_messages = _tool_retry_messages(
            messages,
            tools,
            tool_choice,
            tool_error,
            attempt + 1,
        )

    if calls:
        response = completion_response(
            model,
            "",
            messages=messages,
        )

        response["choices"][0]["message"]["content"] = None
        response["choices"][0]["message"]["tool_calls"] = [
            {
                "id": f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(
                        arguments,
                        ensure_ascii=False,
                    ),
                },
            }
            for name, arguments in calls
        ]

        response["choices"][0]["finish_reason"] = "tool_calls"

    elif tool_error not in {"", "not_tool_call"}:
        response = completion_response(
            model,
            f"[JSON Tool Bridge 拒绝调用：{tool_error}]",
            messages=messages,
        )

    else:
        response = completion_response(
            model,
            text,
            messages=messages,
        )

    return _with_log_metadata(
        response,
        _backend_account_email(backend),
    )


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    if body.get("stream"):
        if is_image_chat_request(body):
            return image_chat_events(body)
        model, messages = text_chat_parts(body)
        if is_web_search_chat_request(body) and not has_unsupported_tools(body, WEB_SEARCH_TOOL_TYPES):
            return stream_web_search_chat_completion(messages, model)
        thinking_effort = thinking_effort_from_body(body)
        effective_tool_choice = _effective_tool_choice_from_body(body)
        # M6_FUNCTION_TOOL_CACHE_BYPASS_20260806
        # 可执行 function tool 响应不得进入通用聊天缓存，
        # 防止缓存命中时重放旧 tool_call_id 和旧工具参数。
        if normalized_function_tools(body.get("tools")):
            return stream_text_chat_completion(
                text_backend(),
                messages,
                model,
                thinking_effort,
                body.get("tools"),
                effective_tool_choice,
            )

        key = cache_key(body, messages, stream=True)
        return chat_completion_cache.get_or_compute_stream(
            key,
            lambda: stream_text_chat_completion(
                text_backend(),
                messages,
                model,
                thinking_effort,
                body.get("tools"),
                effective_tool_choice,
            ),
        )
    if is_image_chat_request(body):
        return image_chat_response(body)
    model, messages = text_chat_parts(body)
    if is_web_search_chat_request(body) and not has_unsupported_tools(body, WEB_SEARCH_TOOL_TYPES):
        return web_search_chat_response(messages, model)
    thinking_effort = thinking_effort_from_body(body)
    effective_tool_choice = _effective_tool_choice_from_body(body)
    # M6_FUNCTION_TOOL_CACHE_BYPASS_20260806
    # 非流式 function tool 请求同样绕过缓存。
    if normalized_function_tools(body.get("tools")):
        return text_completion_response(
            model,
            messages,
            thinking_effort,
            body.get("tools"),
            effective_tool_choice,
        )

    key = cache_key(body, messages, stream=False)
    return chat_completion_cache.get_or_compute_response(
        key,
        lambda: text_completion_response(
                model,
                messages,
                thinking_effort,
                body.get("tools"),
                effective_tool_choice,
            ),
    )


# ============ Tool Bridge Functions ============
import json
from typing import Any, Dict, List


# JSON_TOOL_BRIDGE_HELPERS_20260806

JSON_TOOL_OPEN_TAG = "<tool_call>"
JSON_TOOL_CLOSE_TAG = "</tool_call>"


def normalized_function_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """提取本轮实际允许使用的 function tools。"""
    normalized: list[dict[str, Any]] = []

    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            continue

        function = tool.get("function")
        if not isinstance(function, dict):
            continue

        name = str(function.get("name") or "").strip()
        if not name:
            continue

        description = str(function.get("description") or "").strip()
        parameters = function.get("parameters")

        # 严格工具边界：缺失或非法 parameters 不得静默降级为
        # 可调用的空参数 Schema；显式 parameters={} 仍然合法。
        if not isinstance(parameters, dict):
            continue

        normalized.append({
            "name": name,
            "description": description,
            "parameters": parameters,
        })

    return normalized


def build_json_tool_bridge_prompt(
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None = None,
) -> str:
    """把真实工具 Schema 写成模型可见的严格协议。"""
    available_tools = normalized_function_tools(tools)

    if not available_tools:
        return ""

    forced_name = _forced_function_name(tool_choice)

    if forced_name:
        available_tools = [
            tool
            for tool in available_tools
            if tool.get("name") == forced_name
        ]

        if not available_tools:
            return ""

    protocol = {
        "available_tools": available_tools,
        "tool_choice": (
            tool_choice
            if tool_choice is not None
            else "auto"
        ),
        "forced_tool_name": forced_name or None,
        "required_output": (
            '<tool_call>{"name":"工具名",'
            '"arguments":{"参数名":"参数值"}}</tool_call>'
        ),
    }

    if forced_name:
        mode_rules = (
            f"本轮 tool_choice 已指定必须编码且只能编码对 {forced_name} 的调用请求。\n"
            f"{forced_name} 是外部客户端的工具名称，不要求你本人拥有或访问它。\n"
            "不得输出普通回答、拒绝说明、模拟结果、搜索语法、"
            "Shell 命令文本或任何其他内容。\n"
            "即使你认为可以直接回答，也必须按照工具 Schema "
            "生成发送给外部客户端的调用请求。\n"
        )
    elif tool_choice == "none":
        mode_rules = (
            "本轮禁止调用任何工具。\n"
            "请直接输出正常回答，不得输出 tool_call 标签。\n"
        )
    else:
        mode_rules = (
            "只有当任务确实需要外部客户端执行工具时，才编码工具调用请求。\n"
            "不需要外部工具时，直接输出正常回答。\n"
        )

    return (
        "你是外部 Function Call 协议编码器，不是工具执行器。\n"
        "你不需要拥有或访问下面的工具；你的职责是生成发送给外部客户端的调用请求。\n"
        "<tool_call> 只表示请求外部客户端执行，不表示你本人已经执行。\n"
        "不得因你本人不能访问工具而拒绝编码调用请求；不得模拟或填写执行结果。\n"
        "工具定义和参数 Schema 如下：\n"
        + json.dumps(
            protocol,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n"
        + mode_rules
        + "\n严格规则：\n"
        "1. 工具调用必须且只能输出一个 "
        "<tool_call>...</tool_call> 包络。\n"
        "2. 标签内部必须是一个完整、合法的 JSON 对象。\n"
        '3. JSON 只能包含两个字段："name" 和 "arguments"。\n'
        "4. name 必须来自 available_tools。\n"
        "5. arguments 必须是 JSON 对象，并符合对应 "
        "parameters Schema。\n"
        "6. 工具调用前后不得出现解释、Markdown、代码围栏、"
        "搜索语法或执行结果。\n"
        "7. 不得声称工具已经执行；实际执行由外部客户端完成。\n"
    )


def _json_value_matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
    if expected == "null":
        return value is None
    return False


def validate_tool_arguments(
    value: Any,
    schema: dict[str, Any],
    path: str = "$",
) -> list[str]:
    """
    校验 Hermes 常用 JSON Schema 子集。

    支持：
    type、required、properties、additionalProperties、
    enum、items、minLength、maxLength、minimum、maximum。
    """
    errors: list[str] = []

    if not isinstance(schema, dict):
        return [f"{path}: schema 不是对象"]

    if "enum" in schema:
        enum_values = schema.get("enum")
        if isinstance(enum_values, list) and value not in enum_values:
            errors.append(f"{path}: 值不在 enum 范围内")
            return errors

    expected_type = schema.get("type")

    if isinstance(expected_type, list):
        if not any(
            isinstance(item, str)
            and _json_value_matches_type(value, item)
            for item in expected_type
        ):
            errors.append(
                f"{path}: 类型不符合 {expected_type}"
            )
            return errors
    elif isinstance(expected_type, str):
        if not _json_value_matches_type(value, expected_type):
            errors.append(
                f"{path}: 类型应为 {expected_type}"
            )
            return errors

    if isinstance(value, dict):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}

        required = schema.get("required")
        if not isinstance(required, list):
            required = []

        for key in required:
            if isinstance(key, str) and key not in value:
                errors.append(f"{path}.{key}: 缺少必需参数")

        # 为了桥接安全，Schema 未明确允许额外参数时默认拒绝。
        additional = schema.get("additionalProperties", False)

        for key, item in value.items():
            child_path = f"{path}.{key}"

            if key in properties:
                property_schema = properties[key]
                if isinstance(property_schema, dict):
                    errors.extend(
                        validate_tool_arguments(
                            item,
                            property_schema,
                            child_path,
                        )
                    )
                continue

            if additional is True:
                continue

            if isinstance(additional, dict):
                errors.extend(
                    validate_tool_arguments(
                        item,
                        additional,
                        child_path,
                    )
                )
                continue

            errors.append(f"{child_path}: 不允许的额外参数")

    if isinstance(value, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for index, item in enumerate(value):
                errors.extend(
                    validate_tool_arguments(
                        item,
                        items_schema,
                        f"{path}[{index}]",
                    )
                )

    if isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")

        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"{path}: 字符串长度小于 {min_length}")

        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(f"{path}: 字符串长度大于 {max_length}")

    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")

        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}: 数值小于 {minimum}")

        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path}: 数值大于 {maximum}")

    return errors


def _forced_function_name(
    tool_choice: str | dict[str, Any] | None,
) -> str:
    if not isinstance(tool_choice, dict):
        return ""

    if tool_choice.get("type") != "function":
        return ""

    function = tool_choice.get("function")
    if not isinstance(function, dict):
        return ""

    return str(function.get("name") or "").strip()


# 强制工具输出允许更多模型级尝试；普通污染与 post-tool
# 恢复仍保持独立的三次上限。
FORCED_TOOL_MAX_ATTEMPTS = 5
AUTO_TOOL_RECOVERY_MAX_ATTEMPTS = 3

FORCED_TOOL_RETRYABLE_ERRORS = {
    "forced_tool_call_required",
    "invalid_envelope",
    "empty_payload",
    "invalid_json",
    "payload_not_object",
    "invalid_payload_fields",
    "invalid_tool_name",
    "arguments_not_object",
    "unknown_tool",
    "tool_choice_mismatch",
}


def _should_retry_forced_tool_output(
    tool_choice: str | dict[str, Any] | None,
    tool_error: str,
    attempt: int,
) -> bool:
    """仅为强制工具模式重试无效模型输出。"""
    if not _forced_function_name(tool_choice):
        return False

    if attempt >= FORCED_TOOL_MAX_ATTEMPTS:
        return False

    if tool_error in FORCED_TOOL_RETRYABLE_ERRORS:
        return True

    return tool_error.startswith("schema_error:")


def _forced_tool_retry_messages(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
    next_attempt: int,
) -> list[dict[str, Any]]:
    """
    构造最小化强制工具重试请求。

    第一次模型输出被完全丢弃。重试时不复制原 Hermes
    developer/system 工具语法，只保留一条最高优先级严格协议
    和当前用户请求。
    """
    forced_name = _forced_function_name(tool_choice)

    forced_tools: list[dict[str, Any]] = []

    for tool in tools or []:
        if not isinstance(tool, dict):
            continue

        function = tool.get("function")

        if not isinstance(function, dict):
            continue

        if (
            str(function.get("name") or "").strip()
            == forced_name
        ):
            forced_tools.append(tool)

    strict_prompt = build_json_tool_bridge_prompt(
        forced_tools,
        tool_choice,
    )

    correction = (
        f"这是强制工具协议的第 {next_attempt} 次全新尝试。\n"
        "之前的模型输出已经被完全丢弃，不得引用、续写、"
        "修补或解释之前的输出。\n"
        f"本轮必须编码且只能编码对工具 {forced_name} 的调用请求。\n"
        "请只根据当前用户请求和上方工具 Schema 生成 arguments；不要判断你本人是否能访问该工具。\n"
        "最终输出必须从 <tool_call> 开始，"
        "并以 </tool_call> 结束。\n"
        "包络前后不得出现任何字符。\n"
        "禁止输出 search(...)、search_files、Shell 命令文本、"
        "执行结果、自然语言、Markdown、空回答或拒绝说明。"
    )

    latest_user: dict[str, Any] | None = None

    for message in reversed(messages):
        if (
            isinstance(message, dict)
            and message.get("role") == "user"
        ):
            latest_user = dict(message)
            break

    if latest_user is None:
        latest_user = {
            "role": "user",
            "content": (
                f"请按照 Schema 调用工具 {forced_name}。"
            ),
        }

    return [
        {
            # 与首次请求保持相同的最高优先级，避免重试时
            # 从 system 降为 developer 后降低协议约束力。
            "role": "system",
            "content": strict_prompt + "\n\n" + correction,
        },
        latest_user,
    ]





def _has_bridged_tool_result_message(
    messages: list[dict[str, Any]],
) -> bool:
    """
    判断当前 Web 消息上下文中是否已经存在 Hermes 的真实工具结果。

    _bridge_openai_tool_history 会把 role=tool 转成带有固定前缀的
    user 消息。只在这个边界之后启用 auto 污染恢复，避免影响
    第一轮普通聊天和正常的安全停止。
    """
    prefix = (
        "以下是 Hermes 已实际执行的工具结果，"
        "请根据它继续完成原任务。"
    )

    for message in messages:
        if not isinstance(message, dict):
            continue

        if message.get("role") != "user":
            continue

        content = message.get("content")

        if (
            isinstance(content, str)
            and content.startswith(prefix)
        ):
            return True

    return False


def _looks_like_tool_syntax_pollution(
    text: str,
) -> bool:
    """
    识别窄范围的工具语法污染。

    这里只识别明显的协议包络碎片、其他工具 JSON 前缀，
    以及以 Shell 调用形式开头的响应。普通自然语言、真实结果
    总结和正常最终回答不在此范围内。
    """
    stripped = str(text or "").strip()

    if not stripped:
        return False

    lowered = stripped.lower()
    head = lowered[:320]

    # 严格包络被前缀或其他字符污染。
    if (
        JSON_TOOL_OPEN_TAG in stripped
        or JSON_TOOL_CLOSE_TAG in stripped
    ):
        return True

    incompatible_prefixes = (
        '{"paths":',
        '{"path":',
        '{"command":',
        '{"cmd":',
        "bash -lc ",
        "sh -lc ",
        "terminal(",
        "search(",
        "search_files",
    )

    if any(
        lowered.startswith(prefix)
        for prefix in incompatible_prefixes
    ):
        return True

    # 兼容观察到的混合污染：
    # {"paths":[...]}bash -lc ...
    has_foreign_json_key = (
        '"paths"' in head
        or '"path"' in head
        or '"command"' in head
        or '"cmd"' in head
    )

    has_shell_invocation = (
        "bash -lc" in head
        or "sh -lc" in head
    )

    if has_foreign_json_key and has_shell_invocation:
        return True

    # Shell 文本与“已经执行”的声明同时出现，也视为污染。
    execution_claims = (
        "真实执行结果",
        "执行结果如下",
        "已经执行",
        "已执行",
        "exit_code",
        "stdout",
        "stderr",
    )

    if (
        has_shell_invocation
        and any(
            claim in stripped
            for claim in execution_claims
        )
    ):
        return True

    return False


def _latest_user_text(
    messages: list[dict[str, Any]],
) -> str:
    """
    返回当前请求中最新一条纯文本 user 消息。

    普通聊天污染检测只比较最新用户消息，不读取 system、
    developer 或 assistant 内容。
    """
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue

        if message.get("role") != "user":
            continue

        content = message.get("content")

        if isinstance(content, str):
            return content.strip()

    return ""


def _looks_like_ordinary_search_echo_pollution(
    text: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
) -> bool:
    """
    识别尚未出现真实工具结果时的窄范围 search 回显污染。

    Hermes 的普通聊天请求也可能携带默认 Function Tools，因此
    不能以 tools 是否为空作为普通回答的判断条件。

    仅匹配如下结构：

        search("<最新用户消息的 JSON 字符串>")<普通回答>

    search 参数必须在 JSON 解码后精确等于最新用户消息，
    且 search(...) 后必须还存在非空回答。

    强制工具模式以及已经存在真实工具结果的后续轮次不在此
    分类中，继续由原强制工具或 post-tool 污染逻辑处理。
    """
    if _forced_function_name(tool_choice):
        return False

    if _has_bridged_tool_result_message(messages):
        return False

    stripped = str(text or "").strip()
    prefix = "search("

    if not stripped.lower().startswith(prefix):
        return False

    payload_text = stripped[len(prefix):].lstrip()

    try:
        argument, end = json.JSONDecoder().raw_decode(
            payload_text
        )
    except (
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return False

    if not isinstance(argument, str):
        return False

    tail = payload_text[end:].lstrip()

    if not tail.startswith(")"):
        return False

    remainder = tail[1:].strip()

    if not remainder:
        return False

    latest_user = _latest_user_text(messages)

    if not latest_user:
        return False

    return argument.strip() == latest_user



def _ordinary_pollution_retry_messages(
    messages: list[dict[str, Any]],
    next_attempt: int,
) -> list[dict[str, Any]]:
    """
    为普通聊天 search 回显污染构造全新尝试。

    保留原始会话历史，只在最新 user 消息之前插入一条局部
    developer 纠正指令；上一模型污染输出不会进入新上下文。
    """
    correction = {
        "role": "developer",
        "content": (
            f"这是普通聊天污染恢复的第 {next_attempt} 次全新尝试。\n"
            "上一模型输出已经被完全丢弃，不得引用、续写、"
            "修补或解释上一输出。\n"
            "请直接回答最新用户消息。\n"
            "不得把用户消息放入 search(...)、search_files、"
            "terminal(...)、bash -lc、sh -lc、JSON 对象、"
            "<tool_call> 标签或任何其他工具调用语法中。\n"
            "本次不得声称已经执行任何工具或搜索。\n"
            "若用户要求只输出一个精确字符串，只输出该字符串，"
            "不得增加前缀、后缀、解释或工具语法。\n"
            "若任务无法在无工具情况下完成，应明确说明无法执行，"
            "不得伪造工具调用或结果。"
        ),
    }

    retry_messages = [
        dict(message)
        for message in messages
        if isinstance(message, dict)
    ]

    insert_at = len(retry_messages)

    for index in range(
        len(retry_messages) - 1,
        -1,
        -1,
    ):
        if retry_messages[index].get("role") == "user":
            insert_at = index
            break

    retry_messages.insert(
        insert_at,
        correction,
    )

    return retry_messages



def _looks_like_false_tool_unavailable_claim(
    text: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
) -> bool:
    """
    识别极窄的 post-tool 虚假“工具不可用”直接声明。

    只处理已经观测到的 M6-08 CASE_B 结构：

    - 已存在真实 bridged tool result；
    - 当前仍实际提供 function tools；
    - tool_choice 没有明确禁止工具；
    - 原解析结果已经是 not_tool_call；
    - assistant 以“当前环境”为主语直接断言一个实际存在的
      工具不存在/不可用；
    - 同一声明中还包含因此无法/不能继续执行的后果。

    不匹配：
    - 否定这种说法；
    - 条件句；
    - 用户引用；
    - 对工具可用性的讨论；
    - 普通 post-tool 最终回答。
    """

    if _forced_function_name(tool_choice):
        return False

    if (
        isinstance(tool_choice, str)
        and tool_choice.strip().lower() == "none"
    ):
        return False

    available_tools = normalized_function_tools(tools)

    if not available_tools:
        return False

    if not _has_bridged_tool_result_message(messages):
        return False

    normalized_text = str(text or "").strip().lower()

    if not normalized_text:
        return False

    # 只移除 Markdown 对工具名的展示反引号。
    # 不移除引号，因为引号本身是“引用而非 assistant 自己断言”
    # 的一个安全边界。
    normalized_text = normalized_text.replace(
        "`",
        "",
    )

    # 按真正的句子/行边界拆分。
    # 故意不按逗号拆分，因为真实 CASE_B 的“因此无法继续”
    # 通常与错误声明处于同一个句子。
    for separator in (
        "\r",
        "\n",
        "。",
        "！",
        "？",
        "；",
        ";",
        "!",
        "?",
    ):
        normalized_text = normalized_text.replace(
            separator,
            "\n",
        )

    clauses = [
        "".join(part.split())
        for part in normalized_text.split("\n")
        if part.strip()
    ]

    consequence_markers = (
        "因此",
        "无法",
        "不能",
    )

    for tool in available_tools:
        name = str(
            tool.get("name") or ""
        ).strip().lower()

        if not name:
            continue

        compact_name = "".join(
            name.split()
        )

        # 只保留已真实观察过的主语 + 不可用声明骨架。
        claim_prefixes = (
            (
                "当前对话环境"
                f"未提供可调用的{compact_name}工具"
            ),
            (
                "当前对话环境"
                f"没有可用的{compact_name}工具"
            ),
            (
                "当前对话环境中"
                f"没有可调用的{compact_name}工具"
            ),
            (
                "我当前会话中"
                f"未提供可调用的{compact_name}工具"
            ),
            (
                "当前对话中"
                f"没有可用的{compact_name}工具"
            ),
        )

        for clause in clauses:
            if not any(
                clause.startswith(prefix)
                for prefix in claim_prefixes
            ):
                continue

            if not any(
                marker in clause
                for marker in consequence_markers
            ):
                continue

            return True

    return False

def _classify_tool_output_error(
    text: str,
    tool_error: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
) -> str:
    """
    在严格解析之后进行窄范围二次分类：

    1. 尚未出现真实工具结果时的 search 回显污染；
    2. 已有真实工具结果后的空响应；
    3. 已有真实工具结果后的工具语法污染。
    """
    if tool_error != "not_tool_call":
        return tool_error

    if _looks_like_ordinary_search_echo_pollution(
        text,
        messages,
        tools,
        tool_choice,
    ):
        return "ordinary_search_echo_pollution"

    if (
        isinstance(tool_choice, str)
        and tool_choice.strip().lower() == "none"
    ):
        return tool_error

    if not normalized_function_tools(tools):
        return tool_error

    if not _has_bridged_tool_result_message(messages):
        return tool_error

    if not str(text or "").strip():
        return "tool_empty_response"

    if _looks_like_tool_syntax_pollution(text):
        return "tool_syntax_pollution"

    if _looks_like_false_tool_unavailable_claim(
        text,
        messages,
        tools,
        tool_choice,
    ):
        return "false_tool_unavailable_claim"

    return tool_error




def _should_retry_tool_output(
    tool_choice: str | dict[str, Any] | None,
    tool_error: str,
    attempt: int,
) -> bool:
    """
    分离两类模型级有限重试：

    - 强制工具模式最多五次；
    - 普通污染、post-tool 污染和空响应最多三次。
    """
    if _should_retry_forced_tool_output(
        tool_choice,
        tool_error,
        attempt,
    ):
        return True

    if attempt >= AUTO_TOOL_RECOVERY_MAX_ATTEMPTS:
        return False

    return tool_error in {
        "tool_syntax_pollution",
        "ordinary_search_echo_pollution",
        "tool_empty_response",
        "false_tool_unavailable_claim",
    }




def _auto_tool_pollution_retry_messages(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
    next_attempt: int,
) -> list[dict[str, Any]]:
    """
    构造 auto 模式的最小化污染恢复请求。

    与强制工具重试不同，这里不强迫调用某一个工具：
    - 原任务仍有未执行步骤时，必须输出严格工具包络；
    - 原任务已经完成时，允许正常回答；
    - 任何情况下都不得编造未执行结果。
    """
    strict_prompt = build_json_tool_bridge_prompt(
        tools,
        tool_choice,
    )

    correction = (
        f"这是 JSON Tool Bridge 污染恢复的第 "
        f"{next_attempt} 次全新尝试。\n"
        "上一模型输出已经被完全丢弃，不得引用、续写、"
        "修补或解释上一输出。\n"
        "请重新阅读原始用户请求以及已经提供的真实工具结果。\n"
        "若原任务仍需要外部客户端执行工具步骤，必须且只能编码一个"
        "严格的 <tool_call>...</tool_call> 调用请求；"
        "不得判断你本人是否拥有工具，也不得用 Shell 命令文本、其他工具语法或自然语言代替。\n"
        "若原任务已经完成，可以直接正常回答，但只能引用上下文"
        "中已经由 Hermes 提供的真实工具结果。\n"
        "绝不得声称未通过真实工具结果提供的命令已经执行，"
        "也不得编造 stdout、stderr、exit_code 或任何执行结果。\n"
        '禁止输出 {"paths":...}、{"command":...}、'
        "bash -lc、sh -lc、search(...)、search_files、"
        "裸 Shell 命令或伪造结果。"
    )

    user_messages = [
        dict(message)
        for message in messages
        if (
            isinstance(message, dict)
            and message.get("role") == "user"
        )
    ]

    if not user_messages:
        user_messages = [{
            "role": "user",
            "content": (
                "请根据原任务和已经提供的真实工具结果继续。"
            ),
        }]

    return [
        {
            "role": "developer",
            "content": (
                strict_prompt
                + "\n\n"
                + correction
            ),
        },
        *user_messages,
    ]


def _tool_retry_messages(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
    tool_error: str,
    next_attempt: int,
) -> list[dict[str, Any]]:
    if _forced_function_name(tool_choice):
        return _forced_tool_retry_messages(
            messages,
            tools,
            tool_choice,
            next_attempt,
        )

    if tool_error == "ordinary_search_echo_pollution":
        return _ordinary_pollution_retry_messages(
            messages,
            next_attempt,
        )

    if tool_error in {
        "tool_syntax_pollution",
        "tool_empty_response",
        "false_tool_unavailable_claim",
    }:
        return _auto_tool_pollution_retry_messages(
            messages,
            tools,
            tool_choice,
            next_attempt,
        )

    return list(messages)




def parse_strict_json_tool_call(
    text: str,
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None = None,
) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    """
    只解析一个严格的 <tool_call>JSON</tool_call> 包络。

    返回：
        (调用列表, 错误代码)

    普通回答：
        ([], "not_tool_call")
    """
    stripped = str(text or "").strip()
    forced_name = _forced_function_name(tool_choice)

    if not stripped.startswith(JSON_TOOL_OPEN_TAG):
        if forced_name:
            return [], "forced_tool_call_required"
        return [], "not_tool_call"

    if (
        stripped.count(JSON_TOOL_OPEN_TAG) != 1
        or stripped.count(JSON_TOOL_CLOSE_TAG) != 1
        or not stripped.endswith(JSON_TOOL_CLOSE_TAG)
    ):
        return [], "invalid_envelope"

    json_text = stripped[
        len(JSON_TOOL_OPEN_TAG):
        -len(JSON_TOOL_CLOSE_TAG)
    ].strip()

    if not json_text:
        return [], "empty_payload"

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        return [], "invalid_json"

    if not isinstance(payload, dict):
        return [], "payload_not_object"

    if set(payload.keys()) != {"name", "arguments"}:
        return [], "invalid_payload_fields"

    name = payload.get("name")
    arguments = payload.get("arguments")

    if not isinstance(name, str) or not name.strip():
        return [], "invalid_tool_name"

    name = name.strip()

    if not isinstance(arguments, dict):
        return [], "arguments_not_object"

    allowed = {
        item["name"]: item
        for item in normalized_function_tools(tools)
    }

    if name not in allowed:
        return [], "unknown_tool"

    if tool_choice == "none":
        return [], "tool_choice_none"

    if forced_name and name != forced_name:
        return [], "tool_choice_mismatch"

    parameters = allowed[name].get("parameters")
    if not isinstance(parameters, dict):
        return [], "invalid_tool_schema"

    errors = validate_tool_arguments(arguments, parameters)
    if errors:
        return [], "schema_error:" + " | ".join(errors)

    return [(name, arguments)], ""
