"""ARTIFEX in-house slicer.

Pipeline per layer:  mesh section -> islands -> perimeters (inner first, outer last)
-> solid/sparse infill split (top/bottom detection) -> support regions -> G-code.

It intentionally stays simple and readable (no bridges/ironing/arc fitting), but it
produces complete, Klipper-ready G-code for single-object prints, including skirt/brim,
supports, retraction, cooling, minimum layer time and AFS colour changes.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import trimesh
from shapely.geometry.base import BaseGeometry

from . import geometry as G
from .gcode import TYPE_NAMES, GCodeWriter, LayerPlan, PathSpec, path_time
from .settings import SliceSettings

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, float], None]

VERSION = "0.1.0"


class SliceError(RuntimeError):
    pass


@dataclass
class SliceResult:
    gcode_path: Path
    layers: int
    estimated_time_s: float
    filament_mm: float
    filament_g: float
    max_z: float
    warnings: list[str] = field(default_factory=list)
    slicer: str = "ARTIFEX Slicer " + VERSION

    def to_dict(self) -> dict:
        return {
            "gcode": self.gcode_path.name,
            "layers": self.layers,
            "estimated_time_s": round(self.estimated_time_s),
            "filament_mm": round(self.filament_mm, 1),
            "filament_g": round(self.filament_g, 1),
            "max_z": round(self.max_z, 2),
            "warnings": self.warnings,
            "slicer": self.slicer,
        }


def layer_tops(height: float, s: SliceSettings) -> list[tuple[float, float]]:
    """(z_top, layer_height) for every layer."""
    out = [(s.first_layer_height, s.first_layer_height)]
    z = s.first_layer_height
    while z + s.layer_height / 2 < height:
        z = round(z + s.layer_height, 4)
        out.append((z, s.layer_height))
    return out


class ArtifexSlicer:
    name = "artifex"

    def slice(
        self,
        stl_path: Path,
        out_path: Path,
        s: SliceSettings,
        progress: ProgressFn | None = None,
    ) -> SliceResult:
        progress = progress or (lambda msg, frac: None)
        t0 = time.monotonic()
        warnings: list[str] = []
        mesh = trimesh.load(stl_path, force="mesh")
        if mesh.is_empty:
            raise SliceError("Leeres Modell")
        zmin = mesh.bounds[0][2]
        if abs(zmin) > 1e-3:
            mesh.apply_translation([0, 0, -zmin])
        height = float(mesh.bounds[1][2])
        b = mesh.bounds
        if b[0][0] < 0 or b[0][1] < 0 or b[1][0] > s.bed_x or b[1][1] > s.bed_y:
            raise SliceError("Modell liegt außerhalb des Druckbetts")

        tops = layer_tops(height, s)
        if len(tops) < 1:
            raise SliceError("Modell ist zu flach")
        progress("Schneide Schichten", 0.02)
        regions = self._sections(mesh, [z - h / 2 for z, h in tops], warnings)
        if all(r.is_empty for r in regions):
            raise SliceError("Keine druckbaren Querschnitte gefunden (Mesh defekt?)")

        progress("Erkenne Ober-/Unterseiten", 0.25)
        sparse_masks = self._sparse_masks(regions, s)
        support = self._supports(regions, tops, s) if s.supports else [G.EMPTY] * len(regions)

        layers: list[LayerPlan] = []
        pos = (s.bed_x / 2, s.bed_y)
        n = len(tops)
        for i, ((z, h), region) in enumerate(zip(tops, regions)):
            if i % 10 == 0:
                progress(f"Plane Schicht {i + 1}/{n}", 0.3 + 0.55 * i / n)
            layer = LayerPlan(index=i, z=z, height=h)
            if i == 0:
                self._skirt_brim(layer, region, s)
            pos = self._plan_layer(layer, region, sparse_masks[i], support[i], s, pos)
            if layer.paths:
                layers.append(layer)

        if not layers:
            raise SliceError("Keine Druckpfade erzeugt - Modell zu dünn?")
        progress("Schreibe G-Code", 0.9)
        writer = self._emit(layers, s)
        if writer.bounds[0] < -0.01 or writer.bounds[1] < -0.01 or writer.bounds[2] > s.bed_x + 0.01 or writer.bounds[3] > s.bed_y + 0.01:
            raise SliceError("Druckpfade außerhalb des Betts")
        grams = writer.filament_mm * math.pi * (s.filament_diameter / 2) ** 2 / 1000.0 * s.density
        result = SliceResult(
            gcode_path=out_path,
            layers=len(layers),
            estimated_time_s=writer.time_s + 180,  # + heat-up / homing / purge
            filament_mm=writer.filament_mm,
            filament_g=grams,
            max_z=layers[-1].z,
            warnings=warnings,
        )
        self._write(out_path, writer, s, result)
        log.info("Sliced %s: %d layers in %.1fs", stl_path.name, len(layers), time.monotonic() - t0)
        progress("Fertig geslict", 1.0)
        return result

    # -- geometry ------------------------------------------------------------------
    def _sections(self, mesh: trimesh.Trimesh, heights: list[float], warnings: list[str]) -> list[BaseGeometry]:
        paths = mesh.section_multiplane(plane_origin=[0, 0, 0], plane_normal=[0, 0, 1], heights=heights)
        regions: list[BaseGeometry] = []
        bad = 0
        for p in paths:
            if p is None:
                regions.append(G.EMPTY)
                continue
            try:
                regions.append(G.clean(G.union(p.polygons_full), simplify=0.01))
            except Exception:  # noqa: BLE001 - broken section loops
                bad += 1
                regions.append(G.EMPTY)
        if bad:
            warnings.append(f"{bad} Schichten mit defekten Konturen übersprungen")
        return regions

    def _sparse_masks(self, regions: list[BaseGeometry], s: SliceSettings) -> list[BaseGeometry]:
        """Area that may get sparse infill: covered by `top_layers` layers above and
        `bottom_layers` layers below. Everything else in the infill area becomes solid."""
        n = len(regions)
        masks: list[BaseGeometry] = []
        w = s.line_width
        for i in range(n):
            if s.infill_density >= 0.999 or i - s.bottom_layers < 0 or i + s.top_layers >= n:
                masks.append(G.EMPTY)
                continue
            window = regions[i - s.bottom_layers : i + s.top_layers + 1]
            mask = G.intersection_all(window)
            # Open the mask a bit so tiny sparse slivers become solid.
            mask = G.offset(G.offset(mask, -w), w) if not mask.is_empty else mask
            masks.append(mask)
        return masks

    def _supports(self, regions: list[BaseGeometry], tops: list[tuple[float, float]], s: SliceSettings) -> list[BaseGeometry]:
        n = len(regions)
        out: list[BaseGeometry] = [G.EMPTY] * n
        carry: BaseGeometry = G.EMPTY
        gap = s.support_z_gap_layers
        for i in range(n - 1, -1, -1):
            k = i + 1 + gap  # layer that needs support from layer i
            if 1 <= k < n:
                allowed = tops[k][1] * math.tan(math.radians(s.support_angle))
                overhang = regions[k].difference(G.offset(regions[k - 1], allowed))
                # Per-layer overhangs of gentle slopes are thin rings - keep them (no
                # morphological opening), only drop numerical noise.
                overhang = G.remove_small(overhang, 0.3)
                carry = G.union([carry, overhang])
            if carry.is_empty:
                continue
            # The model itself blocks the column below (support rests on the model) ...
            carry = G.remove_small(carry.difference(regions[i]), 0.3).simplify(0.05)
            # ... and the printed support keeps an XY gap so it can be removed.
            out[i] = G.remove_small(carry.difference(G.offset(regions[i], s.support_xy_gap)), 1.0)
        return out

    # -- per-layer planning -------------------------------------------------------
    def _speed(self, s: SliceSettings, base: float, first: bool) -> float:
        return s.first_layer_speed if first else base * s.speed_factor

    def _skirt_brim(self, layer: LayerPlan, region: BaseGeometry, s: SliceSettings) -> None:
        w = s.first_layer_line_width
        if s.brim_width > 0:
            loops = []
            for k in range(max(1, int(round(s.brim_width / w)))):
                for ring, is_hole in G.rings(G.offset(region, w / 2 + k * w)):
                    if not is_hole:
                        loops.append(G.ring_points(ring))
            for pts in reversed(loops):  # outside -> inside, ends next to the part
                layer.paths.append(PathSpec("brim", pts, w, s.first_layer_speed))
        if s.skirt_loops > 0:
            base = G.offset(region, s.skirt_distance + s.brim_width).convex_hull
            for k in range(s.skirt_loops):
                ring = G.offset(base, k * w)
                for poly in G.polygons(ring):
                    layer.paths.insert(0, PathSpec("skirt", G.ring_points(poly.exterior), w, s.first_layer_speed))

    def _plan_layer(
        self,
        layer: LayerPlan,
        region: BaseGeometry,
        sparse_mask: BaseGeometry,
        support: BaseGeometry,
        s: SliceSettings,
        pos: tuple[float, float],
    ) -> tuple[float, float]:
        first = layer.index == 0
        w = s.first_layer_line_width if first else s.line_width
        islands = G.polygons(region)
        if layer.paths:
            pos = tuple(layer.paths[-1].points[-1])  # type: ignore[assignment]

        while islands:
            # nearest island next
            d = [isl.centroid.distance(_pt(pos)) for isl in islands]
            isl = islands.pop(int(np.argmin(d)))
            pos = self._plan_island(layer, isl, sparse_mask, s, w, first, pos)

        if not support.is_empty:
            spacing = s.line_width / max(s.support_density, 0.01)
            segs = G.hatch(support, spacing, 0.0)  # same angle every layer -> stable walls
            for pts in G.order_paths(segs, pos):
                layer.paths.append(PathSpec("support", pts, w, self._speed(s, s.support_speed, first)))
                pos = tuple(pts[-1])  # type: ignore[assignment]
        return pos

    def _plan_island(self, layer, isl, sparse_mask, s: SliceSettings, w: float, first: bool, pos):
        shells: list[list[tuple[np.ndarray, bool]]] = []
        for k in range(s.perimeters):
            shell = G.offset(isl, -(w / 2 + k * w))
            if shell.is_empty:
                break
            shells.append([(np.asarray(r.coords), hole) for r, hole in G.rings(shell)])
        if not shells:
            # Feature thinner than one line: trace its outline once at reduced width.
            thin = G.offset(isl, -w / 4)
            for ring, _ in G.rings(thin):
                pts = G.ring_points(ring, pos)
                layer.paths.append(PathSpec("outer_wall", pts, w * 0.8, self._speed(s, s.outer_perimeter_speed, first)))
                pos = tuple(pts[-1])
            return pos

        # Inner walls first, outer wall last (better surface & dimensional accuracy).
        seam = (isl.centroid.x, isl.bounds[3] + 50)  # aligned seam at the back
        for k in range(len(shells) - 1, -1, -1):
            for coords, _hole in shells[k]:
                ring_start = seam if k == 0 else pos
                pts = _rotate_ring(coords, ring_start)
                kind = "outer_wall" if k == 0 else "inner_wall"
                speed = s.outer_perimeter_speed if k == 0 else s.perimeter_speed
                layer.paths.append(PathSpec(kind, pts, w, self._speed(s, speed, first)))
                pos = tuple(pts[-1])

        inner = G.offset(isl, -(len(shells) * w) + s.infill_overlap * w)
        if inner.is_empty:
            return pos
        sparse = inner.intersection(sparse_mask) if not sparse_mask.is_empty else G.EMPTY
        solid = inner.difference(sparse) if not sparse.is_empty else inner

        angle = 45.0 if layer.index % 2 == 0 else 135.0
        segs = G.hatch(solid, w, angle)
        for pts in G.order_paths(segs, pos):
            layer.paths.append(PathSpec("solid", pts, w, self._speed(s, s.solid_infill_speed, first)))
            pos = tuple(pts[-1])

        if not sparse.is_empty and s.infill_density > 0.001:
            segs = []
            dens = s.infill_density
            if s.infill_pattern == "grid":
                sp = 2 * w / dens
                segs = G.hatch(sparse, sp, 45.0) + G.hatch(sparse, sp, 135.0)
            elif s.infill_pattern == "triangles":
                sp = 3 * w / dens
                segs = G.hatch(sparse, sp, 0.0) + G.hatch(sparse, sp, 60.0) + G.hatch(sparse, sp, 120.0)
            else:
                segs = G.hatch(sparse, w / dens, angle)
            for pts in G.order_paths(segs, pos):
                layer.paths.append(PathSpec("sparse", pts, w, self._speed(s, s.infill_speed, first)))
                pos = tuple(pts[-1])
        return pos

    # -- output -------------------------------------------------------------------
    def _emit(self, layers: list[LayerPlan], s: SliceSettings) -> GCodeWriter:
        wr = GCodeWriter(s)
        changes = sorted(s.color_changes, key=lambda c: c.z)
        est_total = sum(sum(path_time(p) for p in L.paths) for L in layers) or 1.0
        done_t = 0.0
        fan_on = False
        for L in layers:
            while changes and changes[0].z <= L.z - L.height / 2:
                c = changes.pop(0)
                wr.retract()
                wr.comment(f"AFS colour change at Z{c.z:.2f}")
                wr.line(f"AFS_CHANGE SLOT={c.slot}")
                wr.forget_position()
            wr.line(";LAYER_CHANGE")
            wr.line(f";Z:{L.z:.3f}")
            wr.line(f";HEIGHT:{L.height:.3f}")
            wr.line(f"SET_PRINT_STATS_INFO CURRENT_LAYER={L.index + 1}")
            wr.line(f"M73 P{int(100 * done_t / est_total)}")
            if L.index == 1 and s.first_layer_nozzle_temp:
                wr.line(f"M104 S{s.nozzle_temp:.0f}")
            if not fan_on and L.index >= s.disable_fan_first_layers and s.fan_speed > 0:
                wr.line(f"M106 S{int(255 * s.fan_speed / 100)}")
                fan_on = True
            wr.set_z(L.z)

            layer_t = sum(path_time(p) for p in L.paths)
            factor = 1.0
            if s.min_layer_time > 0 and 0 < layer_t < s.min_layer_time and L.index > 0:
                factor = layer_t / s.min_layer_time
            current = None
            for p in L.paths:
                if p.kind != current:
                    wr.line(f";TYPE:{TYPE_NAMES.get(p.kind, p.kind)}")
                    current = p.kind
                speed = max(p.speed * factor, s.min_speed) if factor < 1 else p.speed
                wr.extrude(p.points, p.width, L.height, speed)
            done_t += layer_t
        wr.retract()
        wr.line("M73 P100")
        return wr

    def _write(self, out_path: Path, wr: GCodeWriter, s: SliceSettings, r: SliceResult) -> None:
        start_temp = s.first_layer_nozzle_temp or s.nozzle_temp
        header = [
            f"; generated by ARTIFEX Slicer {VERSION}",
            ";FLAVOR:Klipper",
            f"; estimated printing time (normal mode) = {_fmt_time(r.estimated_time_s)}",
            f"; filament used [mm] = {r.filament_mm:.1f}",
            f"; filament used [g] = {r.filament_g:.1f}",
            f"; filament_type = {s.material}",
            f"; layer_height = {s.layer_height}",
            f"; first_layer_height = {s.first_layer_height}",
            f"; nozzle_diameter = {s.nozzle_diameter}",
            f"; total_layers = {r.layers}",
            f"; max_z = {r.max_z:.3f}",
            f"; artifex_settings = {s.model_dump_json(exclude={'color_changes'})}",
            "",
            f"SET_PRINT_STATS_INFO TOTAL_LAYER={r.layers}",
            f"PRINT_START BED={s.bed_temp:.0f} EXTRUDER={start_temp:.0f} SLOT={s.afs_slot} "
            f"MATERIAL={s.material} FILTER={1 if s.filtration else 0}",
            "G90",
            "M83 ; relative extrusion",
            "G92 E0",
            "M107",
        ]
        footer = ["M107", "PRINT_END", f"; ARTIFEX total layers {r.layers}"]
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(header) + "\n")
            fh.write("\n".join(wr.out) + "\n")
            fh.write("\n".join(footer) + "\n")


def _pt(p):
    from shapely.geometry import Point

    return Point(p[0], p[1])


def _rotate_ring(coords: np.ndarray, start_near: tuple[float, float]) -> np.ndarray:
    pts = coords[:-1] if len(coords) > 1 and np.allclose(coords[0], coords[-1]) else coords
    d = np.hypot(pts[:, 0] - start_near[0], pts[:, 1] - start_near[1])
    k = int(np.argmin(d))
    pts = np.roll(pts, -k, axis=0)
    return np.vstack([pts, pts[:1]])


def _fmt_time(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
