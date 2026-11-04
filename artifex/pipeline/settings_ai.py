"""AI-assisted print settings.

A deterministic rule set produces a solid baseline from the mesh analysis. If an LLM is
available it may refine the values; everything it returns is clamped to safe limits.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..config import ArtifexConfig
from ..llm import LLMClient, LLMError
from ..slicer.settings import SliceSettings, clamp
from . import prompts
from .mesh import MeshStats
from .plan import DesignPlan

log = logging.getLogger(__name__)

AI_KEYS = ("layer_height", "perimeters", "top_layers", "bottom_layers", "infill_density",
           "infill_pattern", "supports", "brim_width", "speed_factor")


def rule_based(plan: DesignPlan, stats: MeshStats, base: SliceSettings) -> tuple[dict[str, Any], list[str]]:
    v: dict[str, Any] = {}
    why: list[str] = []
    organic = plan.mode == "organic"
    small = max(stats.size_x, stats.size_y, stats.size_z) < 40

    if organic:
        v.update(layer_height=0.12 if small else 0.16, perimeters=2, infill_density=0.10, infill_pattern="grid")
        why.append("Organische Form: feine Schichten für glatte Rundungen, wenig Füllung.")
    elif plan.functional:
        v.update(layer_height=0.2, perimeters=4, infill_density=0.30, infill_pattern="triangles",
                 top_layers=5, bottom_layers=4)
        why.append("Funktionsteil: 4 Wände und 30 % Dreiecksfüllung für Stabilität.")
    else:
        v.update(layer_height=0.2, perimeters=3, infill_density=0.15, infill_pattern="grid")
        why.append("Standardteil: 0,2 mm Schichten, 3 Wände, 15 % Füllung.")

    if stats.overhang_ratio > 0.04:
        v["supports"] = True
        why.append(f"{stats.overhang_ratio * 100:.0f} % Überhangfläche: Stützstrukturen an.")
    footprint = stats.size_x * stats.size_y
    if stats.slenderness > 2.5 or stats.base_area_mm2 < 0.08 * footprint:
        v["brim_width"] = 5.0
        why.append("Schmale Standfläche bzw. hohes Teil: 5 mm Brim gegen Ablösen.")
    if stats.size_z > 120:
        v["speed_factor"] = 0.9
    if small:
        v["speed_factor"] = min(v.get("speed_factor", 1.0), 0.8)
    return v, why


class SettingsAdvisor:
    def __init__(self, cfg: ArtifexConfig, llm: LLMClient):
        self.cfg = cfg
        self.llm = llm

    def choose(
        self,
        plan: DesignPlan,
        stats: MeshStats,
        material: str,
        slot: int,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[SliceSettings, str, str]:
        p = self.cfg.printer
        base = SliceSettings.for_material(
            material, afs_slot=slot, nozzle_diameter=p.nozzle_diameter,
            filament_diameter=p.filament_diameter, bed_x=p.bed_x, bed_y=p.bed_y,
        )
        values, why = rule_based(plan, stats, base)
        source = "rules"

        if self.cfg.slicer.ai_settings and self.llm.available:
            try:
                user = prompts.SETTINGS_USER.format(
                    name=plan.object_name, mode=plan.mode, functional=plan.functional,
                    prompt=plan.refined_prompt, material=base.material,
                    stats=json.dumps(stats.to_dict(), ensure_ascii=False),
                )
                data = self.llm.chat_json(prompts.SETTINGS_SYSTEM, user, task="settings")
                ai = sanitize_ai_settings(data)
                if ai:
                    values.update(ai)
                    reason = str(data.get("reasoning") or "").strip()
                    why = [reason] if reason else why
                    source = "ai"
            except LLMError as exc:
                log.warning("AI settings unavailable, using rules: %s", exc)

        settings = base.with_overrides(values)
        if overrides:
            settings = settings.with_overrides(overrides)
            why.append("Manuelle Anpassungen übernommen.")
        # First layer height must not be smaller than a sensible minimum.
        settings.first_layer_height = max(settings.first_layer_height, min(0.24, settings.layer_height * 1.5))
        return settings, " ".join(why), source


def sanitize_ai_settings(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in AI_KEYS:
        if key not in data or data[key] is None:
            continue
        val = data[key]
        if key == "supports":
            out[key] = bool(val) if not isinstance(val, str) else val.strip().lower() in ("true", "yes", "1", "ja")
        elif key == "infill_pattern":
            if str(val) in ("grid", "lines", "triangles"):
                out[key] = str(val)
        elif key == "infill_density":
            try:
                f = float(str(val).rstrip("%"))
            except ValueError:
                continue
            out[key] = clamp(key, f / 100 if f > 1 else f)
        else:
            try:
                out[key] = clamp(key, float(val))
            except (TypeError, ValueError):
                continue
    return out
