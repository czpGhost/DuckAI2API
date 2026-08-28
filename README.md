# DuckAI2API

> 🇬🇧 English | [🇨🇳 中文](./README.zh.md)

OpenAI / Anthropic / Responses compatible API server backed by **Duck.ai** (`https://duck.ai/`) — including **GPT-5.6 Luna** reasoning. Built from a js-reverse analysis of Duck.ai's live web protocol (XHR + SSE, no official API), and re-verified after every major site change.

The server speaks three protocols at once, so it works as a drop-in backend for **Claude Code**, the **OpenAI SDK**, and the **OpenAI Responses API**. All chat is streamed from the real Duck.ai SSE feed.

> Works on **Windows, macOS, and Linux** — see [Requirements](#requirements).

## Why this exists

Duck.ai has no public API. Its chat runs over:

- `POST https://duck.ai/duckchat/api/status` — fetch `server_vqd_02` (X-Vqd-4 token)
- `POST https://duck.ai/duckchat/api/refs` — fetch `vqd` (X-Vqd-4 token for chat)
- `POST https://duck.ai/duckchat/api/requests/chat` with `Accept: text/event-stream` — SSE response

The model id is sent as `model: "<model>"` in the JSON body. The relay maps friendly model names to these ids.

## Features

- **GPT-5.6 Luna** plus 8 more free models.
- **Three protocols**: `/v1/chat/completions` (OpenAI), `/v1/messages` (Anthropic), `/v1/responses` (OpenAI Responses).
- **Real streaming** on every protocol, sourced from Duck.ai's SSE.
- **Agent tool-loop support**: Claude Code and compatible agents send `tools`; the relay synthesizes the `tool_use` / `tool_calls` / `function_call` block from the user's intent (Duck.ai cannot emit tool calls), the agent executes locally, returns the result back, and the relay feeds it to Duck.ai as grounded context. **No command is ever executed server-side.**
- **Session rotation**: per-model headless-Chrome session, auto-recreated on rate-limit / crash.
- **Optional API-key wall**: set `DUCKAI_API_KEY` and present `Authorization: Bearer <key>`.

## Models

| id                  | label                     |
|---------------------|---------------------------|
| `gpt-5.6-luna`      | GPT-5.6 Luna               |
| `gpt-5.4`           | GPT-5.4                   |
| `gpt-5.4-mini`      | GPT-5.4 mini              |
| `claude-sonnet-4-6` | Claude Sonnet 4.6         |
| `claude-haiku-4-5`  | Claude Haiku 4.5          |
| `claude-opus-4-8`   | Claude Opus 4.8           |
| `mistral-small-2603`| Mistral Small 4           |
| `tinfoil/gpt-oss-120b` | gpt-oss 120B          |
| `tinfoil/gemma4-31b`   | Gemma 4 31B           |

Unknown model ids are passed through verbatim (so future Duck.ai models work without a code change). Common aliases (`gpt-5.6`, `claude-haiku`, `o3-mini`, …) are resolved to the table above.

## Requirements

- **Python** ≥ 3.10
- **Google Chrome (stable)** installed on the host. The relay drives Duck.ai through a real Chrome instance (`Playwright` `channel="chrome"`); the bundled Playwright Chromium is fingerprint-banned by Duck.ai, so system Chrome is required. You do **not** need `playwright install chromium`.
  - Windows: install from <https://www.google.com/chrome/>
  - macOS: `brew install --cask google-chrome` (or download from google.com/chrome)
  - Linux (Debian/Ubuntu): `sudo apt-get install google-chrome-stable`
- If Chrome is at a non-default location, point `DUCKAI_CHROME_PATH` at the binary.

## Install & run

```bash
# 1. venv
python3 -m venv .venv
#    Windows:        .venv\Scripts\activate
#    macOS / Linux:  source .venv/bin/activate

# 2. deps
pip install -r requirements.txt

# 3. (optional) config
cp .env.example .env          # edit if you want an API key, a proxy, etc.

# 4. serve
python -m uvicorn main:app --host 0.0.0.0 --port 8080
```

The server listens on `http://localhost:8080` (override with `PORT`).

## Configuration

All settings are optional and read from environment (`.env` via `python-dotenv`).

| Variable                | Meaning                                                                                       | Default              |
|-------------------------|-----------------------------------------------------------------------------------------------|----------------------|
| `DUCKAI_API_KEY`        | Bearer token required on all `/v1` requests. **Empty = open access.**                         | *(empty)*            |
| `DUCKAI_BASE`           | Duck.ai host.                                                                                 | `https://duck.ai`    |
| `DUCKAI_MODEL`          | Default model when the client omits one.                                                      | `gpt-5.6-luna`       |
| `DUCKAI_NEW_CHAT`       | `1` = fresh chat per request (stateless, OpenAI/Anthropic-like); `0` = keep session history.  | `0`                  |
| `DUCKAI_PROXIES`        | Comma-separated proxy pool; the relay rotates past per-IP `ERR_BN_LIMIT` bans.                | *(none)*             |
| `DUCKAI_PROXY`          | Single proxy (alternative to the pool).                                                       | *(none)*             |
| `DUCKAI_MAX_CONCURRENCY` | Max in-flight requests per proxy before queuing.                                            | `2`                  |
| `DUCKAI_CHROME_PATH`    | Override the path to the Google Chrome binary (auto-detected per OS otherwise).               | per-OS default       |
| `PORT`                  | Server port.                                                                                  | `8080`               |

## Usage

### OpenAI SDK (streaming)

```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8080/v1", api_key="t")  # any key if wall off
stream = c.chat.completions.create(
    model="gpt-5.6-luna",
    messages=[{"role": "user", "content": "Explain quantum tunneling in one sentence."}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

### Anthropic SDK (Claude Code)

```python
from anthropic import Anthropic
c = Anthropic(base_url="http://localhost:8080", api_key="t")
c.messages.create(
    model="gpt-5.6-luna",
    max_tokens=1024,
    messages=[{"role": "user", "content": "What is 2+2?"}],
)
```

Point Claude Code at the relay with `ANTHROPIC_BASE_URL=http://localhost:8080` and `ANTHROPIC_API_KEY=t`.

### Tool loop (Claude Code compatible)

Send tools as you would to Claude. The relay emits a `tool_use` block from the user's
intent; your agent executes it and returns the `tool_result`, which the relay then
forwards to Duck.ai for a grounded final answer.

```python
tools=[{"name":"Read","description":"Read a file",
        "input_schema":{"type":"object","properties":{"file_path":{"type":"string"}}}}]
# user: "Read the file README.md"  ->  relay returns tool_use: Read {file_path:"README.md"}
# agent runs Read locally, returns tool_result; relay answers using that content.
```

## Files

- `duckai.py` — low-level Duck.ai client (token fetch + SSE chat, Chrome session mgmt, rotation). Exposes `MODEL_LABELS`, `resolve_model`, `DuckAISession`.
- `main.py` — FastAPI app: the three protocol endpoints, request schemas, model routing, conversation flattening, and tool-loop wiring.
- `tools.py` — tool-schema parsing + prompt rendering (transparency / future native support).
- `toolrouter.py` — relay-side intent → `tool_use` synthesis.

All chat/Anthropic/Responses logic lives in `main.py`; the Duck.ai transport lives in `duckai.py`.

## Disclaimer

Not affiliated with DuckDuckGo. For educational/personal use only; respect Duck.ai's terms of service. Use at your own rate-limit risk.
