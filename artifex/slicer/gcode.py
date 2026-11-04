"""G-code writer for Klipper (relative extrusion, ARTIFEX macros)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .settings import SliceSettings

Z_ACCEL = 100.0  # printer.cfg max_z_accel


@dataclass
class PathSpec:
    kind: str  # outer_wall, inner_wall, solid, sparse, support, skirt, brim
    points: np.ndarray  # (N, 2)
    width: float
    speed: float  # mm/s before layer-time scaling


@dataclass
class LayerPlan:
    index: int
    z: float
    height: float
    paths: list[PathSpec] = field(default_factory=list)


TYPE_NAMES = {
    "outer_wall": "Outer wall",
    "inner_wall": "Inner wall",
    "solid": "Solid infill",
    "sparse": "Sparse infill",
    "support": "Support",
    "skirt": "Skirt",
    "brim": "Brim",
}


class GCodeWriter:
    def __init__(self, s: SliceSettings):
        self.s = s
        self.out: list[str] = []
        self.x: float | None = None
        self.y: float | None = None
        self.z = 0.0
        self.retracted = False
        self.time_s = 0.0
        self.filament_mm = 0.0
        self.bounds = [math.inf, math.inf, -math.inf, -math.inf]
        r = s.filament_diameter / 2
        self._filament_area = math.pi * r * r

    # -- low level -------------------------------------------------------------
    def line(self, text: str) -> None:
        self.out.append(text)

    def comment(self, text: str) -> None:
        self.out.append(f";{text}")

    def e_per_mm(self, width: float, height: float) -> float:
        # Slic3r's rounded-rectangle bead model.
        area = (width - height) * height + math.pi * (height / 2) ** 2
        return area / self._filament_area

    def retract(self) -> None:
        if self.retracted or self.s.retract_length <= 0:
            return
        self.line(f"G1 E-{self.s.retract_length:.3f} F{self.s.retract_speed * 60:.0f}")
        self.time_s += self.s.retract_length / self.s.retract_speed
        if self.s.z_hop > 0:
            self.line(f"G1 Z{self.z + self.s.z_hop:.3f} F1200")
            self.time_s += move_time(self.s.z_hop, 20.0, Z_ACCEL, 0.0)
        self.retracted = True

    def unretract(self) -> None:
        if not self.retracted:
            return
        if self.s.z_hop > 0:
            self.line(f"G1 Z{self.z:.3f} F1200")
            self.time_s += move_time(self.s.z_hop, 20.0, Z_ACCEL, 0.0)
        self.line(f"G1 E{self.s.retract_length:.3f} F{self.s.retract_speed * 60:.0f}")
        self.time_s += self.s.retract_length / self.s.retract_speed
        self.retracted = False

    def travel(self, x: float, y: float) -> None:
        if self.x is not None and self.y is not None:
            dist = math.hypot(x - self.x, y - self.y)
            if dist < 1e-4:
                return
            if dist > self.s.retract_min_travel:
                self.retract()
            self.time_s += move_time(dist, self.s.travel_speed, self.s.accel, self.s.corner_speed)
        else:
            self.retract()
        self.line(f"G0 X{x:.3f} Y{y:.3f} F{self.s.travel_speed * 60:.0f}")
        self.x, self.y = x, y

    def set_z(self, z: float) -> None:
        self.time_s += move_time(abs(z - self.z), 20.0, Z_ACCEL, 0.0)
        self.z = z
        hop = self.s.z_hop if self.retracted else 0.0
        self.line(f"G0 Z{z + hop:.3f} F1200")

    def extrude(self, points: np.ndarray, width: float, height: float, speed: float) -> None:
        if len(points) < 2:
            return
        self.travel(float(points[0][0]), float(points[0][1]))
        self.unretract()
        epm = self.e_per_mm(width, height)
        feed = f" F{speed * 60:.0f}"
        for px, py in points[1:]:
            dist = math.hypot(px - self.x, py - self.y)  # type: ignore[operator]
            if dist < 1e-4:
                continue
            e = dist * epm
            self.line(f"G1 X{px:.3f} Y{py:.3f} E{e:.5f}{feed}")
            feed = ""
            self.filament_mm += e
            self.time_s += move_time(dist, speed, self.s.accel, self.s.corner_speed)
            self.x, self.y = float(px), float(py)
        b = self.bounds
        b[0] = min(b[0], float(points[:, 0].min()))
        b[1] = min(b[1], float(points[:, 1].min()))
        b[2] = max(b[2], float(points[:, 0].max()))
        b[3] = max(b[3], float(points[:, 1].max()))

    def forget_position(self) -> None:
        """After a macro that moves the head (e.g. filament change)."""
        self.x = self.y = None
        self.retracted = False


def move_time(length: float, speed: float, accel: float, v0: float) -> float:
    """Trapezoidal motion profile; moves start/end at the corner speed ``v0``."""
    if length <= 0:
        return 0.0
    v0 = min(v0, speed)
    d_acc = (speed * speed - v0 * v0) / (2 * accel)
    if length >= 2 * d_acc:
        return 2 * (speed - v0) / accel + (length - 2 * d_acc) / speed
    vp = math.sqrt(accel * length + v0 * v0)
    return 2 * (vp - v0) / accel


def path_time(path: PathSpec) -> float:
    d = np.diff(path.points, axis=0)
    return float(np.hypot(d[:, 0], d[:, 1]).sum()) / max(path.speed, 1e-6)
