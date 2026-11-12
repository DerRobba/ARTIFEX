"""Job orchestration: runs the AI pipeline stage by stage in a worker thread."""
from __future__ import annotations

import logging
import queue
import random
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..config import ArtifexConfig
from ..pipeline.mesh import MeshError, prepare_mesh
from ..pipeline.organic import OrganicError, OrganicGenerator
from ..pipeline.parametric import ParametricError, ParametricGenerator
from ..pipeline.plan import DesignPlan, JobOptions
from ..pipeline.router import Router
from ..pipeline.settings_ai import SettingsAdvisor
from ..printer.afs import AFSStore
from ..printer.moonraker import Moonraker, MoonrakerError
from ..slicer import ArtifexSlicer, SliceError, SliceSettings
from ..slicer.orca import OrcaSlicer
from .models import STAGE_TITLES, Job, StageInfo
from .store import JobStore

log = logging.getLogger(__name__)

PIPELINE_ERRORS = (ParametricError, OrganicError, MeshError, SliceError, MoonrakerError, ValueError, OSError)


class Cancelled(Exception):
    pass


def stages_for(job: Job, mode: str | None) -> list[str]:
    if job.source == "upload":
        return ["prepare", "settings", "slice"]
    middle = ["codegen"] if mode == "parametric" else ["image", "mesh3d"] if mode == "organic" else []
    return ["refine", *middle, "prepare", "settings", "slice"]


def slugify(text: str) -> str:
    text = text.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40] or "objekt"


