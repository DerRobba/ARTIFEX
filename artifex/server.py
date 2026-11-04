"""FastAPI application: REST API + WebSocket for the touch display and LAN browsers."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import ArtifexConfig, load_config
from .jobs import JobManager, JobStore
from .llm import LLMClient
from .pipeline.organic import InferenceClient, OrganicGenerator
from .pipeline.parametric import ParametricGenerator
from .pipeline.plan import JobOptions
from .pipeline.router import Router
from .pipeline.settings_ai import SettingsAdvisor
from .printer.afs import AFSStore
from .printer.moonraker import Moonraker, MoonrakerError
from .slicer.settings import MATERIALS

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"
JOB_FILES = {"model.scad", "source.stl", "print.stl", "print.gcode", "image.png"}


class EventBus:
    """Fan-out of JSON events to all WebSocket clients (thread-safe publish)."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.Queue] = set()

    def publish(self, event: dict[str, Any]) -> None:
        if self.loop is None:
            return
        try:
            self.loop.call_soon_threadsafe(self._fanout, event)
        except RuntimeError:  # loop closed
            pass

    def _fanout(self, event: dict[str, Any]) -> None:
        for q in list(self.clients):
            if q.qsize() < 200:
                q.put_nowait(event)


@dataclass
class Services:
    cfg: ArtifexConfig
    llm: LLMClient
    inference: InferenceClient
    moonraker: Moonraker
    afs: AFSStore
    jobs: JobManager
    events: EventBus
    printer_status: dict[str, Any]


def build_services(cfg: ArtifexConfig, *, llm: LLMClient | None = None, inference: InferenceClient | None = None,
                   moonraker: Moonraker | None = None) -> Services:
    events = EventBus()
    llm = llm or LLMClient(cfg.llm)
    inference = inference or InferenceClient(cfg)
    moonraker = moonraker or Moonraker(cfg.printer)
    afs = AFSStore(cfg.data_path / "afs.json", cfg.afs.slots)
    store = JobStore(cfg.data_path / "jobs.sqlite3")
    jobs = JobManager(
        cfg, store,
        Router(cfg, llm), ParametricGenerator(cfg, llm), OrganicGenerator(cfg, inference),
        SettingsAdvisor(cfg, llm), moonraker, afs, publish=events.publish,
    )
    return Services(cfg, llm, inference, moonraker, afs, jobs, events, {"connected": False})


# --- request models -------------------------------------------------------------------
class CreateJob(BaseModel):
    prompt: str = ""
    mode: str = "auto"
    size_mm: float | None = Field(default=None, ge=5, le=400)
    slot: int = 0
    material: str | None = None
    auto_print: bool | None = None
    parent_id: str | None = None
    feedback: str | None = None
    slice_overrides: dict[str, Any] = Field(default_factory=dict)


class Reslice(BaseModel):
    size_mm: float | None = Field(default=None, ge=5, le=400)
    slot: int | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)


class PrinterAction(BaseModel):
    action: str
    value: float | None = None
    script: str | None = None


class SlotUpdate(BaseModel):
    material: str | None = None
    color: str | None = None
    name: str | None = None
    loaded: bool | None = None


