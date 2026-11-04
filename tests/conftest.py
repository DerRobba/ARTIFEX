from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Callable

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "inference_server"))

from artifex.config import ArtifexConfig  # noqa: E402
from artifex.llm import LLMClient  # noqa: E402

HAS_OPENSCAD = shutil.which("openscad") is not None
needs_openscad = pytest.mark.skipif(not HAS_OPENSCAD, reason="OpenSCAD not installed")


@pytest.fixture
def cfg(tmp_path: Path) -> ArtifexConfig:
    return ArtifexConfig(
        data_dir=str(tmp_path / "data"),
        llm={"enabled": False},
        inference={"url": "http://inference.test"},
        printer={"moonraker_url": "http://moonraker.test"},
    )


class FakeLLM:
    """Scriptable OpenAI-compatible endpoint. ``handlers`` map a task keyword found in
    the system prompt ("design planner", "OpenSCAD", "slicing expert") to a callable
    returning the assistant text."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[list[dict]], str]] = {}
        self.calls: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": []})
            body = json.loads(request.content)
            self.calls.append(body)
            system = body["messages"][0]["content"]
            for key, fn in self.handlers.items():
                if key in system:
                    text = fn(body["messages"])
                    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})
            return httpx.Response(500, text="no handler")

        return httpx.MockTransport(handle)

    def client(self, cfg: ArtifexConfig) -> LLMClient:
        cfg.llm.enabled = True
        cfg.llm.base_url = "http://llm.test/v1"
        cfg.llm.max_retries = 0
        return LLMClient(cfg.llm, http=httpx.Client(transport=self.transport()))


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()