class JobManager:
    def __init__(
        self,
        cfg: ArtifexConfig,
        store: JobStore,
        router: Router,
        parametric: ParametricGenerator,
        organic: OrganicGenerator,
        advisor: SettingsAdvisor,
        moonraker: Moonraker,
        afs: AFSStore,
        publish: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.router = router
        self.parametric = parametric
        self.organic = organic
        self.advisor = advisor
        self.moonraker = moonraker
        self.afs = afs
        self.publish = publish or (lambda ev: None)
        self.jobs_dir = cfg.data_path / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._queue: "queue.Queue[tuple[str, str | None]]" = queue.Queue()
        self._cancel: set[str] = set()
        self._current: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        for job in self.store.by_status("running"):
            job.status = "failed"
            job.error = "Abgebrochen durch Neustart"
            self.store.save(job)
        for job in self.store.by_status("queued"):
            self._queue.put((job.id, None))
        self._thread = threading.Thread(target=self._worker, name="artifex-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(("", None))

    def job_dir(self, job_id: str) -> Path:
        d = self.jobs_dir / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- public API ------------------------------------------------------------------
    def submit_prompt(self, prompt: str, options: JobOptions) -> Job:
        prompt = prompt.strip()
        if not prompt and not options.feedback:
            raise ValueError("Bitte beschreibe das Objekt")
        if options.parent_id and not prompt:
            parent = self.store.get(options.parent_id)
            prompt = parent.prompt if parent else ""
        job = Job(source="prompt", prompt=prompt[:1000], options=options)
        job.seed = options.seed if options.seed is not None else random.randint(0, 2**31 - 1)
        job.stages = [StageInfo(name=s, title=STAGE_TITLES[s]) for s in stages_for(job, None if options.mode == "auto" else options.mode)]
        self._save(job)
        self._queue.put((job.id, None))
        return job

    def submit_upload(self, filename: str, data: bytes, options: JobOptions) -> Job:
        suffix = Path(filename).suffix.lower()
        if suffix not in (".stl", ".obj", ".3mf", ".glb", ".ply"):
            raise ValueError("Nur STL, OBJ, 3MF, GLB oder PLY werden unterstützt")
        job = Job(source="upload", prompt=Path(filename).stem, options=options)
        job.plan = DesignPlan(
            object_name=Path(filename).stem, refined_prompt=Path(filename).stem, mode="parametric",
            reasoning="Hochgeladene Datei", size_mm=options.size_mm or self.cfg.default_size_mm, source="user",
        )
        job.stages = [StageInfo(name=s, title=STAGE_TITLES[s]) for s in stages_for(job, None)]
        d = self.job_dir(job.id)
        (d / f"upload{suffix}").write_bytes(data)
        job.files.append(f"upload{suffix}")
        self._save(job)
        self._queue.put((job.id, None))
        return job

    def reslice(self, job_id: str, overrides: dict[str, Any], size_mm: float | None, slot: int | None) -> Job:
        job = self._require(job_id)
        if job.status in ("running", "queued", "printing"):
            raise ValueError("Auftrag ist gerade aktiv")
        if not self._source_model(job):
            raise ValueError("Kein Modell vorhanden")
        job.options.slice_overrides.update({k: v for k, v in overrides.items() if v is not None})
        if size_mm:
            job.options.size_mm = size_mm
        if slot is not None:
            job.options.slot = slot
        job.status = "queued"
        job.error = None
        for st in job.stages:
            if st.name in ("prepare", "settings", "slice"):
                st.state, st.started, st.finished = "pending", None, None
        self._save(job)
        self._queue.put((job.id, "prepare"))
        return job

    def cancel(self, job_id: str) -> Job:
        job = self._require(job_id)
        if job.status == "queued":
            job.status = "cancelled"
            job.message = "Abgebrochen"
            self._save(job)
        elif job.status == "running":
            self._cancel.add(job_id)
        return job

    def delete(self, job_id: str) -> None:
        job = self._require(job_id)
        if job.status in ("running", "printing"):
            raise ValueError("Laufende Aufträge können nicht gelöscht werden")
        self.store.delete(job_id)
        shutil.rmtree(self.jobs_dir / job_id, ignore_errors=True)
        self.publish({"type": "job_deleted", "id": job_id})

    def print_job(self, job_id: str) -> Job:
        job = self._require(job_id)
        if job.status not in ("ready", "done", "failed", "cancelled") or "print.gcode" not in job.files:
            raise ValueError("Auftrag hat noch keinen fertigen G-Code")
        st = self.moonraker.status()
        if not st.get("connected") or st.get("klippy") != "ready":
            raise MoonrakerError("Drucker ist nicht bereit (Klipper/Moonraker prüfen)")
        if st.get("state") in ("printing", "paused"):
            raise MoonrakerError("Der Drucker druckt bereits")
        name = f"{slugify(job.plan.object_name if job.plan else job.prompt)}-{job.id[:6]}.gcode"
        remote = self.moonraker.upload(self.job_dir(job.id) / "print.gcode", name, start=False)
        self.moonraker.start_print(remote)
        job.remote_file = remote
        job.status = "printing"
        job.message = "Druckt"
        job.error = None
        job.add_log(f"An Drucker gesendet: {remote}")
        self._save(job)
        return job

    def on_printer_status(self, st: dict[str, Any]) -> None:
        """Called by the status poller - keeps job state in sync with Klipper."""
        for job in self.store.by_status("printing"):
            state = st.get("state")
            if not job.remote_file or st.get("filename") not in (job.remote_file, job.remote_file.split("/")[-1]):
                # Klipper restarted or the file was replaced: the print is gone. Grace
                # period because Moonraker needs a moment to switch to "printing".
                if st.get("klippy") == "ready" and state in ("standby", None) and time.time() - job.updated > 30:
                    job.status = "failed"
                    job.error = "Druck wurde unterbrochen (Klipper-Neustart?)"
                    job.message = "Unterbrochen"
                    self._save(job)
                continue
            new = {"complete": "done", "cancelled": "cancelled", "error": "failed"}.get(state or "")
            if new:
                job.status = new  # type: ignore[assignment]
                job.message = {"done": "Druck fertig", "cancelled": "Druck abgebrochen", "failed": "Druckfehler"}[new]
                if new == "failed":
                    job.error = st.get("message") or "Klipper-Fehler"
                job.progress = 1.0 if new == "done" else job.progress
                self._save(job)

    # -- worker ----------------------------------------------------------------------
    def _require(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def _save(self, job: Job) -> None:
        self.store.save(job)
        self.publish({"type": "job", "job": job.model_dump(mode="json")})

    def _worker(self) -> None:
        while not self._stop.is_set():
            job_id, from_stage = self._queue.get()
            if not job_id:
                continue
            job = self.store.get(job_id)
            if job is None or job.status == "cancelled":
                continue
            self._current = job_id
            try:
                self.run(job, from_stage)
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                log.exception("Job %s crashed", job_id)
                job.status = "failed"
                job.error = f"Interner Fehler: {exc}"
                self._save(job)
            finally:
                self._current = None
                self._cancel.discard(job_id)

    def _stage(self, job: Job, name: str) -> StageInfo:
        for st in job.stages:
            if st.name == name:
                return st
        st = StageInfo(name=name, title=STAGE_TITLES[name])
        job.stages.append(st)
        return st

    def _progress_fn(self, job: Job, stage_index: int, total: int):
        last = [0.0]

        def fn(msg: str, frac: float) -> None:
            if job.id in self._cancel:
                raise Cancelled()
            job.message = msg
            job.progress = min(0.999, (stage_index + max(0.0, min(frac, 1.0))) / total)
            now = time.monotonic()
            if now - last[0] > 0.4:
                last[0] = now
                self._save(job)

        return fn

    def run(self, job: Job, from_stage: str | None = None) -> None:
        job.status = "running"
        job.error = None
        self._save(job)
        d = self.job_dir(job.id)
        try:
            if from_stage is None and job.source == "prompt":
                self._run_stage(job, "refine", lambda p: self._do_refine(job, p))
                # Now we know the mode -> fix the stage list.
                done = {s.name: s for s in job.stages}
                job.stages = [done.get(n) or StageInfo(name=n, title=STAGE_TITLES[n]) for n in stages_for(job, job.plan.mode)]
                if job.plan.mode == "parametric":
                    self._run_stage(job, "codegen", lambda p: self._do_codegen(job, d, p))
                else:
                    self._run_stage(job, "image", lambda p: self._do_image(job, d, p))
                    self._run_stage(job, "mesh3d", lambda p: self._do_mesh3d(job, d, p))
            self._run_stage(job, "prepare", lambda p: self._do_prepare(job, d, p))
            self._run_stage(job, "settings", lambda p: self._do_settings(job, p))
            self._run_stage(job, "slice", lambda p: self._do_slice(job, d, p))
        except Cancelled:
            job.status = "cancelled"
            job.message = "Abgebrochen"
            job.add_log("Vom Nutzer abgebrochen")
            self._save(job)
            return
        except PIPELINE_ERRORS as exc:
            job.status = "failed"
            job.error = str(exc)
            job.message = "Fehlgeschlagen"
            job.add_log(f"Fehler: {exc}")
            self._save(job)
            return

        job.status = "ready"
        job.progress = 1.0
        job.message = "Bereit zum Drucken"
        self._save(job)
        auto = job.options.auto_print if job.options.auto_print is not None else self.cfg.auto_print
        if auto:
            try:
                self.print_job(job.id)
            except (MoonrakerError, ValueError) as exc:
                job = self._require(job.id)
                job.add_log(f"Automatischer Druckstart fehlgeschlagen: {exc}")
                self._save(job)

    def _run_stage(self, job: Job, name: str, fn: Callable[[Callable[[str, float], None]], None]) -> None:
        if job.id in self._cancel:
            raise Cancelled()
        st = self._stage(job, name)
        idx = [s.name for s in job.stages].index(name)
        job.stage = name
        st.state, st.started, st.finished = "running", time.time(), None
        job.message = st.title
        job.progress = idx / len(job.stages)
        self._save(job)
        try:
            fn(self._progress_fn(job, idx, len(job.stages)))
        except BaseException:
            st.state = "failed"
            st.finished = time.time()
            raise
        st.state = "done"
        st.finished = time.time()
        job.add_log(f"{st.title}: fertig ({st.finished - st.started:.1f} s)")
        self._save(job)

    # -- stages ----------------------------------------------------------------------
    def _parent(self, job: Job) -> Job | None:
        return self.store.get(job.options.parent_id) if job.options.parent_id else None

    def _do_refine(self, job: Job, progress) -> None:
        progress("KI analysiert die Beschreibung", 0.2)
        parent = self._parent(job)
        job.plan = self.router.plan(job.prompt, job.options, previous=parent.plan if parent else None)
        job.add_log(f"Modus: {job.plan.mode} ({job.plan.source}) - {job.plan.reasoning}")

    def _do_codegen(self, job: Job, d: Path, progress) -> None:
        parent = self._parent(job)
        res = self.parametric.generate(
            job.plan, d, progress,
            previous_code=parent.scad_code if parent else None,
            feedback=job.options.feedback,
        )
        job.scad_code = res.scad_code
        job.codegen_attempts = res.attempts
        job.used_template = res.used_template
        shutil.copyfile(res.stl_path, d / "source.stl")
        _add_files(job, "model.scad", "source.stl")

    def _do_image(self, job: Job, d: Path, progress) -> None:
        progress("z-image-turbo malt das Objekt", 0.1)
        self.organic.generate_image(job.plan, d, seed=job.seed)
        _add_files(job, "image.png")

    def _do_mesh3d(self, job: Job, d: Path, progress) -> None:
        progress("Hunyuan3D-2.1 rekonstruiert das 3D-Modell", 0.1)
        raw = self.organic.image_to_mesh(d / "image.png", d, seed=job.seed)
        shutil.copyfile(raw, d / "source.stl")
        _add_files(job, "source.stl")

    def _source_model(self, job: Job) -> Path | None:
        d = self.jobs_dir / job.id
        if (d / "source.stl").exists():
            return d / "source.stl"
        for f in job.files:
            if f.startswith("upload"):
                return d / f
        return None

    def _do_prepare(self, job: Job, d: Path, progress) -> None:
        src = self._source_model(job)
        if src is None:
            raise MeshError("Kein Quellmodell vorhanden")
        progress("Repariere und richte das Modell aus", 0.2)
        organic = bool(job.plan and job.plan.mode == "organic")
        # Parametric parts and uploads keep their exact dimensions unless the user asks
        # for a size; generated organic meshes have no real scale and get the planned one.
        size = job.options.size_mm
        if organic:
            size = job.options.size_mm or job.plan.size_mm
        stats = prepare_mesh(src, d / "print.stl", self.cfg, size_mm=size, organic=organic)
        job.mesh_stats = stats.to_dict()
        _add_files(job, "print.stl")
        for note in stats.repaired:
            job.add_log(note)

    def _do_settings(self, job: Job, progress) -> None:
        progress("Wähle Druckeinstellungen", 0.3)
        from ..pipeline.mesh import MeshStats

        stats = MeshStats(**job.mesh_stats)  # type: ignore[arg-type]
        slot_idx = job.options.slot
        try:
            slot = self.afs.get(slot_idx)
            material = job.options.material or slot.material
        except KeyError:
            material = job.options.material or (job.plan.material if job.plan else None) or "PLA"
            slot_idx = 0
        settings, reason, source = self.advisor.choose(job.plan, stats, material, slot_idx, job.options.slice_overrides)
        job.settings = settings.model_dump()
        job.settings_reason = reason
        job.settings_source = source

    def _do_slice(self, job: Job, d: Path, progress) -> None:
        settings = SliceSettings.model_validate(job.settings)
        backend = self.cfg.slicer.backend
        slicer: Any = ArtifexSlicer()
        if backend == "orca":
            slicer = OrcaSlicer(self.cfg.slicer)
        result = slicer.slice(d / "print.stl", d / "print.gcode", settings, progress)
        job.slice = result.to_dict()
        _add_files(job, "print.gcode")
        for w in result.warnings:
            job.add_log(f"Slicer: {w}")


def _add_files(job: Job, *names: str) -> None:
    for n in names:
        if n not in job.files:
            job.files.append(n)
