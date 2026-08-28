# DuckAI2API

> 🇨🇳 中文 | [🇬🇧 English](./README.md)

将 **Duck.ai**（`https://duck.ai/`）转换为兼容 OpenAI / Anthropic / Responses 的 API 服务——包含 **GPT-5.6 Luna** 推理。基于对 Duck.ai 实时 Web 协议（XHR + SSE，无官方 API）的反向分析构建，并在每次网站大改后重新验证。

服务同时支持三种协议，可作为 **Claude Code**、**OpenAI SDK**、**OpenAI Responses API** 的即插即用后端。所有聊天均来自 Duck.ai 真实的 SSE 流。

> 支持 **Windows、macOS、Linux** —— 见 [环境要求](#环境要求)。

## 为什么做这个

Duck.ai 没有公开 API。它的聊天走以下接口：

- `POST https://duck.ai/duckchat/api/status` — 获取 `server_vqd_02`（X-Vqd-4 令牌）
- `POST https://duck.ai/duckchat/api/refs` — 获取 `vqd`（聊天用的 X-Vqd-4 令牌）
- `POST https://duck.ai/duckchat/api/requests/chat`，请求头 `Accept: text/event-stream` — SSE 响应

模型 id 在 JSON 体中以 `model: "<model>"` 发送。中转服务会把友好的模型名映射到这些 id。

## 功能

- **GPT-5.6 Luna** 外加 8 个免费模型。
- **三种协议**：`/v1/chat/completions`（OpenAI）、`/v1/messages`（Anthropic）、`/v1/responses`（OpenAI Responses）。
- **真实流式**：所有协议均从 Duck.ai 的 SSE 流式输出。
- **Agent 工具循环支持**：Claude Code 及兼容 agent 发送 `tools`；中转服务根据用户意图合成 `tool_use` / `tool_calls` / `function_call` 块（Duck.ai 自身无法发出工具调用），agent 在本地执行并将结果返回，中转服务再把结果作为上下文喂给 Duck.ai 得到有依据的最终回答。**服务端绝不执行任何命令。**
- **会话轮换**：按模型维护无头 Chrome 会话，在限流/崩溃时自动重建。
- **可选 API Key 鉴权**：设置 `DUCKAI_API_KEY`，请求时带上 `Authorization: Bearer <key>`。

## 模型

| id                  | 名称                      |
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

未知的模型 id 会原样透传（因此未来的 Duck.ai 模型无需改代码即可使用）。常用别名（`gpt-5.6`、`claude-haiku`、`o3-mini` 等）会自动解析到上表。

## 环境要求

- **Python** ≥ 3.10
- 主机上安装 **Google Chrome（稳定版）**。中转服务通过真实 Chrome 实例（`Playwright` 的 `channel="chrome"`）驱动 Duck.ai；Playwright 自带的 Chromium 会被 Duck.ai 指纹封禁，因此必须使用系统 Chrome。你**不需要**执行 `playwright install chromium`。
  - Windows：从 <https://www.google.com/chrome/> 安装
  - macOS：`brew install --cask google-chrome`（或从 google.com/chrome 下载）
  - Linux（Debian/Ubuntu）：`sudo apt-get install google-chrome-stable`
- 如果 Chrome 不在默认路径，用 `DUCKAI_CHROME_PATH` 指向二进制文件。

## 安装与运行

```bash
# 1. 虚拟环境
python3 -m venv .venv
#    Windows:        .venv\Scripts\activate
#    macOS / Linux:  source .venv/bin/activate

# 2. 依赖
pip install -r requirements.txt

# 3. （可选）配置
cp .env.example .env          # 如需 API Key、代理等再编辑

# 4. 启动
python -m uvicorn main:app --host 0.0.0.0 --port 8080
```

服务监听 `http://localhost:8080`（可用 `PORT` 覆盖）。

## 配置

所有配置均为可选，从环境变量读取（通过 `python-dotenv` 加载 `.env`）。

| 变量                  | 说明                                                                                          | 默认值               |
|-----------------------|-----------------------------------------------------------------------------------------------|----------------------|
| `DUCKAI_API_KEY`      | 所有 `/v1` 请求所需的 Bearer 令牌。**留空 = 开放访问。**                                      | *(空)*               |
| `DUCKAI_BASE`         | Duck.ai 主机地址。                                                                             | `https://duck.ai`    |
| `DUCKAI_MODEL`        | 客户端未指定时的默认模型。                                                                     | `gpt-5.6-luna`       |
| `DUCKAI_NEW_CHAT`     | `1` = 每次请求新建对话（无状态，类 OpenAI/Anthropic）；`0` = 保留会话历史。                     | `0`                  |
| `DUCKAI_PROXIES`      | 逗号分隔的代理池；中转服务借此轮换以绕过按 IP 的 `ERR_BN_LIMIT` 封禁。                         | *(无)*               |
| `DUCKAI_PROXY`        | 单个代理（代理池的替代方案）。                                                                 | *(无)*               |
| `DUCKAI_MAX_CONCURRENCY` | 每个代理在排队前的最大并发请求数。                                                        | `2`                  |
| `DUCKAI_CHROME_PATH`  | 覆盖 Google Chrome 二进制路径（否则按系统自动探测）。                                          | 各系统默认值         |
| `PORT`                | 服务端口。                                                                                     | `8080`               |

## 使用示例

### OpenAI SDK（流式）

```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8080/v1", api_key="t")  # 未开启鉴权时任意 key 均可
stream = c.chat.completions.create(
    model="gpt-5.6-luna",
    messages=[{"role": "user", "content": "用一句话解释量子隧穿。"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

### Anthropic SDK（Claude Code）

```python
from anthropic import Anthropic
c = Anthropic(base_url="http://localhost:8080", api_key="t")
c.messages.create(
    model="gpt-5.6-luna",
    max_tokens=1024,
    messages=[{"role": "user", "content": "2+2 等于多少？"}],
)
```

将 Claude Code 指向中转服务：`ANTHROPIC_BASE_URL=http://localhost:8080` 且 `ANTHROPIC_API_KEY=t`。

### 工具循环（兼容 Claude Code）

像给 Claude 一样发送 `tools`。中转服务根据用户意图发出 `tool_use` 块；你的 agent 在本地执行并返回 `tool_result`，中转服务再把它转发给 Duck.ai 得到有依据的最终回答。

```python
tools=[{"name":"Read","description":"读取文件",
        "input_schema":{"type":"object","properties":{"file_path":{"type":"string"}}}}]
# 用户："读取 README.md"  ->  中转服务返回 tool_use: Read {file_path:"README.md"}
# agent 本地执行 Read，返回 tool_result；中转服务据此作答。
```

## 文件结构

- `duckai.py` — 底层 Duck.ai 客户端（令牌获取 + SSE 聊天、Chrome 会话管理、轮换）。导出 `MODEL_LABELS`、`resolve_model`、`DuckAISession`。
- `main.py` — FastAPI 应用：三个协议端点、请求模型、模型路由、对话扁平化、工具循环接线。
- `tools.py` — 工具 schema 解析 + 提示词渲染（透明性 / 未来原生支持）。
- `toolrouter.py` — 中转端意图 → `tool_use` 的合成。

所有聊天/Anthropic/Responses 逻辑在 `main.py`；Duck.ai 传输在 `duckai.py`。

## 免责声明

与 DuckDuckGo 无关。仅用于学习/个人用途；请遵守 Duck.ai 的服务条款。使用时自行承担限流风险。
