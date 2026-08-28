"""Tool-use emulation for a chat-only upstream (Duck.ai has no function-calling API).

Claude Code / Codex / PI send a `tools` array and expect `tool_use` (Anthropic) or
`tool_calls` (OpenAI) back so they can execute the call and return `tool_result`.
Because Duck.ai is chat-only, we emulate this with prompt injection:

  1. Render the tool schemas into the system prompt with a STRICT call envelope.
  2. The model emits `<tool_call name="...">{json}</tool_call>` when it wants to call.
  3. We parse that envelope and return a proper tool_use / tool_calls block.
  4. The agent's tool_result blocks are flattened back into the next prompt as text.

This mirrors what gateways do when the upstream lacks native tools. It is not as
reliable as real function calling, but it is the correct way to give a chat-only
backend agent-compatible tool capability.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional

_TOOL_CALL_RE = re.compile(
    r"<tool_call\s+name=\"([^\"]+)\"\s*>(.*?)</tool_call>", re.DOTALL
)


@dataclass
class ToolCall:
    name: str
    input: dict


def render_tools_prompt(tools: List[dict]) -> str:
    """Build a system-prompt suffix that teaches the model the tool-call protocol."""
    if not tools:
        return ""
    lines = [
        "",
        "## TOOL USE",
        "You have access to tools. To call one, emit EXACTLY ONE XML envelope on its own,",
        "with the arguments as a JSON object (no markdown fences):",
        "",
        '<tool_call name="tool_name">{ "arg": "value" }</tool_call>',
        "",
        "Rules:",
        "- Emit the envelope only when you need a tool; otherwise reply in plain text.",
        "- Do not wrap the JSON in ``` or explain the call. Just the envelope.",
        "- Use valid JSON matching the schema. Available tools:",
        "",
    ]
    for t in tools:
        name = t.get("name", "tool")
        desc = (t.get("description") or "").strip()
        schema = t.get("input_schema") or t.get("parameters") or {}
        lines.append(f"### {name}")
        if desc:
            lines.append(desc)
        lines.append("input_schema: " + json.dumps(schema, ensure_ascii=False))
        lines.append("")
    return "\n".join(lines)


def parse_tool_call(text: str) -> Optional[ToolCall]:
    """Extract the first <tool_call> envelope from model output.

    Returns None if the model replied in plain text (no tool call).
    """
    m = _TOOL_CALL_RE.search(text)
    if not m:
        return None
    name = m.group(1)
    raw = m.group(2).strip()
    # strip accidental markdown fences
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        inp = json.loads(raw)
    except json.JSONDecodeError:
        # best-effort: if not JSON, pass as a single "input" string
        inp = {"input": raw}
    if not isinstance(inp, dict):
        inp = {"input": raw}
    return ToolCall(name=name, input=inp)


def split_text_and_tool(text: str):
    """Split model output into (preamble_text, tool_call).

    preamble_text is any natural-language text before the tool envelope (may be "").
    """
    m = _TOOL_CALL_RE.search(text)
    if not m:
        return text.strip(), None
    preamble = text[: m.start()].strip()
    tc = parse_tool_call(text)
    return preamble, tc
