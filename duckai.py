"""Headless Duck.ai chat client (real Chrome + UI-driven, no IP rotation).

WHY THIS DESIGN (root-cause analysis, not guesswork):
  The same machine/IP that the user browses Duck.ai with manually works fine,
  but our Playwright relay got 418 ERR_BN_LIMIT. Two findings from js-reverse:

  1. Playwright's *bundled Chromium* is fingerprinted (TLS/HTTP2/JA3 + UA carries
     "HeadlessChrome") and is hard-banned with ERR_BN_LIMIT on the FIRST request.
     Real Chrome (`channel="chrome"`) passes that layer and instead gets
     ERR_CHALLENGE - i.e. the server is willing to talk, just wants a valid
     client-generated challenge answer.

  2. Calling fetch('/duckchat/v1/chat') by hand (bypassing the app) yields
     ERR_CHALLENGE because x-fe-signals / x-vqd-hash-1 are only produced by the
     app's OWN send logic (real interaction telemetry + challenge JS). Driving
     the real UI - type into the textarea, trigger send - lets the app generate
     the full, valid request. That path returns the real answer ("PONG" in test).

  => The fix is NOT a new IP. It is: real Chrome + drive the app's own send,
     then read the assistant reply from the intercepted chat response.

  We intercept the /duckchat/v1/chat RESPONSE (SSE) and parse `message` chunks,
  which is far more robust than scraping the DOM.

Model catalog (live /duckchat/v1/models snapshot 2026-08-27) + alias map follow.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import AsyncIterator, List, Optional

from playwright.async_api import async_playwright

logger = logging.getLogger("duckai")

BASE = os.getenv("DUCKAI_BASE", "https://duck.ai")
# Use the system Chrome, not Playwright's bundled Chromium (fingerprint reasons above).
CHROME_PATH = os.getenv("DUCKAI_CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
# A real desktop Chrome UA (no "HeadlessChrome" marker). Version pinned to a common
# stable release; must NOT contain "HeadlessChrome".
REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)

# Real backend model ids served by Duck.ai (snapshot 2026-08-27).
MODEL_LABELS = {
    "gpt-5.6-luna": "GPT-5.6 Luna",
    "gpt-5.4": "GPT-5.4",
    "gpt-5.4-mini": "GPT-5.4 mini",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5": "Claude Haiku 4.5",
    "claude-opus-4-8": "Claude Opus 4.8",
    "mistral-small-2603": "Mistral Small 4",
    "tinfoil/gpt-oss-120b": "gpt-oss 120B",
    "tinfoil/gemma4-31b": "Gemma 4 31B",
}
DEFAULT_MODEL = "gpt-5.6-luna"

MODEL_ALIASES = {
    "gpt-5.6-sol": "gpt-5.6-luna",
    "gpt-5.6": "gpt-5.6-luna",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.5": "gpt-5.4",
    "gpt-5": "gpt-5.4",
    "gpt-4o": "gpt-5.4",
    "gpt-4o-mini": "gpt-5.4-mini",
    "o3-mini": "gpt-5.4-mini",
    "claude-3-5-sonnet": "claude-sonnet-4-6",
    "claude-3.7-sonnet": "claude-sonnet-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-3-haiku": "claude-haiku-4-5",
    "claude-haiku": "claude-haiku-4-5",
    "claude-haiku-4-5": "claude-haiku-4-5",
    "claude-3-opus": "claude-opus-4-8",
    "claude-opus": "claude-opus-4-8",
    "claude-opus-4-8": "claude-opus-4-8",
    "mistral-small": "mistral-small-2603",
    "mistral-small-2603": "mistral-small-2603",
    "gpt-oss-120b": "tinfoil/gpt-oss-120b",
    "tinfoil/gpt-oss-120b": "tinfoil/gpt-oss-120b",
    "gemma4-31b": "tinfoil/gemma4-31b",
    "tinfoil/gemma4-31b": "tinfoil/gemma4-31b",
}

MODEL_FAMILY = {
    "gpt-5.6": "gpt-5.6-luna",
    "gpt-5.4": "gpt-5.4",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-haiku": "claude-haiku-4-5",
    "claude-opus": "claude-opus-4-8",
    "claude": "claude-sonnet-4-6",
    "mistral": "mistral-small-2603",
    "gpt-oss": "tinfoil/gpt-oss-120b",
    "gemma": "tinfoil/gemma4-31b",
}


def resolve_model(name: str | None) -> str:
    """Map any user-facing model name to a real Duck.ai backend id."""
    if not name:
        return DEFAULT_MODEL
    n = name.strip()
    if n in MODEL_LABELS:
        return n
    low = n.lower()
    if low in MODEL_ALIASES:
        return MODEL_ALIASES[low]
    for k, v in MODEL_ALIASES.items():
        if k.lower() == low:
            return v
    for fam in sorted(MODEL_FAMILY, key=len, reverse=True):
        if low.startswith(fam):
            return MODEL_FAMILY[fam]
    return n


class DuckAIError(Exception):
    pass


class DuckAIRateLimit(DuckAIError):
    pass


class _Null:
    banned = False


_NULL = _Null()


class _BrowserSession:
    """One real-Chrome tab, UI-driven send, response intercepted from the wire."""

    def __init__(self, model: str, proxy: Optional[str], timeout: float) -> None:
        self.model = model
        self.proxy = proxy
        self.timeout = timeout
        self._pw = None
        self.browser = None
        self.ctx = None
        self.page = None
        self._ready = False
        self.banned = False

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        self._pw = await async_playwright().start()
        launch: dict = {"headless": True, "channel": "chrome"}
        if CHROME_PATH and os.path.exists(CHROME_PATH):
            launch["executable_path"] = CHROME_PATH
        launch["args"] = ["--disable-blink-features=AutomationControlled"]
        self.browser = await self._pw.chromium.launch(**launch)
        self.ctx = await self.browser.new_context(user_agent=REAL_UA)
        # hide navigator.webdriver so the app can't tell it's automated, and
        # install a fetch hook that captures the chat SSE (cloned, non-intrusive).
        await self.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "(function(){const O=window.fetch;"
            "window.__duckai={last:null,err:null};"
            "window.fetch=async function(u,o){const r=await O.apply(this,arguments);"
            "if(typeof u==='string'&&u.includes('/duckchat/v1/chat')){"
            "try{const c=r.clone();const t=await c.text();"
            "if(t.includes('\"action\":\"error\"')){window.__duckai.err=t;}else{window.__duckai.last=t;}}catch(e){}"
            "}return r;};})();"
        )
        # No warm-up page here; send_ui opens a fresh page per request and warms it.
        self._ready = True

    async def send_ui(self, prompt: str, timeout: float = 120.0) -> str:
        """Drive the real UI to send, then read the assistant reply from a fetch hook.

        We type into the textarea and click send so the app generates its full,
        valid request (token + x-fe-signals). A page-level fetch hook (installed in
        _ensure_ready) clones the chat SSE and stores it on window.__duckai, which
        we poll - this avoids Playwright's inability to read streaming bodies.

        A FRESH page is used per request to avoid cross-request UI state pollution.
        """
        await self._ensure_ready()
        page = await self.ctx.new_page()
        try:
            await page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
            # Warm-up: Duck.ai mints its challenge/token JS during the first seconds
            # of a fresh page. Too short -> the first send hits ERR_BN_LIMIT. 7s is safe.
            await page.wait_for_timeout(7000)
            ta = await page.query_selector("textarea")
            if ta is None:
                raise DuckAIError("duck.ai textarea not found (page layout changed)")
            await ta.click()
            await ta.fill(prompt)
            await page.wait_for_timeout(300)
            sent = False
            for btn in await page.query_selector_all("button"):
                t = (await btn.inner_text() or "").strip()
                a = await btn.get_attribute("aria-label") or ""
                if t in ("Ask", "Send", "问", "发送") or a in ("Ask", "Send", "问", "发送"):
                    await btn.click(force=True)
                    sent = True
                    break
            if not sent:
                await ta.press("Enter")

            # Poll the fetch hook for the captured SSE (or an error payload).
            raw = None
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                await page.wait_for_timeout(1000)
                err, last = await page.evaluate(
                    "()=>[window.__duckai.err, window.__duckai.last]"
                )
                logger.info("poll: err=%s last_len=%s", bool(err), len(last or ""))
                if err:
                    self._raise_err(err)
                if last:
                    raw = last
                    # stream may still be appending; wait for it to stabilize
                    await page.wait_for_timeout(1500)
                    last2 = await page.evaluate("()=>window.__duckai.last")
                    if last2 == last:
                        break
                    raw = last2
            if not raw:
                raise DuckAIError("timed out waiting for Duck.ai reply")
            return self._parse_messages(raw)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    @staticmethod
    def _parse_messages(text: str) -> str:
        chunks = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                # SSE: data: {...}
                if line.startswith("data:"):
                    line = line[5:].strip()
                else:
                    continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                msg = obj.get("message")
                if msg:
                    chunks.append(msg)
        return "".join(chunks)

    @staticmethod
    def _raise_err(text: str) -> None:
        m = re.search(r'"type"\s*:\s*"([^"]+)"', text)
        typ = m.group(1) if m else "error"
        if "ERR_BN_LIMIT" in text:
            raise DuckAIRateLimit("Duck.ai ERR_BN_LIMIT (IP/fingerprint banned)")
        if "ERR_CHALLENGE" in text:
            raise DuckAIError("Duck.ai ERR_CHALLENGE (challenge required)")
        raise DuckAIError(f"Duck.ai error: {typ}")

    async def close(self) -> None:
        try:
            if self.browser:
                await self.browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass


class DuckAISession:
    """Thin pool over _BrowserSession; rotates on ban.

    The fundamental fix for the ERR_BN_LIMIT the user hit is real Chrome + UI-driven
    send (see module docstring) - NOT IP rotation. Proxy is optional, kept only for
    users who genuinely need it.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        proxies: Optional[List[str]] = None,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.timeout = timeout
        if not proxies:
            self.proxies: List[Optional[str]] = [None]
        elif isinstance(proxies, str):
            self.proxies = [p.strip() or None for p in proxies.split(",")]
        else:
            self.proxies = [p.strip() or None for p in proxies]
        self._sessions: dict = {}
        self._order = list(range(len(self.proxies)))
        self._idx = 0

    def _next_proxy_index(self) -> Optional[int]:
        healthy = [i for i in self._order if not self._sessions.get(i, _NULL).banned]
        if not healthy:
            return None
        for i in range(len(self._order)):
            cand = (self._idx + i) % len(self._order)
            if not self._sessions.get(cand, _NULL).banned:
                self._idx = cand
                return cand
        return healthy[0]

    async def _session_for(self, idx: int) -> _BrowserSession:
        if idx not in self._sessions:
            self._sessions[idx] = _BrowserSession(self.model, self.proxies[idx], self.timeout)
        return self._sessions[idx]

    async def send(self, prompt: str, max_rotations: int = 2) -> str:
        last_err: Optional[Exception] = None
        for _ in range(max(1, max_rotations)):
            idx = self._next_proxy_index()
            if idx is None:
                raise DuckAIRateLimit("all sessions failed (Duck.ai ban)")
            try:
                return await (await self._session_for(idx)).send_ui(prompt, self.timeout)
            except DuckAIRateLimit as e:
                last_err = e
                self._sessions.get(idx).banned = True
                continue
        raise last_err or DuckAIRateLimit("all sessions failed")

    async def send_stream(self, prompt: str, max_rotations: int = 2) -> AsyncIterator[str]:
        """Duck.ai UI is not natively streamable here; we return the full reply as
        one chunk (no token-level streaming over the intercepted response)."""
        text = await self.send(prompt, max_rotations)
        yield text

    async def close(self) -> None:
        for s in self._sessions.values():
            await s.close()
