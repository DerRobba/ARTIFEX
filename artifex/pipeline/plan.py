"""Data model shared by all pipeline stages."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Mode = Literal["organic", "parametric"]


class DesignPlan(BaseModel):
    """Output of the prompt-refinement/routing stage."""

    object_name: str = "Objekt"
    refined_prompt: str
    mode: Mode
    reasoning: str = ""
    # Largest outer dimension of the finished object in mm.
    size_mm: float = 60.0
    # Optional exact bounding box for parametric parts.
    dimensions_mm: dict[str, float] | None = None
    image_prompt: str | None = None
    scad_spec: str | None = None
    functional: bool = False
    material: str | None = None
    source: Literal["llm", "heuristic", "user"] = "llm"
    # Offline fallback: built-in OpenSCAD template + parameters.
    template: str | None = None
    template_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("size_mm")
    @classmethod
    def _clamp_size(cls, v: float) -> float:
        return float(min(max(v, 5.0), 400.0))


class JobOptions(BaseModel):
    """What the user picked on the display."""

    mode: Literal["auto", "organic", "parametric"] = "auto"
    size_mm: float | None = None
    slot: int = 0
    material: str | None = None
    auto_print: bool | None = None
    seed: int | None = None
    # Overrides for the slicer (layer_height, infill_density, supports, ...).
    slice_overrides: dict[str, Any] = Field(default_factory=dict)
    # Iterative design: feedback on a previous job ("mach es 2 cm höher").
    parent_id: str | None = None
    feedback: str | None = None
