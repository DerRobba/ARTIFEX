"""Parametric path: AI writes OpenSCAD code, OpenSCAD renders it to STL.

Compile errors and geometry problems are fed back to the model for a bounded number of
repair rounds. If the model fails (or is offline) a matching built-in template is used.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import trimesh

from ..config import ArtifexConfig
from ..llm import LLMClient, LLMError, extract_code
from . import prompts
from .plan import DesignPlan
from .templates import TEMPLATES, scad_value

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, float], None]


class ParametricError(RuntimeError):
    pass


@dataclass
class ParametricResult:
    scad_code: str
    stl_path: Path
    attempts: int
    used_template: str | None = None


_FORBIDDEN = [
    (re.compile(r"\bimport\s*\("), "import() is not allowed"),
    (re.compile(r"\bsurface\s*\("), "surface() is not allowed"),
]
_INCLUDE_RE = re.compile(r"^\s*(include|use)\s*<([^>]*)>", re.M)


def check_code_safety(code: str, allowed_libraries: list[str]) -> list[str]:
    """Return a list of problems; empty list = OK. OpenSCAD can read local files, so the
    generated code must not reference anything except whitelisted libraries."""
    problems = [msg for rx, msg in _FORBIDDEN if rx.search(code)]
    for m in _INCLUDE_RE.finditer(code):
        path = m.group(2)
        if ".." in path or path.startswith("/") or not any(path.startswith(lib) for lib in allowed_libraries):
            problems.append(f"{m.group(1)} <{path}> is not allowed")
    return problems


def openscad_version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        m = re.search(r"(\d{4}\.\d{2}(?:\.\d+)?)", out.stdout + out.stderr)
        return m.group(1) if m else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def render_scad(
    cfg: ArtifexConfig,
    scad_path: Path,
    stl_path: Path,
    defines: dict[str, object] | None = None,
) -> str:
    """Render a .scad file to binary STL. Returns OpenSCAD's log; raises on failure."""
    cmd = [cfg.openscad.binary, "-o", str(stl_path), "--export-format", "binstl", *cfg.openscad.extra_args]
    for k, v in (defines or {}).items():
        cmd += ["-D", f"{k}={scad_value(v)}"]
    cmd.append(str(scad_path))
    stl_path.unlink(missing_ok=True)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=cfg.openscad.timeout_s, cwd=scad_path.parent
        )
    except subprocess.TimeoutExpired as exc:
        raise ParametricError(
            f"OpenSCAD rendering took longer than {cfg.openscad.timeout_s:.0f} s - simplify the model "
            "(fewer facets, no minkowski, fewer loop iterations)"
        ) from exc
    except FileNotFoundError as exc:
        raise ParametricError(f"OpenSCAD binary '{cfg.openscad.binary}' not found") from exc
    log_text = (proc.stdout + proc.stderr).strip()
    errors = [ln for ln in log_text.splitlines() if re.search(r"\b(ERROR|error)\b", ln)]
    if proc.returncode != 0 or errors or not stl_path.exists() or stl_path.stat().st_size < 100:
        detail = "\n".join(errors[-8:]) or log_text[-1500:] or "no output"
        if "Current top level object is empty" in log_text:
            detail += "\nThe top level object is empty - nothing would be printed."
        raise ParametricError(detail)
    return log_text


def check_geometry(stl_path: Path, cfg: ArtifexConfig) -> list[str]:
    """Geometry sanity checks whose results are fed back to the model."""
    problems: list[str] = []
    try:
        mesh = trimesh.load_mesh(stl_path, force="mesh")
    except Exception as exc:  # noqa: BLE001 - any loader error is a model problem
        return [f"The exported STL could not be loaded: {exc}"]
    if mesh.is_empty or len(mesh.faces) < 4:
        return ["The model is empty."]
    ext = mesh.extents
    p = cfg.printer
    if ext[0] > p.bed_x - 2 * p.margin or ext[1] > p.bed_y - 2 * p.margin or ext[2] > p.max_z:
        problems.append(
            f"The model is {ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f} mm and does not fit the build volume "
            f"{p.bed_x - 2 * p.margin:.0f} x {p.bed_y - 2 * p.margin:.0f} x {p.max_z:.0f} mm."
        )
    if max(ext) < 2:
        problems.append(f"The model is only {max(ext):.2f} mm large - check the units (millimetres).")
    if mesh.bounds[0][2] < -0.05:
        problems.append(f"Parts of the model are below z = 0 (min z = {mesh.bounds[0][2]:.2f}).")
    return problems


