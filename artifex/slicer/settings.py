"""Slice settings, material presets and validated overrides."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MATERIALS: dict[str, dict[str, Any]] = {
    "PLA": {"nozzle_temp": 210, "bed_temp": 60, "fan_speed": 100, "density": 1.24, "filtration": False,
            "retract_length": 0.8, "speed_factor": 1.0},
    "PETG": {"nozzle_temp": 240, "bed_temp": 75, "fan_speed": 40, "density": 1.27, "filtration": False,
             "retract_length": 1.0, "speed_factor": 0.85},
    "ABS": {"nozzle_temp": 250, "bed_temp": 100, "fan_speed": 0, "density": 1.04, "filtration": True,
            "retract_length": 0.8, "speed_factor": 0.9},
    "ASA": {"nozzle_temp": 255, "bed_temp": 100, "fan_speed": 20, "density": 1.07, "filtration": True,
            "retract_length": 0.8, "speed_factor": 0.9},
    "TPU": {"nozzle_temp": 228, "bed_temp": 50, "fan_speed": 60, "density": 1.21, "filtration": False,
            "retract_length": 0.4, "speed_factor": 0.4},
}

# (min, max) for every value the AI or the user may change.
LIMITS: dict[str, tuple[float, float]] = {
    "layer_height": (0.06, 0.3),
    "first_layer_height": (0.15, 0.32),
    "line_width": (0.35, 0.7),
    "perimeters": (1, 8),
    "top_layers": (0, 12),
    "bottom_layers": (0, 10),
    "infill_density": (0.0, 1.0),
    "nozzle_temp": (170, 290),
    "bed_temp": (0, 110),
    "fan_speed": (0, 100),
    "brim_width": (0, 15),
    "skirt_loops": (0, 5),
    "speed_factor": (0.3, 1.5),
    "support_angle": (30, 75),
    "support_density": (0.05, 0.5),
    "retract_length": (0.0, 6.0),
    "z_hop": (0.0, 1.0),
    "min_layer_time": (0, 30),
}


class ColorChange(BaseModel):
    z: float
    slot: int


class SliceSettings(BaseModel):
    material: str = "PLA"
    afs_slot: int = 0
    nozzle_diameter: float = 0.4
    filament_diameter: float = 1.75
    bed_x: float = 235.0
    bed_y: float = 235.0

    layer_height: float = 0.2
    first_layer_height: float = 0.24
    line_width: float = 0.45
    first_layer_line_width: float = 0.5
    perimeters: int = 2
    top_layers: int = 4
    bottom_layers: int = 3
    infill_density: float = 0.15
    infill_pattern: Literal["grid", "lines", "triangles"] = "grid"
    infill_overlap: float = 0.15  # fraction of line width

    # mm/s
    outer_perimeter_speed: float = 40
    perimeter_speed: float = 60
    infill_speed: float = 100
    solid_infill_speed: float = 70
    support_speed: float = 70
    first_layer_speed: float = 25
    travel_speed: float = 200
    min_speed: float = 10
    speed_factor: float = 1.0
    min_layer_time: float = 6.0
    # Used for the print time estimate (keep in sync with printer.cfg).
    accel: float = 3000.0
    corner_speed: float = 8.0

    retract_length: float = 0.8
    retract_speed: float = 35
    retract_min_travel: float = 1.5
    z_hop: float = 0.2

    nozzle_temp: float = 210
    first_layer_nozzle_temp: float | None = None
    bed_temp: float = 60
    fan_speed: float = 100  # percent
    disable_fan_first_layers: int = 1
    density: float = 1.24  # g/cm3
    filtration: bool = False

    skirt_loops: int = 1
    skirt_distance: float = 5.0
    brim_width: float = 0.0

    supports: bool = False
    support_angle: float = 50.0  # degrees from vertical
    support_density: float = 0.15
    support_xy_gap: float = 0.7
    support_z_gap_layers: int = 1

    color_changes: list[ColorChange] = Field(default_factory=list)

    @classmethod
    def for_material(cls, material: str | None, **kw: Any) -> "SliceSettings":
        mat = (material or "PLA").upper()
        preset = MATERIALS.get(mat, MATERIALS["PLA"])
        base = {k: v for k, v in preset.items()}
        base.update(kw)
        return cls(material=mat if mat in MATERIALS else "PLA", **base)

    def with_overrides(self, overrides: dict[str, Any]) -> "SliceSettings":
        data = self.model_dump()
        if "material" in overrides and overrides["material"]:
            mat = str(overrides["material"]).upper()
            if mat in MATERIALS:
                data.update(MATERIALS[mat])
                data["material"] = mat
        for key, value in overrides.items():
            if key == "material" or key not in data or value is None:
                continue
            data[key] = clamp(key, value)
        return SliceSettings.model_validate(data)


def clamp(key: str, value: Any) -> Any:
    if key not in LIMITS or isinstance(value, bool):
        return value
    lo, hi = LIMITS[key]
    try:
        v = float(value)
    except (TypeError, ValueError):
        return value
    v = min(max(v, lo), hi)
    if key in ("perimeters", "top_layers", "bottom_layers", "skirt_loops"):
        return int(round(v))
    return v
