"""Organic path: z-image-turbo (text -> image) + Hunyuan3D-2.1 (image -> mesh).

Both models need a CUDA GPU, so they run in the ARTIFEX inference server
(``inference_server/``) on a PC/cloud machine; the Raspberry Pi only talks HTTP to it.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from ..config import ArtifexConfig
from .plan import DesignPlan

log = logging.getLogger(__name__)


class OrganicError(RuntimeError):
    pass


class InferenceClient:
    def __init__(self, cfg: ArtifexConfig, http: httpx.Client | None = None):
        self.cfg = cfg.inference
        self._http = http or httpx.Client(timeout=self.cfg.timeout_s)
        key = self.cfg.resolved_api_key()
        self._headers = {"Authorization": f"Bearer {key}"} if key else {}

    def _url(self, path: str) -> str:
        return self.cfg.url.rstrip("/") + path

    def health(self) -> dict | None:
        try:
            r = self._http.get(self._url("/health"), headers=self._headers, timeout=5)
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    def generate_image(self, prompt: str, out_path: Path, seed: int | None = None) -> Path:
        body = {
            "prompt": prompt,
            "width": self.cfg.image_size,
            "height": self.cfg.image_size,
            "steps": self.cfg.image_steps,
            "seed": seed,
        }
        try:
            r = self._http.post(self._url("/v1/image"), json=body, headers=self._headers, timeout=self.cfg.timeout_s)
        except httpx.HTTPError as exc:
            raise OrganicError(f"Inference-Server nicht erreichbar ({self.cfg.url}): {exc}") from exc
        if r.status_code != 200:
            raise OrganicError(f"Bildgenerierung fehlgeschlagen: HTTP {r.status_code} {r.text[:200]}")
        out_path.write_bytes(r.content)
        return out_path

    def image_to_mesh(self, image_path: Path, out_path: Path, seed: int | None = None) -> Path:
        data = {
            "steps": str(self.cfg.mesh_steps),
            "octree_resolution": str(self.cfg.octree_resolution),
            "max_faces": str(self.cfg.max_faces),
            "remove_background": "true",
        }
        if seed is not None:
            data["seed"] = str(seed)
        try:
            with open(image_path, "rb") as fh:
                r = self._http.post(
                    self._url("/v1/mesh"),
                    data=data,
                    files={"image": (image_path.name, fh, "image/png")},
                    headers=self._headers,
                    timeout=self.cfg.timeout_s,
                )
        except httpx.HTTPError as exc:
            raise OrganicError(f"Inference-Server nicht erreichbar ({self.cfg.url}): {exc}") from exc
        if r.status_code != 200:
            raise OrganicError(f"3D-Generierung fehlgeschlagen: HTTP {r.status_code} {r.text[:200]}")
        out_path.write_bytes(r.content)
        return out_path


class OrganicGenerator:
    def __init__(self, cfg: ArtifexConfig, client: InferenceClient):
        self.cfg = cfg
        self.client = client

    def generate_image(self, plan: DesignPlan, workdir: Path, seed: int | None = None) -> Path:
        prompt = plan.image_prompt or plan.refined_prompt
        return self.client.generate_image(prompt, workdir / "image.png", seed=seed)

    def image_to_mesh(self, image_path: Path, workdir: Path, seed: int | None = None) -> Path:
        return self.client.image_to_mesh(image_path, workdir / "raw_mesh.stl", seed=seed)
