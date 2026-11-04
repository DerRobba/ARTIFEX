"""Configuration loading for the ARTIFEX host software.

The configuration lives in a single YAML file (default:
``~/printer_data/config/artifex.yaml`` so it can be edited from Mainsail/Fluidd
next to ``printer.cfg``). Every value has a sensible default, so an empty file
is a valid configuration. Secrets can be supplied via environment variables.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    """OpenAI-compatible chat endpoint (OpenAI cloud, Ollama, llama.cpp, LM Studio, vLLM ...)."""

    enabled: bool = True
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None  # falls back to $ARTIFEX_LLM_API_KEY / $OPENAI_API_KEY
    model: str = "gpt-5-mini"
    # Optional per-task model overrides (e.g. a strong coder model for OpenSCAD).
    router_model: str | None = None
    codegen_model: str | None = None
    settings_model: str | None = None
    # Some models (e.g. reasoning models) reject a custom temperature -> None = don't send.
    temperature: float | None = None
    # "json_object" works with OpenAI, Ollama, llama.cpp, vLLM. Use "none" for servers
    # without JSON mode - the answer is then parsed leniently.
    json_mode: Literal["json_object", "none"] = "json_object"
    timeout_s: float = 120.0
    max_retries: int = 2

    def resolved_api_key(self) -> str | None:
        return self.api_key or os.environ.get("ARTIFEX_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")

    def model_for(self, task: str) -> str:
        return getattr(self, f"{task}_model", None) or self.model


class InferenceConfig(BaseModel):
    """GPU inference server running z-image-turbo + Hunyuan3D-2.1 (see inference_server/)."""

    url: str = "http://artifex-gpu.local:8765"
    api_key: str | None = None  # falls back to $ARTIFEX_INFERENCE_KEY
    timeout_s: float = 900.0
    image_size: int = 1024
    image_steps: int = 9
    mesh_steps: int = 30
    octree_resolution: int = 256
    max_faces: int = 150_000

    def resolved_api_key(self) -> str | None:
        return self.api_key or os.environ.get("ARTIFEX_INFERENCE_KEY")


class OpenSCADConfig(BaseModel):
    binary: str = "openscad"
    timeout_s: float = 180.0
    max_attempts: int = 4
    # Libraries the model may `use <...>`/`include <...>` (e.g. "BOSL2/"). Everything
    # else (file imports, absolute paths) is rejected for safety.
    allowed_libraries: list[str] = Field(default_factory=list)
    # Extra CLI args, e.g. ["--backend=manifold"] for OpenSCAD >= 2024 (much faster).
    extra_args: list[str] = Field(default_factory=list)


class PrinterConfig(BaseModel):
    name: str = "ARTIFEX"
    moonraker_url: str = "http://127.0.0.1:7125"
    moonraker_api_key: str | None = None
    bed_x: float = 235.0
    bed_y: float = 235.0
    max_z: float = 250.0
    # Keep-out margin around the bed edge (clips, skirt, purge line).
    margin: float = 8.0
    nozzle_diameter: float = 0.4
    filament_diameter: float = 1.75
    upload_folder: str = "artifex"
    poll_interval_s: float = 1.0


class SlicerConfig(BaseModel):
    backend: Literal["artifex", "orca"] = "artifex"
    # OrcaSlicer CLI (only used when backend == "orca", or as fallback for supports).
    orca_command: str = "orca-slicer"
    orca_machine_profile: str | None = None
    orca_process_profile: str | None = None
    orca_filament_profile: str | None = None
    orca_timeout_s: float = 600.0
    ai_settings: bool = True


class AFSConfig(BaseModel):
    slots: int = 4


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class ArtifexConfig(BaseModel):
    data_dir: str = "~/printer_data/artifex"
    language: str = "de"
    default_size_mm: float = 60.0
    # Start printing automatically once slicing is done (otherwise the user confirms on
    # the display - recommended, because the bed has to be clear).
    auto_print: bool = False
    llm: LLMConfig = Field(default_factory=LLMConfig)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    openscad: OpenSCADConfig = Field(default_factory=OpenSCADConfig)
    printer: PrinterConfig = Field(default_factory=PrinterConfig)
    slicer: SlicerConfig = Field(default_factory=SlicerConfig)
    afs: AFSConfig = Field(default_factory=AFSConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    @property
    def data_path(self) -> Path:
        p = Path(os.path.expanduser(self.data_dir))
        p.mkdir(parents=True, exist_ok=True)
        return p


DEFAULT_CONFIG_LOCATIONS = (
    "~/printer_data/config/artifex.yaml",
    "./artifex.yaml",
    "./config.yaml",
)


def find_config_file(explicit: str | None = None) -> Path | None:
    candidates = [explicit, os.environ.get("ARTIFEX_CONFIG"), *DEFAULT_CONFIG_LOCATIONS]
    for c in candidates:
        if c:
            p = Path(os.path.expanduser(c))
            if p.is_file():
                return p
    return None


def load_config(path: str | None = None) -> ArtifexConfig:
    cfg_file = find_config_file(path)
    data: dict = {}
    if cfg_file is not None:
        with open(cfg_file, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    return ArtifexConfig.model_validate(data)