class ParametricGenerator:
    def __init__(self, cfg: ArtifexConfig, llm: LLMClient):
        self.cfg = cfg
        self.llm = llm
        self._version: str | None = None

    @property
    def version(self) -> str:
        if self._version is None:
            self._version = openscad_version(self.cfg.openscad.binary)
        return self._version

    def _system(self) -> str:
        libs = self.cfg.openscad.allowed_libraries
        p = self.cfg.printer
        return prompts.CODEGEN_SYSTEM.format(
            bed_x=p.bed_x, bed_y=p.bed_y, max_z=p.max_z, version=self.version,
            libraries=(" plus these libraries: " + ", ".join(libs)) if libs else "",
        )

    def generate(
        self,
        plan: DesignPlan,
        workdir: Path,
        progress: ProgressFn | None = None,
        previous_code: str | None = None,
        feedback: str | None = None,
    ) -> ParametricResult:
        progress = progress or (lambda msg, frac: None)
        workdir.mkdir(parents=True, exist_ok=True)
        stl_path = workdir / "model.stl"
        llm_error: str | None = None

        if self.llm.available:
            try:
                return self._generate_llm(plan, workdir, stl_path, progress, previous_code, feedback)
            except (LLMError, ParametricError) as exc:
                llm_error = str(exc)
                log.warning("AI OpenSCAD generation failed: %s", exc)

        if plan.template and plan.template in TEMPLATES:
            progress(f"Nutze Vorlage '{TEMPLATES[plan.template].title}'", 0.8)
            return self._render_template(plan, workdir, stl_path)
        raise ParametricError(llm_error or "Kein KI-Modell erreichbar und keine passende Vorlage gefunden")

    def _generate_llm(self, plan, workdir, stl_path, progress, previous_code, feedback) -> ParametricResult:
        dims = ""
        if plan.dimensions_mm:
            dims = " (bounding box " + " x ".join(f"{k}={v:g}" for k, v in plan.dimensions_mm.items()) + " mm)"
        user = prompts.CODEGEN_USER.format(
            name=plan.object_name, prompt=plan.refined_prompt, spec=plan.scad_spec or plan.refined_prompt,
            size=f"{plan.size_mm:g}", dims=dims,
        )
        messages = [{"role": "system", "content": self._system()}, {"role": "user", "content": user}]
        if previous_code and feedback:
            messages.append({"role": "user", "content": prompts.CODEGEN_MODIFY.format(code=previous_code, feedback=feedback)})

        attempts = self.cfg.openscad.max_attempts
        last_error = ""
        for attempt in range(1, attempts + 1):
            progress(f"KI schreibt OpenSCAD-Code (Versuch {attempt}/{attempts})", 0.1 + 0.8 * (attempt - 1) / attempts)
            answer = self.llm.chat(messages, task="codegen")
            code = extract_code(answer)
            messages.append({"role": "assistant", "content": f"```openscad\n{code}\n```"})
            scad_path = workdir / "model.scad"
            scad_path.write_text(code, encoding="utf-8")

            problems = check_code_safety(code, self.cfg.openscad.allowed_libraries)
            if not problems:
                progress(f"OpenSCAD rendert (Versuch {attempt}/{attempts})", 0.15 + 0.8 * (attempt - 1) / attempts)
                try:
                    render_scad(self.cfg, scad_path, stl_path)
                    problems = check_geometry(stl_path, self.cfg)
                except ParametricError as exc:
                    problems = [str(exc)]
            if not problems:
                return ParametricResult(scad_code=code, stl_path=stl_path, attempts=attempt)
            last_error = "\n".join(problems)
            log.info("OpenSCAD attempt %d failed: %s", attempt, last_error)
            messages.append({"role": "user", "content": prompts.CODEGEN_FIX.format(error=last_error[:2000])})
        raise ParametricError(f"OpenSCAD-Code nach {attempts} Versuchen fehlerhaft: {last_error[:500]}")

    def _render_template(self, plan: DesignPlan, workdir: Path, stl_path: Path) -> ParametricResult:
        t = TEMPLATES[plan.template]  # type: ignore[index]
        src = t.path.read_text(encoding="utf-8")
        # Bake parameters into the saved copy so the user sees the actual values.
        lines = []
        for line in src.splitlines():
            m = re.match(r"^(\w+)\s*=", line)
            if m and m.group(1) in plan.template_params:
                line = f"{m.group(1)} = {scad_value(plan.template_params[m.group(1)])};"
            lines.append(line)
        code = "\n".join(lines) + "\n"
        scad_path = workdir / "model.scad"
        scad_path.write_text(code, encoding="utf-8")
        render_scad(self.cfg, scad_path, stl_path)
        return ParametricResult(scad_code=code, stl_path=stl_path, attempts=1, used_template=t.name)
