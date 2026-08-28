"""DuckAI2API - OpenAI + Anthropic compatible relay for Duck.ai chat.

Duck.ai gates its chat backend behind a behavioral anti-bot challenge that cannot
be replayed server-side. Exposes BOTH protocols on the same /v1 prefix:
  - POST /v1/chat/completions   (OpenAI)
  - POST /v1/messages           (Anthropic)

Env:
  DUCKAI_API_KEY   bearer token required on requests (empty = open)
  DUCKAI_BASE      duck.ai host (default https://duck.ai)
  DUCKAI_PROXY     optional proxy for the browser (http/https/socks5)
  DUCKAI_MODEL     default model (default gpt-4o-mini)
  DUCKAI_NEW_CHAT  "1" to start a fresh chat per request (default: reuse session)
  PORT             server port (default 8080)
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, List, Optional, Union
from uuid import uuid4

from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from dotenv import load_dotenv
from pydantic import BaseModel
from duckai import (
    DEFAULT_MODEL,
    DuckAIError,
    DuckAIRateLimit,
    DuckAISession,
    MODEL_LABELS,
    resolve_model,
)
from tools import parse_tool_call, render_tools_prompt, split_text_and_tool
from toolrouter import has_tool_result, route_intent

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("duckai2api")

API_KEY = os.getenv("DUCKAI_API_KEY", "").strip()
# Single proxy (DUCKAI_PROXY) or a comma-separated pool (DUCKAI_PROXIES).
# A pool lets the relay rotate past Duck.ai's per-IP ERR_BN_LIMIT bans.
_proxy_pool = os.getenv("DUCKAI_PROXIES", "").strip() or os.getenv("DUCKAI_PROXY", "").strip()
PROXIES = [p.strip() for p in _proxy_pool.split(",") if p.strip()] or None
DEFAULT = os.getenv("DUCKAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
NEW_CHAT = os.getenv("DUCKAI_NEW_CHAT", "0").strip() == "1"


def _id() -> str:
    return f"chatcmpl-{uuid4().hex[:24]}"


def _created() -> int:
    return int(time.time())


# resolve_model is imported from duckai (single source of truth for model mapping).


# ---------------------------------------------------------------------------
# Anthropic message helpers
# ---------------------------------------------------------------------------
def _anthropic_block_text(content: Any) -> str:
    """Flatten an Anthropic content field (str or list of blocks) to text.

    Image/tool blocks are skipped - Duck.ai only consumes text.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "image":
                    # cannot be forwarded to duck.ai; ignore
                    continue
                elif btype == "tool_result":
                    inner = block.get("content", "")
                    if isinstance(inner, str):
                        parts.append(f"[tool_result {block.get('tool_use_id','')}]: {inner}")
                    elif isinstance(inner, list):
                        parts.append(f"[tool_result {block.get('tool_use_id','')}]: {_anthropic_block_text(inner)}")
                elif btype == "tool_use":
                    # replay the model's prior call so context stays consistent
                    parts.append(
                        f'[tool_call name="{block.get("name","")}"] '
                        f"{json.dumps(block.get('input',{}), ensure_ascii=False)}"
                    )
                elif btype == "input_json_delta" and "partial_json" in block:
                    parts.append(block.get("partial_json", ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def _role_label(role: str) -> str:
    return {"user": "Human", "assistant": "Assistant", "system": "System"}.get(role, "Human")


def flatten_conversation(system: Any, messages: List[dict]) -> str:
    """Flatten system + EVERY turn into one duck.ai prompt string.

    Duck.ai is single-turn (one textarea send), so multi-turn context must be
    re-sent in full on every request. Claude Code ships the entire messages[]
    each turn; we preserve all of it (role-labelled) so the model stays coherent.
    """
    parts: List[str] = []
    if system:
        sys_text = _anthropic_block_text(system)
        if sys_text:
            parts.append(sys_text)
    for m in messages:
        role = m.get("role", "user")
        text = _anthropic_block_text(m.get("content", ""))
        if role == "tool":
            # OpenAI tool-result message: feed the tool output as context
            tool_id = m.get("tool_call_id", "")
            label = f"[tool_result {tool_id}]"
        else:
            label = _role_label(role)
        if text.strip():
            parts.append(f"{label}: {text}")
    return "\n\n".join(parts).strip()


def _to_anthropic_tool(openai_tool: dict) -> dict:
    """Convert an OpenAI tool spec ({type:function, function:{...}}) to Anthropic shape."""
    if openai_tool.get("type") == "function" and "function" in openai_tool:
        fn = openai_tool["function"]
        return {
            "name": fn.get("name", "tool"),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {}),
        }
    return openai_tool
def build_anthropic_prompt(system: Any, messages: List[dict]) -> str:
    """Combine system + full conversation into a single duck.ai prompt string."""
    return flatten_conversation(system, messages)


# ---------------------------------------------------------------------------
# App + session
# ---------------------------------------------------------------------------
app = FastAPI(title="DuckAI2API")

_sessions: dict = {}


async def get_session(model: str) -> DuckAISession:
    global _sessions
    if model not in _sessions:
        _sessions[model] = DuckAISession(model=model, proxies=PROXIES)
    return _sessions[model]


def require_key(authorization: str = Header(default="")) -> None:
    if not API_KEY:
        return
    if not authorization.startswith("Bearer ") or authorization.split(" ", 1)[1] != API_KEY:
        raise HTTPException(status_code=401, detail="invalid API key")


# ---------------------------------------------------------------------------
# OpenAI protocol
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Any]]


class ChatCompletionRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    tools: Optional[List[dict]] = None

async def _openai_stream(session: DuckAISession, prompt: str, model: str):
    def end_event():
        return (
            f'data: {json.dumps({"id": _id(), "object": "chat.completion.chunk", "created": _created(), "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})}\n\n'
        )

    try:
        async for token in session.send_stream(prompt):
            chunk = {
                "id": _id(),
                "object": "chat.completion.chunk",
                "created": _created(),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
        yield end_event()
        yield "data: [DONE]\n\n"
    except (DuckAIRateLimit, DuckAIError) as e:
        err = {"error": {"message": str(e), "type": "server_error"}}
        yield f"data: {json.dumps(err)}\n\n"
    except Exception as e:  # noqa: BLE001 - never let the SSE connection die uncleanly
        err = {"error": {"message": f"stream failed: {e}", "type": "server_error"}}
        yield f"data: {json.dumps(err)}\n\n"

@app.post("/v1/chat/completions", dependencies=[Depends(require_key)])
async def chat_completions(request: ChatCompletionRequest):
    system = None
    turns = []
    for m in request.messages:
        if m.role == "system":
            system = m.content
        else:
            turns.append({"role": m.role, "content": m.content})
    prompt = flatten_conversation(system, turns)
    if not prompt.strip() and not request.tools:
        raise HTTPException(status_code=400, detail="empty prompt")
    model = resolve_model(request.model)

    # Relay-side tool routing: synthesise a tool_calls block from the user's
    # intent (Duck.ai cannot emit one). Skip when a tool_result is already present.
    if request.tools and not has_tool_result(turns):
        routed = route_intent(turns, request.tools)
        if routed is not None:
            return {
                "id": _id(),
                "object": "chat.completion",
                "created": _created(),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": f"call_{uuid4().hex[:24]}",
                            "type": "function",
                            "function": {
                                "name": routed.name,
                                "arguments": json.dumps(routed.input, ensure_ascii=False),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

    session = await get_session(model)

    if request.stream:
        return StreamingResponse(_openai_stream(session, prompt, model), media_type="text/event-stream")

    try:
        result = await session.send(prompt)
    except DuckAIRateLimit as e:
        raise HTTPException(status_code=429, detail=str(e))
    except DuckAIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    msg = {"role": "assistant", "content": result.strip()}
    finish = "stop"
    preamble, tc = split_text_and_tool(result)
    if tc is not None:
        msg["content"] = preamble or None
        msg["tool_calls"] = [{
            "id": f"call_{uuid4().hex[:24]}",
            "type": "function",
            "function": {"name": tc.name, "arguments": json.dumps(tc.input, ensure_ascii=False)},
        }]
        finish = "tool_calls"
    return {
        "id": _id(),
        "object": "chat.completion",
        "created": _created(),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# OpenAI Responses API (/v1/responses)
# ---------------------------------------------------------------------------
    """Flatten a Responses API `input` into a full multi-turn duck.ai prompt.

    `input` may be a string, or a list of items:
      - {"role": "user"|"system"|"assistant", "content": str | [blocks]}
      - {"type": "message", "role": ..., "content": str | [blocks]}
    Every turn is preserved (role-labelled) so multi-turn stays coherent.
    """
    if inp is None:
        return ""
    if isinstance(inp, str):
        return inp.strip()
    if isinstance(inp, list):
        parts: List[str] = []
        for item in inp:
            if not isinstance(item, dict):
                continue
            role = item.get("role") or "user"
            content = item.get("content", "")
            text = _anthropic_block_text(content) if not isinstance(content, str) else content
            if role == "tool":
                label = f"[tool_result {item.get('tool_call_id', '')}]"
            else:
                label = _role_label(role)
            if text.strip():
                parts.append(f"{label}: {text}")
        return "\n\n".join(parts).strip()
    return str(inp).strip()

@app.post("/v1/responses", dependencies=[Depends(require_key)])
async def openai_responses(request: Request):
    raw = await request.json()
    model = resolve_model(raw.get("model"))
    tools = raw.get("tools") or []
    prompt = _responses_input_text(raw.get("input"))
    # Relay-side tool routing: synthesise a function_call from the user's intent.
    if tools:
        resp_items = raw.get("input") or []
        route_msgs = []
        if isinstance(resp_items, list):
            for it in resp_items:
                if isinstance(it, dict):
                    role = it.get("role") or ("user" if it.get("type") == "message" else "user")
                    content = it.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                        )
                    route_msgs.append({"role": role, "content": content})
        if not has_tool_result(route_msgs):
            routed = route_intent(route_msgs, tools)
            if routed is not None:
                return {
                    "id": f"resp_{uuid4().hex[:24]}",
                    "object": "response",
                    "created_at": _created(),
                    "model": model,
                    "status": "completed",
                    "output": [{
                        "type": "function_call",
                        "id": f"fc_{uuid4().hex[:24]}",
                        "name": routed.name,
                        "arguments": json.dumps(routed.input, ensure_ascii=False),
                    }],
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                }
    if not prompt:
        raise HTTPException(status_code=400, detail="empty input")
    stream = bool(raw.get("stream", False))
    session = await get_session(model)
    resp_id = f"resp_{uuid4().hex[:24]}"

    if not stream:
        try:
            result = await session.send(prompt)
        except DuckAIRateLimit as e:
            raise HTTPException(status_code=429, detail=str(e))
        except DuckAIError as e:
            raise HTTPException(status_code=502, detail=str(e))
        preamble, tc = split_text_and_tool(result)
        output = []
        if preamble:
            output.append({
                "type": "message",
                "id": f"msg_{uuid4().hex[:24]}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": preamble}],
            })
        if tc is not None:
            output.append({
                "type": "function_call",
                "id": f"fc_{uuid4().hex[:24]}",
                "name": tc.name,
                "arguments": json.dumps(tc.input, ensure_ascii=False),
            })
        if not output:
            output.append({
                "type": "message",
                "id": f"msg_{uuid4().hex[:24]}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": result.strip()}],
            })
        return {
            "id": resp_id,
            "object": "response",
            "created_at": _created(),
            "model": model,
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }

    async def _resp_stream():
        yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'status': 'in_progress', 'model': model, 'output': []}})}\n\n"
        try:
            async for token in session.send_stream(prompt):
                yield f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': token})}\n\n"
        except (DuckAIRateLimit, DuckAIError) as e:
            yield f"data: {json.dumps({'type': 'error', 'error': {'message': str(e), 'type': 'server_error'}})}\n\n"
            return
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'error': {'message': f'stream failed: {e}', 'type': 'server_error'}})}\n\n"
            return
        yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'status': 'completed', 'model': model, 'output': []}})}\n\n"

    return StreamingResponse(_resp_stream(), media_type="text/event-stream")


