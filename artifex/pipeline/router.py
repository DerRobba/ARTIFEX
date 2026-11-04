"""Stage 1: prompt refinement + routing (organic vs. parametric)."""
from __future__ import annotations

import json
import logging
import re

from ..config import ArtifexConfig
from ..llm import LLMClient, LLMError
from . import prompts
from .plan import DesignPlan, JobOptions
from .templates import match_template, template_params

log = logging.getLogger(__name__)

ORGANIC_HINTS = (
    "tier", "animal", "figur", "figure", "statue", "skulptur", "sculpture", "büste", "bust", "kopf", "head",
    "gesicht", "face", "drache", "dragon", "katze", "cat", "hund", "dog", "pferd", "horse", "vogel", "bird",
    "eule", "owl", "fisch", "fish", "frosch", "frog", "hase", "rabbit", "bär", "bear", "elefant", "elephant",
    "dino", "monster", "roboter", "robot", "charakter", "character", "männchen", "pflanze", "plant", "baum",
    "tree", "blume", "flower", "frucht", "fruit", "pilz", "mushroom", "schildkröte", "turtle", "einhorn",
    "unicorn", "ritter", "knight", "astronaut", "totenkopf", "skull", "maskottchen", "mascot",
)

_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(mm|cm|m)\b", re.I)
_DIMS_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:mm|cm)?\s*[x×*]\s*(\d+(?:[.,]\d+)?)\s*(?:mm|cm)?"
    r"(?:\s*[x×*]\s*(\d+(?:[.,]\d+)?))?\s*(mm|cm)?",
    re.I,
)
_UNIT = {"mm": 1.0, "cm": 10.0, "m": 1000.0}


def parse_dims_mm(text: str) -> dict[str, float] | None:
    """"80 x 50 x 30 mm" -> {"x": 80, "y": 50, "z": 30} (unit defaults to mm)."""
    m = _DIMS_RE.search(text)
    if not m:
        return None
    f = _UNIT[(m.group(4) or "mm").lower()]
    vals = [float(g.replace(",", ".")) * f for g in m.group(1, 2, 3) if g]
    return dict(zip("xyz", vals))


def parse_size_mm(text: str) -> float | None:
    """Largest explicit size mentioned in the prompt ("10 cm hoch" -> 100)."""
    best = None
    dims = parse_dims_mm(text)
    if dims:
        best = max(dims.values())
    for num, unit in _SIZE_RE.findall(text):
        v = float(num.replace(",", ".")) * _UNIT[unit.lower()]
        best = v if best is None else max(best, v)
    return best


def heuristic_plan(prompt: str, opts: JobOptions, cfg: ArtifexConfig) -> DesignPlan:
    """Rule-based planner used when the LLM is unavailable."""
    size = opts.size_mm or parse_size_mm(prompt)
    template = match_template(prompt)
    p = prompt.lower()
    organic_score = sum(1 for k in ORGANIC_HINTS if k in p)
    if opts.mode == "parametric" or (opts.mode == "auto" and template is not None and organic_score == 0):
        mode = "parametric"
    elif opts.mode == "organic" or organic_score > 0:
        mode = "organic"
    else:
        mode = "parametric" if template else "organic"

    if mode == "parametric":
        size = size or (template.default_size if template else cfg.default_size_mm)
        params = template_params(template, size, prompt) if template else {}
        dims = parse_dims_mm(prompt)
        if template and template.name == "box" and dims and len(dims) == 3 and not opts.size_mm:
            params.update(width=dims["x"], depth=dims["y"], height=dims["z"])
        return DesignPlan(
            object_name=template.title if template else prompt[:40],
            refined_prompt=prompt,
            mode="parametric",
            reasoning="Offline-Regeln: technisches Objekt erkannt" + (f" (Vorlage '{template.title}')" if template else ""),
            size_mm=size,
            scad_spec=prompt,
            functional=True,
            source="heuristic",
            template=template.name if template else None,
            template_params=params,
            dimensions_mm=dims,
        )
    size = size or cfg.default_size_mm
    return DesignPlan(
        object_name=prompt[:40],
        refined_prompt=prompt,
        mode="organic",
        reasoning="Offline-Regeln: Freiform-Objekt erkannt",
        size_mm=size,
        image_prompt=(
            f"{prompt}, single object, centered, full view, three-quarter front view, plain white background, "
            "soft even studio lighting, no shadows, matte grey clay material, chunky proportions, stable flat base"
        ),
        source="heuristic",
    )


