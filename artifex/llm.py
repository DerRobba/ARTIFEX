"""Minimal OpenAI-compatible chat client.

Uses plain HTTP (``POST {base_url}/chat/completions``) instead of a vendor SDK so the
same code talks to OpenAI, Ollama, llama.cpp-server, LM Studio or vLLM - the README's
"local or cloud" requirement.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .config import LLMConfig

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMHTTPError(LLMError):
    def __init__(self, status: int, body: str):
        super().__init__(f"LLM HTTP {status}: {body[:300]}")
        self.status = status


_FENCE_RE = re.compile(r"```(?P<lang>[a-zA-Z0-9_+-]*)\s*\n(?P<body>.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model answer (tolerates code fences and chatter)."""
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    for m in _FENCE_RE.finditer(text):
        try:
            obj = json.loads(m.group("body"))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    # Fall back to the outermost {...} span.
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    raise LLMError("Model answer contained no valid JSON object")


def extract_code(text: str, languages: tuple[str, ...] = ("openscad", "scad")) -> str:
    """Return the first fenced code block (preferring the given languages) or the raw text."""
    blocks = list(_FENCE_RE.finditer(text))
    for m in blocks:
        if m.group("lang").lower() in languages:
            return m.group("body").strip()
    if blocks:
        return blocks[0].group("body").strip()
    return text.strip()


class LLMClient:
    def __init__(self, cfg: LLMConfig, http: httpx.Client | None = None):
        self.cfg = cfg
        headers = {"Content-Type": "application/json"}
        key = cfg.resolved_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self._http = http or httpx.Client(timeout=cfg.timeout_s)
        self._headers = headers

    @property
    def available(self) -> bool:
        return self.cfg.enabled and bool(self.cfg.base_url)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        task: str = "router",
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        if not self.available:
            raise LLMError("LLM is disabled in the configuration")
        payload: dict[str, Any] = {"model": self.cfg.model_for(task), "messages": messages}
        if self.cfg.temperature is not None:
            payload["temperature"] = self.cfg.temperature
        if json_mode and self.cfg.json_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_completion_tokens"] = max_tokens
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                r = self._http.post(url, json=payload, headers=self._headers, timeout=self.cfg.timeout_s)
                if r.status_code >= 400:
                    raise LLMHTTPError(r.status_code, r.text)
                data = r.json()
                content = data["choices"][0]["message"].get("content") or ""
                if not content.strip():
                    raise LLMError("LLM returned an empty answer")
                return content
            except (httpx.HTTPError, LLMError, KeyError, ValueError) as exc:
                last_exc = exc
                # Client errors (bad key/model) will not get better by retrying.
                if isinstance(exc, LLMHTTPError) and 400 <= exc.status < 500 and exc.status != 429:
                    break
                log.warning("LLM request failed (attempt %d): %s", attempt + 1, exc)
        raise LLMError(f"LLM request failed: {last_exc}")

    def chat_json(self, system: str, user: str, *, task: str = "router") -> dict[str, Any]:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        answer = self.chat(messages, task=task, json_mode=True)
        try:
            return extract_json(answer)
        except LLMError:
            # One repair round: show the model its own answer.
            messages += [
                {"role": "assistant", "content": answer},
                {"role": "user", "content": "That was not valid JSON. Reply with ONLY the JSON object."},
            ]
            return extract_json(self.chat(messages, task=task, json_mode=True))

    def ping(self) -> bool:
        try:
            url = self.cfg.base_url.rstrip("/") + "/models"
            r = self._http.get(url, headers=self._headers, timeout=5)
            return r.status_code < 500
        except httpx.HTTPError:
            return False
