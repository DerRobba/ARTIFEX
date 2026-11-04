"""ARTIFEX GPU inference server.

Runs on a PC / cloud machine with an NVIDIA GPU (the Raspberry Pi cannot run these
models) and exposes two endpoints used by the printer:

    POST /v1/image   {"prompt": ..., "width": 1024, "height": 1024, "steps": 9, "seed": 1}
                     -> image/png               (Tongyi-MAI/Z-Image-Turbo via diffusers)
    POST /v1/mesh    multipart: image=<png>, steps, octree_resolution, max_faces, seed
                     -> model/stl (binary, Z-up, millimetre-agnostic)   (tencent/Hunyuan3D-2.1)
    GET  /health

Start:  python server.py --host 0.0.0.0 --port 8765 [--mock] [--low-vram]
Auth:   set ARTIFEX_INFERENCE_KEY and send "Authorization: Bearer <key>".
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

log = logging.getLogger("artifex-inference")

IMAGE_MODEL = os.environ.get("ARTIFEX_IMAGE_MODEL", "Tongyi-MAI/Z-Image-Turbo")
MESH_MODEL = os.environ.get("ARTIFEX_MESH_MODEL", "tencent/Hunyuan3D-2.1")
HUNYUAN_DIR = os.environ.get("HUNYUAN3D_DIR", str(Path(__file__).parent / "Hunyuan3D-2.1"))


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    width: int = Field(default=1024, ge=256, le=2048)
    height: int = Field(default=1024, ge=256, le=2048)
    steps: int = Field(default=9, ge=1, le=50)
    seed: int | None = None


class Models:
    """Lazily loaded model pipelines. One GPU job at a time (lock)."""

    def __init__(self, mock: bool, low_vram: bool, device: str):
        self.mock = mock
        self.low_vram = low_vram
        self.device = device
        self.lock = threading.Lock()
        self.image_pipe = None
        self.mesh_pipe = None
        self.rembg = None

    # -- text -> image ---------------------------------------------------------------
    def load_image(self):
        if self.image_pipe is None:
            import torch
            from diffusers import ZImagePipeline

            log.info("Loading %s", IMAGE_MODEL)
            pipe = ZImagePipeline.from_pretrained(IMAGE_MODEL, torch_dtype=torch.bfloat16)
            if self.low_vram:
                pipe.enable_model_cpu_offload()
            else:
                pipe.to(self.device)
            self.image_pipe = pipe
        return self.image_pipe

    def image(self, req: ImageRequest) -> bytes:
        if self.mock:
            return _mock_image(req.prompt, req.width, req.height)
        import torch

        with self.lock:
            if self.low_vram:
                self._free_mesh()
            pipe = self.load_image()
            gen = torch.Generator(self.device).manual_seed(req.seed if req.seed is not None else int(time.time()))
            img = pipe(
                prompt=req.prompt, height=req.height, width=req.width,
                num_inference_steps=req.steps, guidance_scale=0.0, generator=gen,
            ).images[0]
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # -- image -> mesh ---------------------------------------------------------------
    def load_mesh(self):
        if self.mesh_pipe is None:
            for sub in ("", "hy3dshape"):
                p = str(Path(HUNYUAN_DIR) / sub)
                if p not in sys.path:
                    sys.path.insert(0, p)
            from hy3dshape.pipelines import Hunyuan3DDiTFlowMatchingPipeline
            from hy3dshape.rembg import BackgroundRemover

            log.info("Loading %s", MESH_MODEL)
            self.mesh_pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(MESH_MODEL)
            self.rembg = BackgroundRemover()
        return self.mesh_pipe

    def _free_mesh(self):
        if self.mesh_pipe is not None:
            import torch

            self.mesh_pipe = None
            torch.cuda.empty_cache()

    def _free_image(self):
        if self.image_pipe is not None:
            import torch

            self.image_pipe = None
            torch.cuda.empty_cache()

    def mesh(self, png: bytes, steps: int, octree: int, max_faces: int, seed: int | None, remove_bg: bool) -> bytes:
        import numpy as np
        import trimesh
        from PIL import Image

        if self.mock:
            mesh = _mock_mesh(png)
        else:
            import torch

            with self.lock:
                if self.low_vram:
                    self._free_image()
                pipe = self.load_mesh()
                image = Image.open(io.BytesIO(png))
                if remove_bg or image.mode != "RGBA":
                    image = self.rembg(image.convert("RGB"))
                gen = torch.manual_seed(seed if seed is not None else int(time.time()))
                try:
                    out = pipe(image=image, num_inference_steps=steps, octree_resolution=octree,
                               generator=gen, output_type="trimesh")
                except TypeError:  # older/newer signature
                    out = pipe(image=image)
                mesh = out[0]
                mesh = _postprocess_hunyuan(mesh, max_faces)
            # Hunyuan3D outputs Y-up (glTF convention) -> printer coordinates are Z-up.
            mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
        if len(mesh.faces) > max_faces:
            try:
                mesh = mesh.simplify_quadric_decimation(face_count=max_faces)
            except Exception as exc:  # noqa: BLE001
                log.warning("decimation failed: %s", exc)
        buf = io.BytesIO()
        mesh.export(buf, file_type="stl")
        return buf.getvalue()


def _postprocess_hunyuan(mesh, max_faces: int):
    try:
        from hy3dshape.postprocessors import DegenerateFaceRemover, FaceReducer, FloaterRemover

        mesh = FloaterRemover()(mesh)
        mesh = DegenerateFaceRemover()(mesh)
        mesh = FaceReducer()(mesh, max_facenum=max_faces)
    except Exception as exc:  # noqa: BLE001 - postprocessors are optional
        log.warning("Hunyuan post-processing skipped: %s", exc)
    return mesh


def _mock_image(prompt: str, w: int, h: int) -> bytes:
    """Deterministic placeholder for tests/demos without a GPU."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    r = min(w, h) // 3
    d.ellipse([w // 2 - r, h // 2 - r, w // 2 + r, h // 2 + r], fill=(180, 180, 190), outline=(90, 90, 100), width=6)
    d.text((20, 20), f"MOCK: {prompt[:60]}", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _mock_mesh(png: bytes):
    """A small 'figure' (body + head + feet) so orientation/flattening code is exercised."""
    import trimesh

    body = trimesh.creation.icosphere(subdivisions=3, radius=1.0)
    body.apply_scale([1.0, 0.85, 1.2])
    head = trimesh.creation.icosphere(subdivisions=3, radius=0.62)
    head.apply_translation([0, 0, 1.45])
    ear1 = trimesh.creation.cone(radius=0.22, height=0.5)
    ear1.apply_translation([0.32, 0, 1.9])
    ear2 = ear1.copy()
    ear2.apply_translation([-0.64, 0, 0])
    parts = [body, head, ear1, ear2]
    try:
        mesh = trimesh.boolean.union(parts)
    except Exception:  # noqa: BLE001 - no boolean backend
        mesh = trimesh.util.concatenate(parts)
    return mesh


def create_app(models: Models, api_key: str | None) -> FastAPI:
    app = FastAPI(title="ARTIFEX inference server")

    def auth(authorization: str | None = Header(default=None)) -> None:
        if api_key and authorization != f"Bearer {api_key}":
            raise HTTPException(401, "invalid api key")

    @app.get("/health")
    def health() -> dict:
        info = {"ok": True, "mock": models.mock, "image_model": IMAGE_MODEL, "mesh_model": MESH_MODEL,
                "image_loaded": models.image_pipe is not None, "mesh_loaded": models.mesh_pipe is not None}
        if not models.mock:
            try:
                import torch

                info["cuda"] = torch.cuda.is_available()
                if torch.cuda.is_available():
                    info["gpu"] = torch.cuda.get_device_name(0)
            except ImportError:
                info["cuda"] = False
        return info

    @app.post("/v1/image", dependencies=[Depends(auth)])
    def image(req: ImageRequest) -> Response:
        t = time.time()
        data = models.image(req)
        log.info("image %.1fs: %s", time.time() - t, req.prompt[:80])
        return Response(content=data, media_type="image/png")

    @app.post("/v1/mesh", dependencies=[Depends(auth)])
    async def mesh(
        image: UploadFile = File(...),
        steps: int = Form(30),
        octree_resolution: int = Form(256),
        max_faces: int = Form(150_000),
        seed: int | None = Form(None),
        remove_background: bool = Form(True),
    ) -> Response:
        png = await image.read()
        t = time.time()
        import asyncio

        data = await asyncio.to_thread(models.mesh, png, steps, octree_resolution, max_faces, seed, remove_background)
        log.info("mesh %.1fs", time.time() - t)
        return Response(content=data, media_type="model/stl")

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mock", action="store_true", help="no GPU: return placeholder image/mesh")
    ap.add_argument("--low-vram", action="store_true", help="keep only one model on the GPU at a time")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--preload", action="store_true", help="load models at startup instead of on first use")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    models = Models(mock=args.mock, low_vram=args.low_vram, device=args.device)
    if args.preload and not args.mock:
        models.load_image()
        if not args.low_vram:
            models.load_mesh()
    import uvicorn

    uvicorn.run(create_app(models, os.environ.get("ARTIFEX_INFERENCE_KEY")), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