def _build_anthropic_message(msg_id: str, model: str, result: str, tool_use=None) -> dict:
    """Build an Anthropic message response.

    - If `tool_use` (a RoutedToolCall from the relay router) is given, emit a
      tool_use content block and stop_reason="tool_use" WITHOUT calling Duck.ai.
    - Else if the model emitted a <tool_call> envelope (never happens on Duck.ai,
      kept for completeness), parse it.
    - Else return a plain text block.
    """
    if tool_use is not None:
        return {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{
                "type": "tool_use",
                "id": f"toolu_{uuid4().hex[:24]}",
                "name": tool_use.name,
                "input": tool_use.input,
            }],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    preamble, tc = split_text_and_tool(result)
    if tc is None:
        return {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": result.strip()}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    content = []
    if preamble:
        content.append({"type": "text", "text": preamble})
    content.append({
        "type": "tool_use",
        "id": f"toolu_{uuid4().hex[:24]}",
        "name": tc.name,
        "input": tc.input,
    })
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


# ---------------------------------------------------------------------------
# Anthropic protocol
# ---------------------------------------------------------------------------
@app.get("/v1/models", dependencies=[Depends(require_key)])
async def list_models():
    data = [
        {
            "id": mid,
            "object": "model",
            "created": _created(),
            "owned_by": "duck.ai",
            "display_name": label,
        }
        for mid, label in MODEL_LABELS.items()
    ]
    return {"object": "list", "data": data}


@app.post("/v1/messages", dependencies=[Depends(require_key)])
async def anthropic_messages(request: Request):
    raw = await request.json()
    model = resolve_model(raw.get("model"))
    system = raw.get("system")
    tools = raw.get("tools") or []
    messages = raw.get("messages") or []
    prompt = build_anthropic_prompt(system, messages)
    # Relay-side tool routing: Duck.ai cannot emit tool calls, so the relay
    # synthesizes a tool_use block from the user's intent. Skip routing when the
    # client is already returning a tool_result (mid-loop) - then we just answer.
    if tools and not has_tool_result(messages):
        routed = route_intent(messages, tools)
        if routed is not None:
            return _build_anthropic_message(
                f"msg_{uuid4().hex[:24]}", model, "", tool_use=routed
            )
    stream = bool(raw.get("stream", False))
    session = await get_session(model)
    msg_id = f"msg_{uuid4().hex[:24]}"
    if not stream:
        try:
            result = await session.send(prompt)
        except DuckAIRateLimit as e:
            return _anthropic_error(429, "rate_limit_error", str(e))
        except DuckAIError as e:
            return _anthropic_error(502, "api_error", str(e))
        return _build_anthropic_message(msg_id, model, result)

    async def _anthropic_stream():
        yield _sse("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })
        try:
            full = ""
            async for token in session.send_stream(prompt):
                full += token
            preamble, tc = split_text_and_tool(full)
            if tc is None:
                # plain text reply
                yield _sse("content_block_start", {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                })
                if preamble:
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": preamble},
                    })
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                yield _sse("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 0},
                })
            else:
                # tool_use reply
                if preamble:
                    yield _sse("content_block_start", {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    })
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": preamble},
                    })
                    yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                yield _sse("content_block_start", {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "id": f"toolu_{uuid4().hex[:24]}", "name": tc.name},
                })
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(tc.input, ensure_ascii=False)},
                })
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": 1})
                yield _sse("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 0},
                })
        except (DuckAIRateLimit, DuckAIError) as e:
            yield _sse("error", {"type": "error", "error": {"type": "rate_limit_error", "message": str(e)}})
            return
        except Exception as e:  # noqa: BLE001
            yield _sse("error", {"type": "error", "error": {"type": "api_error", "message": f"stream failed: {e}"}})
            return
        yield _sse("message_stop", {"type": "message_stop"})

    return StreamingResponse(_anthropic_stream(), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _anthropic_error(status: int, etype: str, message: str):
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": etype, "message": message}},
    )

@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