class Router:
    def __init__(self, cfg: ArtifexConfig, llm: LLMClient):
        self.cfg = cfg
        self.llm = llm

    def _system(self) -> str:
        p = self.cfg.printer
        return prompts.ROUTER_SYSTEM.format(
            bed_x=p.bed_x, bed_y=p.bed_y, max_z=p.max_z, default_size=int(self.cfg.default_size_mm)
        )

    def plan(self, prompt: str, opts: JobOptions, previous: DesignPlan | None = None) -> DesignPlan:
        plan: DesignPlan | None = None
        if self.llm.available:
            try:
                if previous is not None and opts.feedback:
                    user = prompts.ROUTER_FEEDBACK.format(
                        previous=previous.model_dump_json(exclude={"source", "template", "template_params"}),
                        feedback=opts.feedback,
                    )
                else:
                    user = prompt
                    if opts.size_mm:
                        user += f"\n(Gewünschte Größe: größte Abmessung ca. {opts.size_mm:.0f} mm)"
                    if opts.mode != "auto":
                        user += f"\n(Der Nutzer verlangt den Modus: {opts.mode})"
                data = self.llm.chat_json(self._system(), user, task="router")
                plan = self._from_llm(data, prompt)
            except (LLMError, ValueError) as exc:
                log.warning("LLM routing failed, using heuristics: %s", exc)
        if plan is None:
            text = prompt if not opts.feedback else f"{prompt}. {opts.feedback}"
            plan = heuristic_plan(text, opts, self.cfg)

        # User choices always win over the model.
        if opts.mode != "auto" and plan.mode != opts.mode:
            plan.mode = opts.mode
            if opts.mode == "organic" and not plan.image_prompt:
                plan.image_prompt = heuristic_plan(prompt, opts, self.cfg).image_prompt
            if opts.mode == "parametric" and not plan.scad_spec:
                plan.scad_spec = plan.refined_prompt
        if opts.size_mm:
            plan.size_mm = opts.size_mm
        if opts.material:
            plan.material = opts.material
        # Keep an offline template as safety net for parametric parts.
        if plan.mode == "parametric" and plan.template is None:
            t = match_template(prompt)
            if t is not None:
                plan.template = t.name
                plan.template_params = template_params(t, plan.size_mm, prompt)
        return self._fit(plan)

    def _from_llm(self, data: dict, prompt: str) -> DesignPlan:
        mode = str(data.get("mode", "")).strip().lower()
        if mode not in ("organic", "parametric"):
            raise ValueError(f"invalid mode {mode!r}")
        dims = data.get("dimensions_mm")
        if isinstance(dims, dict):
            try:
                dims = {k: float(v) for k, v in dims.items() if k in ("x", "y", "z") and v is not None}
            except (TypeError, ValueError):
                dims = None
        else:
            dims = None
        size = data.get("size_mm")
        try:
            size = float(size) if size is not None else None
        except (TypeError, ValueError):
            size = None
        if not size and dims:
            size = max(dims.values())
        return DesignPlan(
            object_name=str(data.get("object_name") or prompt[:40]),
            refined_prompt=str(data.get("refined_prompt") or prompt),
            mode=mode,  # type: ignore[arg-type]
            reasoning=str(data.get("reasoning") or ""),
            size_mm=size or self.cfg.default_size_mm,
            dimensions_mm=dims or None,
            image_prompt=data.get("image_prompt") or None,
            scad_spec=data.get("scad_spec") or None,
            functional=bool(data.get("functional", False)),
            material=(str(data["material"]).upper() if data.get("material") else None),
            source="llm",
        )

    def _fit(self, plan: DesignPlan) -> DesignPlan:
        """Make sure the requested size fits into the build volume."""
        p = self.cfg.printer
        max_xy = min(p.bed_x, p.bed_y) - 2 * p.margin
        limit = min(max_xy, p.max_z)
        if plan.size_mm > limit:
            plan.reasoning += f" (Größe auf {limit:.0f} mm begrenzt)"
            plan.size_mm = limit
        return plan


def describe(plan: DesignPlan) -> str:
    return json.dumps(plan.model_dump(), ensure_ascii=False, indent=2)
