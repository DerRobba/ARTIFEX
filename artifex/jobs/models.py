from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..pipeline.plan import DesignPlan, JobOptions

JobStatus = Literal["queued", "running", "ready", "printing", "done", "failed", "cancelled"]

STAGE_TITLES = {
    "refine": "Prompt verstehen",
    "codegen": "OpenSCAD-Code generieren",
    "image": "Bild generieren (z-image-turbo)",
    "mesh3d": "3D-Modell erzeugen (Hunyuan3D-2.1)",
    "prepare": "Modell reparieren & ausrichten",
    "settings": "Druckeinstellungen wählen",
    "slice": "Slicen",
    "upload": "An Drucker senden",
}


class LogEntry(BaseModel):
    t: float
    msg: str


class StageInfo(BaseModel):
    name: str
    title: str
    state: Literal["pending", "running", "done", "skipped", "failed"] = "pending"
    started: float | None = None
    finished: float | None = None


class Job(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = Field(default_factory=time.time)
    updated: float = Field(default_factory=time.time)
    source: Literal["prompt", "upload"] = "prompt"
    prompt: str = ""
    options: JobOptions = Field(default_factory=JobOptions)
    status: JobStatus = "queued"
    stage: str | None = None
    message: str = "Wartet"
    progress: float = 0.0
    stages: list[StageInfo] = Field(default_factory=list)
    plan: DesignPlan | None = None
    scad_code: str | None = None
    codegen_attempts: int | None = None
    used_template: str | None = None
    mesh_stats: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    settings_reason: str = ""
    settings_source: str = ""
    slice: dict[str, Any] | None = None
    remote_file: str | None = None
    error: str | None = None
    files: list[str] = Field(default_factory=list)
    log: list[LogEntry] = Field(default_factory=list)
    seed: int | None = None

    def add_log(self, msg: str) -> None:
        self.log.append(LogEntry(t=time.time(), msg=msg))
        self.log = self.log[-200:]

    def summary(self) -> dict[str, Any]:
        """Lightweight representation for list views."""
        return {
            "id": self.id,
            "created": self.created,
            "updated": self.updated,
            "source": self.source,
            "prompt": self.prompt,
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "progress": self.progress,
            "mode": self.plan.mode if self.plan else None,
            "object_name": self.plan.object_name if self.plan else None,
            "slice": self.slice,
            "files": self.files,
            "error": self.error,
        }
