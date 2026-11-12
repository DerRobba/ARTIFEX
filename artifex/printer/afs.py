"""ARTIFEX Filament System (AFS) slot bookkeeping.

Klipper only knows slot numbers; what is *in* each slot (material, colour, name) is kept
here so the AI settings and the display can use it.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from pydantic import BaseModel, Field

from ..slicer.settings import MATERIALS


class Slot(BaseModel):
    index: int
    material: str = "PLA"
    color: str = "#d9d9d9"
    name: str = ""
    loaded: bool = False
    remaining_g: float | None = None


class AFSState(BaseModel):
    slots: list[Slot] = Field(default_factory=list)


DEFAULT_COLORS = ["#f2f2f2", "#222222", "#d63c3c", "#2f6fd6", "#2fa84f", "#f2c12e", "#f08a24", "#8e44ad"]


class AFSStore:
    def __init__(self, path: Path, slots: int):
        self.path = path
        self._lock = threading.Lock()
        self.state = self._load(slots)

    def _load(self, n: int) -> AFSState:
        if self.path.exists():
            try:
                state = AFSState.model_validate_json(self.path.read_text(encoding="utf-8"))
            except ValueError:
                state = AFSState()
        else:
            state = AFSState()
        existing = {s.index: s for s in state.slots}
        state.slots = [existing.get(i) or Slot(index=i, color=DEFAULT_COLORS[i % len(DEFAULT_COLORS)]) for i in range(n)]
        return state

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(self.state.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def list(self) -> list[Slot]:
        return list(self.state.slots)

    def get(self, index: int) -> Slot:
        if not 0 <= index < len(self.state.slots):
            raise KeyError(f"AFS-Slot {index} existiert nicht")
        return self.state.slots[index]

    def update(self, index: int, data: dict) -> Slot:
        with self._lock:
            slot = self.get(index)
            merged = slot.model_dump()
            for k in ("material", "color", "name", "loaded", "remaining_g"):
                if k in data:
                    merged[k] = data[k]
            merged["material"] = str(merged["material"]).upper()
            if merged["material"] not in MATERIALS:
                raise ValueError(f"Unbekanntes Material {merged['material']}")
            new = Slot.model_validate(merged)
            self.state.slots[index] = new
            self._save()
            return new

    def to_json(self) -> str:
        return json.dumps([s.model_dump() for s in self.state.slots])