def create_app(cfg: ArtifexConfig | None = None, services: Services | None = None, *, start_worker: bool = True,
               poll_printer: bool = True) -> FastAPI:
    cfg = cfg or (services.cfg if services else load_config())
    svc = services or build_services(cfg)

    async def poll_printer_loop() -> None:
        while True:
            try:
                st = await asyncio.to_thread(svc.moonraker.status)
                svc.printer_status.clear()
                svc.printer_status.update(st)
                svc.printer_status["ts"] = time.time()
                svc.events.publish({"type": "printer", "status": st})
                await asyncio.to_thread(svc.jobs.on_printer_status, st)
            except Exception:  # noqa: BLE001
                log.exception("printer poll failed")
            interval = svc.cfg.printer.poll_interval_s if svc.events.clients else max(5.0, svc.cfg.printer.poll_interval_s)
            await asyncio.sleep(interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc.events.loop = asyncio.get_running_loop()
        if start_worker:
            svc.jobs.start()
        task = asyncio.create_task(poll_printer_loop()) if poll_printer else None
        yield
        if task:
            task.cancel()
        svc.jobs.stop()

    app = FastAPI(title="ARTIFEX", version=__version__, lifespan=lifespan)
    app.state.services = svc

    # --- system ----------------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

    @app.get("/api/system")
    async def system() -> dict:
        llm_ok, inf = await asyncio.gather(asyncio.to_thread(svc.llm.ping), asyncio.to_thread(svc.inference.health))
        from .pipeline.parametric import openscad_version

        return {
            "version": __version__,
            "printer_name": svc.cfg.printer.name,
            "llm": {"enabled": svc.cfg.llm.enabled, "reachable": llm_ok, "model": svc.cfg.llm.model,
                    "base_url": svc.cfg.llm.base_url},
            "inference": {"url": svc.cfg.inference.url, "reachable": inf is not None, "info": inf},
            "openscad": openscad_version(svc.cfg.openscad.binary),
            "slicer": svc.cfg.slicer.backend,
            "printer": svc.printer_status,
            "build_volume": [svc.cfg.printer.bed_x, svc.cfg.printer.bed_y, svc.cfg.printer.max_z],
            "materials": list(MATERIALS),
            "default_size_mm": svc.cfg.default_size_mm,
        }

    # --- jobs ------------------------------------------------------------------------
    @app.get("/api/jobs")
    def list_jobs(limit: int = 50) -> list[dict]:
        return [j.summary() for j in svc.jobs.store.list(limit)]

    @app.post("/api/jobs")
    def create_job(body: CreateJob) -> dict:
        if body.mode not in ("auto", "organic", "parametric"):
            raise HTTPException(400, "mode muss auto, organic oder parametric sein")
        opts = JobOptions(
            mode=body.mode, size_mm=body.size_mm, slot=body.slot, material=body.material,
            auto_print=body.auto_print, parent_id=body.parent_id, feedback=body.feedback,
            slice_overrides=body.slice_overrides,
        )
        try:
            job = svc.jobs.submit_prompt(body.prompt, opts)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return job.model_dump(mode="json")

    @app.post("/api/jobs/upload")
    async def upload_job(file: UploadFile = File(...), size_mm: float | None = Form(None), slot: int = Form(0)) -> dict:
        data = await file.read()
        if len(data) > 200 * 1024 * 1024:
            raise HTTPException(413, "Datei zu groß")
        try:
            job = svc.jobs.submit_upload(file.filename or "upload.stl", data, JobOptions(size_mm=size_mm, slot=slot))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return job.model_dump(mode="json")

    def _job(job_id: str):
        job = svc.jobs.store.get(job_id)
        if job is None:
            raise HTTPException(404, "Auftrag nicht gefunden")
        return job

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        return _job(job_id).model_dump(mode="json")

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str) -> dict:
        _job(job_id)
        try:
            svc.jobs.delete(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict:
        _job(job_id)
        return svc.jobs.cancel(job_id).model_dump(mode="json")

    @app.post("/api/jobs/{job_id}/print")
    def print_job(job_id: str) -> dict:
        _job(job_id)
        try:
            return svc.jobs.print_job(job_id).model_dump(mode="json")
        except (ValueError, MoonrakerError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/reslice")
    def reslice_job(job_id: str, body: Reslice) -> dict:
        _job(job_id)
        try:
            return svc.jobs.reslice(job_id, body.overrides, body.size_mm, body.slot).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/jobs/{job_id}/files/{name}")
    def job_file(job_id: str, name: str):
        job = _job(job_id)
        if name not in JOB_FILES and not name.startswith("upload."):
            raise HTTPException(404)
        if name not in job.files:
            raise HTTPException(404, "Datei nicht vorhanden")
        path = svc.jobs.jobs_dir / job_id / name
        if not path.is_file():
            raise HTTPException(404)
        media = {"png": "image/png", "stl": "model/stl", "gcode": "text/plain", "scad": "text/plain"}.get(
            name.rsplit(".", 1)[-1], "application/octet-stream")
        return FileResponse(path, media_type=media, filename=f"{job_id}-{name}")

    # --- printer ---------------------------------------------------------------------
    @app.get("/api/printer")
    def printer_status() -> dict:
        return svc.printer_status

    @app.post("/api/printer")
    def printer_action(body: PrinterAction) -> dict:
        m = svc.moonraker
        a = body.action
        simple = {
            "pause": m.pause, "resume": m.resume, "cancel": m.cancel, "estop": m.emergency_stop,
            "firmware_restart": m.firmware_restart,
        }
        scripts = {
            "home": "G28",
            "motors_off": "M84",
            "cooldown": "TURN_OFF_HEATERS\nM107",
            "filter_on": "FILTER_ON",
            "filter_off": "FILTER_OFF",
            "afs_confirm": "AFS_LOAD_CONFIRM",
            "tool_confirm": "TOOL_CONFIRM",
            "bed_mesh": "BED_MESH_CALIBRATE",
            "park": "_ARTIFEX_PARK",
        }
        try:
            if a in simple:
                simple[a]()
            elif a in scripts:
                m.gcode(scripts[a])
            elif a == "preheat":
                mat = MATERIALS.get((body.script or "PLA").upper(), MATERIALS["PLA"])
                m.gcode(f"M140 S{mat['bed_temp']}\nM104 S{mat['nozzle_temp']}")
            elif a == "set_extruder" and body.value is not None:
                m.gcode(f"M104 S{max(0.0, min(body.value, 290.0)):.0f}")
            elif a == "set_bed" and body.value is not None:
                m.gcode(f"M140 S{max(0.0, min(body.value, 110.0)):.0f}")
            elif a == "fan" and body.value is not None:
                m.gcode(f"M106 S{int(max(0.0, min(body.value, 1.0)) * 255)}")
            elif a == "tool_change" and body.script:
                m.gcode(f"TOOL_CHANGE TOOL={_safe_token(body.script)}")
            elif a == "gcode" and body.script:
                m.gcode(body.script[:2000])
            elif a in ("shutdown", "reboot"):
                m.machine(a)
            else:
                raise HTTPException(400, f"Unbekannte Aktion {a}")
        except MoonrakerError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"ok": True}

    # --- AFS -------------------------------------------------------------------------
    @app.get("/api/afs")
    def afs_slots() -> list[dict]:
        return [s.model_dump() for s in svc.afs.list()]

    @app.put("/api/afs/{index}")
    def afs_update(index: int, body: SlotUpdate) -> dict:
        try:
            slot = svc.afs.update(index, body.model_dump(exclude_none=True))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        svc.events.publish({"type": "afs", "slots": [s.model_dump() for s in svc.afs.list()]})
        return slot.model_dump()

    @app.post("/api/afs/{index}/{action}")
    def afs_action(index: int, action: str) -> dict:
        try:
            svc.afs.get(index)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        cmd = {"load": f"AFS_LOAD SLOT={index}", "unload": "AFS_UNLOAD", "select": f"AFS_CHANGE SLOT={index}"}.get(action)
        if cmd is None:
            raise HTTPException(400, "Aktion muss load, unload oder select sein")
        try:
            svc.moonraker.gcode(cmd)
        except MoonrakerError as exc:
            raise HTTPException(502, str(exc)) from exc
        if action in ("load", "unload"):
            svc.afs.update(index, {"loaded": action == "load"})
        return {"ok": True}

    # --- websocket -------------------------------------------------------------------
    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        q: asyncio.Queue = asyncio.Queue()
        svc.events.clients.add(q)
        try:
            await websocket.send_json({"type": "printer", "status": svc.printer_status})
            while True:
                ev = await q.get()
                await websocket.send_json(ev)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            svc.events.clients.discard(q)

    @app.exception_handler(KeyError)
    async def key_error(_req, exc: KeyError):
        return JSONResponse({"detail": f"Nicht gefunden: {exc}"}, status_code=404)

    # --- static UI -------------------------------------------------------------------
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def _safe_token(text: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_]", "", text)[:32]
