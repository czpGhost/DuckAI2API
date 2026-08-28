
<div align="center">

# 🦆 DuckAI2API

**Turn Duck.ai into an OpenAI / Anthropic / Responses compatible API — for free.**

[![License](https://img.shields.io/github/license/czpGhost/DuckAI2API.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/czpGhost/DuckAI2API.svg)](https://github.com/czpGhost/DuckAI2API/stargazers)
[![Forks](https://img.shields.io/github/forks/czpGhost/DuckAI2API.svg)](https://github.com/czpGhost/DuckAI2API/network/members)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-2EA043.svg)](#requirements)
[![Models](https://img.shields.io/badge/Models-9%20free-FF6B35.svg)](#models)

[🇨🇳 中文](./README.zh.md) · [🇬🇧 English](./README.md)

</div>

---

A relay that speaks **three** LLM protocols at once, backed by **Duck.ai** (`https://duck.ai/`) — no API key, no payment. Built from a js-reverse analysis of Duck.ai's live web protocol (XHR + SSE, no official API), re-verified after every major site change.

Works as a drop-in backend for **Claude Code**, the **OpenAI SDK**, and the **OpenAI Responses API**. All chat is streamed straight from Duck.ai's real SSE feed.

## ✨ Why

Duck.ai has **no public API**. Its chat runs over:

- `POST https://duck.ai/duckchat/api/status` — fetch `server_vqd_02` (X-Vqd-4 token)
- `POST https://duck.ai/duckchat/api/refs` — fetch `vqd` (X-Vqd-4 token for chat)
- `POST https://duck.ai/duckchat/api/requests/chat` with `Accept: text/event-stream` — SSE response

The model id is sent as `model: "<model>"` in the JSON body. The relay maps friendly model names to these ids.

## 🚀 Features

| | Capability |
|---|---|
| 🧠 **9 free models** | GPT-5.6 Luna, GPT-5.4 (+mini), Claude Sonnet/Haiku/Opus 4.x, Mistral Small, gpt-oss 120B, Gemma 4 31B |
| 🔌 **Triple protocol** | `/v1/chat/completions` (OpenAI), `/v1/messages` (Anthropic), `/v1/responses` (OpenAI Responses) |
| 🌊 **Real streaming** | Sourced from Duck.ai's SSE on every protocol |
| 🤖 **Agent tool-loop** | Synthesizes `tool_use` / `tool_calls` / `function_call` from intent; feeds `tool_result` back to Duck.ai. **No command executed server-side** |
| 🔄 **Session rotation** | Per-model headless-Chrome session, auto-recreated on rate-limit / crash |
| 🔐 **Optional key wall** | Set `DUCKAI_API_KEY`; present `Authorization: Bearer <key>` |
| 🪟 **Cross-platform** | Windows · macOS · Linux |

## 📦 Models

| id                  | label                     |
|---------------------|---------------------------|
| `gpt-5.6-luna`      | GPT-5.6 Luna              |
| `gpt-5.4`           | GPT-5.4                   |
| `gpt-5.4-mini`      | GPT-5.4 mini              |
| `claude-sonnet-4-6` | Claude Sonnet 4.6         |
| `claude-haiku-4-5`  | Claude Haiku 4.5          |
| `claude-opus-4-8`   | Claude Opus 4.8           |
| `mistral-small-2603`| Mistral Small 4           |
| `tinfoil/gpt-oss-120b` | gpt-oss 120B          |
| `tinfoil/gemma4-31b`   | Gemma 4 31B           |

Unknown model ids pass through verbatim (future Duck.ai models work without a code change). Common aliases (`gpt-5.6`, `claude-haiku`, `o3-mini`, …) resolve to the table above.

## 🛠️ Requirements

- **Python** ≥ 3.10
- **Google Chrome (stable)** on the host. The relay drives Duck.ai through a real Chrome instance (`Playwright` `channel="chrome"`); the bundled Playwright Chromium is fingerprint-banned by Duck.ai, so system Chrome is required. You do **not** need `playwright install chromium`.
  - Windows: install from <https://www.google.com/chrome/>
  - macOS: `brew install --cask google-chrome`
  - Linux (Debian/Ubuntu): `sudo apt-get install google-chrome-stable`
- If Chrome is elsewhere, point `DUCKAI_CHROME_PATH` at the binary.

## ⚡ Quick start

```bash
# 1. venv
python3 -m venv .venv
#    Windows:        .venv\Scripts\activate
#    macOS / Linux:  source .venv/bin/activate

# 2. deps
pip install -r requirements.txt

# 3. (optional) config
cp .env.example .env          # API key, proxy, etc.

# 4. serve
python -m uvicorn main:app --host 0.0.0.0 --port 8080
```

Server listens on `http://localhost:8080` (override with `PORT`).

## ⚙️ Configuration

All settings are optional and read from environment (`.env` via `python-dotenv`).

| Variable                | Meaning                                                                                       | Default              |
|-------------------------|-----------------------------------------------------------------------------------------------|----------------------|
| `DUCKAI_API_KEY`        | Bearer token required on all `/v1` requests. **Empty = open access.**                         | *(empty)*            |
| `DUCKAI_BASE`           | Duck.ai host.                                                                                 | `https://duck.ai`    |
| `DUCKAI_MODEL`          | Default model when the client omits one.                                                      | `gpt-5.6-luna`       |
| `DUCKAI_NEW_CHAT`       | `1` = fresh chat per request (stateless); `0` = keep session history.                         | `0`                  |
| `DUCKAI_PROXIES`        | Comma-separated proxy pool; rotates past per-IP `ERR_BN_LIMIT` bans.                          | *(none)*             |
| `DUCKAI_PROXY`          | Single proxy (alternative to the pool).                                                       | *(none)*             |
| `DUCKAI_MAX_CONCURRENCY` | Max in-flight requests per proxy before queuing.                                            | `2`                  |
| `DUCKAI_CHROME_PATH`    | Override the path to the Google Chrome binary (auto-detected per OS otherwise).               | per-OS default       |
| `PORT`                  | Server port.                                                                                  | `8080`               |

## 💡 Usage

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

## 📂 Files

- `duckai.py` — low-level Duck.ai client (token fetch + SSE chat, Chrome session mgmt, rotation). Exposes `MODEL_LABELS`, `resolve_model`, `DuckAISession`.
- `main.py` — FastAPI app: the three protocol endpoints, request schemas, model routing, conversation flattening, and tool-loop wiring.
- `tools.py` — tool-schema parsing + prompt rendering (transparency / future native support).
- `toolrouter.py` — relay-side intent → `tool_use` synthesis.

All chat/Anthropic/Responses logic lives in `main.py`; the Duck.ai transport lives in `duckai.py`.


## ⚠️ Disclaimer

Not affiliated with DuckDuckGo. For educational/personal use only; respect Duck.ai's terms of service. Use at your own rate-limit risk.
